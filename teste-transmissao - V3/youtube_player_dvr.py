#!/usr/bin/env python3
"""DVR do player para lives do YouTube.

Cria um HLS local separado em data/<sessao>/player_dvr/ usando os
fragmentos DASH históricos expostos pelo yt-dlp com live_from_start=True.
Não lê nem altera o live.m3u8 principal e não participa dos grids.
"""
from __future__ import annotations

import itertools
import math
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from youtube_dvr_test import youtube_url, choose_formats, resolve_binary


class PlayerDVR:
    def __init__(self):
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = self._empty_state()

    @staticmethod
    def _empty_state():
        return {
            "running": False,
            "ready": False,
            "session_id": None,
            "playlist": None,
            "segments": 0,
            "seconds_ready": 0.0,
            "estimated_history_sec": 0.0,
            "first_sequence": None,
            "last_sequence": None,
            "live_edge_sequence": None,
            "caught_up": False,
            "error": None,
            "grid_source": "live",
            "session_dir": None,
        }

    def status(self):
        with self._lock:
            return dict(self._state)

    def stop(self):
        self._stop.set()
        marker = None
        with self._lock:
            self._state["running"] = False
            self._state["grid_source"] = "live"
            session_dir = self._state.get("session_dir")
            if session_dir:
                marker = Path(session_dir) / "player_dvr" / "grid_source.flag"
        if marker:
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass
        return self.status()

    def start(self, session_id: str, url: str, session_dir: Path, log=print):
        with self._lock:
            if self._thread and self._thread.is_alive():
                if self._state.get("session_id") == session_id:
                    return dict(self._state)
                raise RuntimeError("já existe um DVR do player sendo preparado")

            self._stop = threading.Event()
            self._state = self._empty_state()
            self._state.update(running=True, session_id=session_id, session_dir=str(session_dir), grid_source="live")
            self._thread = threading.Thread(
                target=self._worker,
                args=(session_id, url, Path(session_dir), log),
                name=f"player-dvr-{session_id}",
                daemon=True,
            )
            self._thread.start()
            return dict(self._state)

    def activate_grids(self):
        """Ativa os grids DVR somente depois que o vídeo histórico começou."""
        with self._lock:
            if not self._state.get("ready"):
                raise RuntimeError("DVR ainda não está pronto")
            session_dir = self._state.get("session_dir")
            if not session_dir:
                raise RuntimeError("sessão DVR inválida")
            marker = Path(session_dir) / "player_dvr" / "grid_source.flag"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("dvr\n", encoding="utf-8")
            self._state["grid_source"] = "dvr"
        return self.status()

    def _set(self, **values):
        with self._lock:
            self._state.update(values)

    @staticmethod
    def _seq(fragment):
        return int(parse_qs(urlparse(fragment["url"]).query)["sq"][0])

    @staticmethod
    def _write_playlist(path: Path, segment_rows, target_duration: int, ended=False):
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{max(1, target_duration)}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:EVENT",
            "#EXT-X-INDEPENDENT-SEGMENTS",
        ]
        for name, duration in segment_rows:
            # Todos os segmentos usam timestamps progressivos na mesma timeline.
            lines.append(f"#EXTINF:{duration:.3f},")
            lines.append(name)
        if ended:
            lines.append("#EXT-X-ENDLIST")
        content = "\n".join(lines) + "\n"

        # No Windows o navegador pode estar lendo live.m3u8 exatamente
        # quando tentamos substituí-lo. Nesse caso Path.replace() pode
        # retornar WinError 5 (Access denied). Isso é transitório e não
        # deve derrubar a thread inteira do DVR.
        #
        # Usamos um .tmp exclusivo por thread e tentamos a troca várias
        # vezes com backoff curto. Se o arquivo continuar bloqueado,
        # fazemos uma última tentativa escrevendo diretamente no playlist.
        tmp = path.with_name(
            f"{path.stem}.{threading.get_ident()}.tmp"
        )

        last_error = None

        for attempt in range(20):
            try:
                tmp.write_text(content, encoding="utf-8")
                tmp.replace(path)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(min(0.02 * (attempt + 1), 0.25))
            except OSError as exc:
                # WinError 5 = arquivo temporariamente bloqueado.
                if getattr(exc, "winerror", None) != 5:
                    raise
                last_error = exc
                time.sleep(min(0.02 * (attempt + 1), 0.25))

        # Fallback: se o rename continuar bloqueado, tenta atualizar o
        # próprio arquivo. O playlist é pequeno e será reescrito de novo
        # no próximo bloco, então uma disputa momentânea não encerra o DVR.
        for attempt in range(10):
            try:
                path.write_text(content, encoding="utf-8")
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.10)
            except OSError as exc:
                if getattr(exc, "winerror", None) != 5:
                    raise
                last_error = exc
                time.sleep(0.10)

        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

        raise RuntimeError(
            f"playlist DVR permaneceu bloqueado pelo Windows: {last_error}"
        )

    @staticmethod
    def _download_track(ydl, options, info, fmt, fragments, output: Path):
        from yt_dlp.downloader.dash import DashSegmentsFD
        finite = {
            **info,
            **fmt,
            "fragments": fragments,
            "protocol": "http_dash_segments",
            "is_live": False,
            "is_from_start": False,
        }
        finite.pop("requested_formats", None)
        finite.pop("requested_downloads", None)
        ok, _ = DashSegmentsFD(ydl, options).download(str(output), finite)
        if not ok:
            raise RuntimeError("yt-dlp não conseguiu baixar um bloco DVR")

    @staticmethod
    def _mux_ts(
        ffmpeg: str,
        video: Path,
        audio: Path,
        out: Path,
        timeline_offset: float,
    ):
        """Muxa um segmento TS em uma timeline contínua."""
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(video), "-i", str(audio),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
            "-bsf:v", "h264_mp4toannexb",
            "-output_ts_offset", f"{max(0.0, timeline_offset):.6f}",
            "-muxdelay", "0",
            "-muxpreload", "0",
            "-f", "mpegts", str(out),
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        if r.returncode != 0 or not out.is_file() or out.stat().st_size < 1024:
            err = (r.stderr or b"").decode("utf-8", "replace")[-600:]
            raise RuntimeError("FFmpeg não montou o bloco DVR: " + err)

    def _worker(self, session_id: str, raw_url: str, session_dir: Path, log):
        try:
            import yt_dlp
            from yt_dlp.networking import Request

            url = youtube_url(raw_url)
            ffmpeg = resolve_binary(None, "ffmpeg")

            out_dir = session_dir / "player_dvr"
            chunks_dir = out_dir / "chunks"
            temp_dir = out_dir / "tmp"
            for d in (out_dir, chunks_dir, temp_dir):
                d.mkdir(parents=True, exist_ok=True)
            marker = out_dir / "grid_source.flag"
            # Enquanto o buffer histórico é preparado, grids continuam LIVE.
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass

            # Limpa somente o DVR do player desta sessão.
            for p in chunks_dir.glob("dvr_*.ts"):
                try: p.unlink()
                except OSError: pass
            for p in temp_dir.glob("*"):
                try: p.unlink()
                except OSError: pass

            playlist = out_dir / "live.m3u8"
            options = dict(
                quiet=True,
                noplaylist=True,
                live_from_start=True,
                socket_timeout=15,
                retries=2,
                fragment_retries=2,
                extractor_retries=1,
                concurrent_fragment_downloads=1,
                skip_unavailable_fragments=False,
                noprogress=True,
                continuedl=False,
                nopart=False,
                fixup="never",
                cachedir=False,
            )

            class QuietLogger:
                def debug(self, _): pass
                def info(self, _): pass
                def warning(self, msg): log(f"DVR yt-dlp: {msg}")
                def error(self, msg): log(f"DVR yt-dlp erro: {msg}")
            options["logger"] = QuietLogger()

            with yt_dlp.YoutubeDL(options) as ydl:
                log("DVR player: analisando histórico do YouTube…")
                info = ydl.extract_info(url, download=False, process=False)
                if not isinstance(info, dict) or not (info.get("is_live") or info.get("live_status") == "is_live"):
                    raise RuntimeError("a URL não está em uma live ativa do YouTube")
                if info.get("is_live_dvr_enabled") is False:
                    raise RuntimeError("o YouTube informou que o DVR desta live está desativado")

                video_fmt, audio_fmt = choose_formats(info.get("formats", []))
                if float(video_fmt["target_duration"]) != float(audio_fmt["target_duration"]):
                    raise RuntimeError("vídeo e áudio DVR usam durações incompatíveis")

                duration = float(video_fmt["target_duration"])
                # Blocos curtos deixam o HLS crescer continuamente.
                # Em lives de 2 s, normalmente gera blocos de ~4 s.
                per_chunk = max(1, min(4, math.ceil(4.0 / max(duration, 0.5))))

                try:
                    with ydl.urlopen(Request(video_fmt["url"], method="HEAD", headers=video_fmt.get("http_headers", {}))) as response:
                        initial_head = int(response.headers["X-Head-Seqnum"])
                except Exception as exc:
                    raise RuntimeError(f"não consegui descobrir a borda ao vivo: {exc}") from exc

                vg = video_fmt["fragments"]({"start": time.time(), "fragment_index": 0})
                ag = audio_fmt["fragments"]({"start": time.time(), "fragment_index": 0})
                try:
                    vf = next(vg)
                    af = next(ag)
                    first_v = self._seq(vf)
                    first_a = self._seq(af)
                    if first_v != first_a:
                        raise RuntimeError("o início DVR de vídeo e áudio não coincide")

                    first_seq = first_v
                    estimate = max(0.0, (initial_head - first_seq + 1) * duration)
                    rel_playlist = f"/media/{session_id}/player_dvr/live.m3u8"
                    self._set(
                        playlist=rel_playlist,
                        first_sequence=first_seq,
                        live_edge_sequence=initial_head,
                        estimated_history_sec=estimate,
                    )
                    log(f"DVR player: sequência inicial={first_seq}, borda={initial_head}, histórico≈{estimate/60:.1f} min")

                    pending_v = [vf]
                    pending_a = [af]
                    rows = []
                    chunk_index = 0
                    last_seq = first_seq - 1

                    while not self._stop.is_set():
                        while len(pending_v) < per_chunk and not self._stop.is_set():
                            pending_v.append(next(vg))
                            pending_a.append(next(ag))

                        if self._stop.is_set():
                            break

                        # Garante alinhamento de sequência no bloco.
                        vseq = [self._seq(x) for x in pending_v]
                        aseq = [self._seq(x) for x in pending_a]
                        if vseq != aseq:
                            raise RuntimeError("fragmentos DVR de vídeo/áudio perderam alinhamento")

                        chunk_index += 1
                        video_tmp = temp_dir / f"v_{chunk_index:05d}.mp4"
                        audio_tmp = temp_dir / f"a_{chunk_index:05d}.m4a"
                        out_ts = chunks_dir / f"dvr_{chunk_index:05d}.ts"

                        self._download_track(ydl, options, info, video_fmt, list(pending_v), video_tmp)
                        self._download_track(ydl, options, info, audio_fmt, list(pending_a), audio_tmp)
                        self._mux_ts(ffmpeg, video_tmp, audio_tmp, out_ts, sum(d for _, d in rows))

                        for p in (video_tmp, audio_tmp):
                            try: p.unlink()
                            except OSError: pass

                        block_duration = len(pending_v) * duration
                        rows.append((f"chunks/{out_ts.name}", block_duration))
                        self._write_playlist(playlist, rows, math.ceil(block_duration) + 1)

                        last_seq = vseq[-1]
                        seconds_ready = sum(d for _, d in rows)
                        # Reserva inicial para o player. Os grids só mudam
                        # para DVR depois do evento "playing" no navegador.
                        buffer_ready = (
                            seconds_ready >= 24.0
                            or last_seq >= initial_head - 2
                        )
                        self._set(
                            ready=buffer_ready,
                            segments=len(rows),
                            seconds_ready=seconds_ready,
                            last_sequence=last_seq,
                        )
                        log(f"DVR player: bloco #{chunk_index} pronto ({seconds_ready:.0f}s desde o início disponível)")

                        pending_v = []
                        pending_a = []

                        # Quando alcançamos perto da borda observada no começo,
                        # já temos histórico suficiente para o usuário navegar.
                        # Continuamos consumindo o gerador, que acompanha a live.
                        if last_seq >= initial_head - 2:
                            self._set(caught_up=True)

                finally:
                    for gen in (locals().get("vg"), locals().get("ag")):
                        if gen is not None and hasattr(gen, "close"):
                            try: gen.close()
                            except Exception: pass

        except StopIteration:
            self._set(caught_up=True)
        except Exception as exc:
            self._set(error=str(exc))
            try: log(f"DVR player falhou: {exc}")
            except Exception: pass
        finally:
            self._set(running=False)


PLAYER_DVR = PlayerDVR()
