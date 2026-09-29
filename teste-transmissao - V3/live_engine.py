#!/usr/bin/env python3
"""
Motor de captura + HLS + grids.

Fluxo:
  URL
    -> yt-dlp resolve video/audio
    -> ffmpeg principal gera HLS
    -> segmentos .ts de 2 segundos
    -> navegador reproduz o HLS
    -> grids usam SOMENTE segmentos .ts já finalizados

IMPORTANTE:
  O gerador de grids NUNCA abre live.m3u8.

Grids disponíveis:
  2x2 = 4 imagens
  3x3 = 9 imagens
  4x4 = 16 imagens
  5x5 = 25 imagens

O tamanho do grid e o lote de processamento são
configurações independentes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


# ==============================================================
# CAMINHOS
# ==============================================================

ROOT = Path(__file__).resolve().parent

DATA = ROOT / "data"

LOGS = ROOT / "logs"


# ==============================================================
# CONFIGURAÇÕES
# ==============================================================

HLS_SEG_SEC = 2.0


# --------------------------------------------------------------
# GRID
# --------------------------------------------------------------

DEFAULT_GRID_SIZE = 2

MIN_GRID_SIZE = 2

MAX_GRID_SIZE = 5


# --------------------------------------------------------------
# PROCESSAMENTO
# --------------------------------------------------------------

DEFAULT_PROCESS_BATCH = 4

MIN_PROCESS_BATCH = 1

MAX_PROCESS_BATCH = 10


# --------------------------------------------------------------
# CHECKPOINT
# --------------------------------------------------------------

GRIDS_BATCH = 5


# --------------------------------------------------------------
# RECUPERAÇÃO
# --------------------------------------------------------------

MAX_RECOVERY_ATTEMPTS = 5

RECOVERY_DELAY_SEC = 3


# ==============================================================
# UTILIDADES
# ==============================================================

def _now() -> str:

    return datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def _safe(
    name: str,
) -> str:

    s = re.sub(
        r"[^\w\-]+",
        "_",
        (name or "live").strip(),
    )[:80]

    return s or "live"


def _clamp_grid_size(value) -> int:
    if isinstance(value, str) and value.strip() in ("2", "3", "4", "5"):
        value = int(value.strip())
    if type(value) is not int or not MIN_GRID_SIZE <= value <= MAX_GRID_SIZE:
        raise ValueError("grid_size deve ser um inteiro entre 2 e 5")
    return value


def _clamp_process_batch(
    value,
) -> int:

    try:

        value = int(value)

    except (
        TypeError,
        ValueError,
    ):

        value = DEFAULT_PROCESS_BATCH

    return max(
        MIN_PROCESS_BATCH,
        min(
            MAX_PROCESS_BATCH,
            value,
        ),
    )


# ==============================================================
# LIVE SESSION
# ==============================================================

class LiveSession:

    def __init__(
        self,
        url: str,
        *,
        name: str = "teste",
        generate_grids: bool = True,
        grid_size: int = DEFAULT_GRID_SIZE,
        process_batch: int = DEFAULT_PROCESS_BATCH,
        log: Callable[[str], None] | None = None,
    ):

        self.url = (
            url
            or ""
        ).strip()

        self.name = _safe(
            name
        )

        self.generate_grids = bool(
            generate_grids
        )

        # ------------------------------------------------------
        # TAMANHO DO GRID
        # ------------------------------------------------------

        self.grid_size = (
            _clamp_grid_size(
                grid_size
            )
        )

        # ------------------------------------------------------
        # LOTE DE PROCESSAMENTO
        # ------------------------------------------------------

        self.process_batch = (
            _clamp_process_batch(
                process_batch
            )
        )

        self._log = (
            log
            or (
                lambda m:
                print(
                    m,
                    flush=True,
                )
            )
        )

        # ------------------------------------------------------
        # ID DA SESSÃO
        # ------------------------------------------------------

        self.id = (
            f"{self.name}_"
            f"{int(time.time())}_"
            f"{uuid.uuid4().hex[:8]}"
        )

        # ------------------------------------------------------
        # DIRETÓRIOS
        # ------------------------------------------------------

        self.dir = (
            DATA
            / self.id
        )

        self.hls_dir = (
            self.dir
            / "hls"
        )

        self.frames_dir = (
            self.dir
            / "frames"
        )

        self.grids_dir = (
            self.dir
            / "grids"
        )

        self.player_dvr_dir = self.dir / "player_dvr"
        self.player_dvr_chunks_dir = self.player_dvr_dir / "chunks"
        self.player_dvr_grid_flag = self.player_dvr_dir / "grid_source.flag"

        self.m3u8 = (
            self.hls_dir
            / "live.m3u8"
        )

        self.meta_path = (
            self.dir
            / "session.json"
        )

        # ------------------------------------------------------
        # ESTADO
        # ------------------------------------------------------

        self.status = "idle"

        self.error: str | None = None

        self.resolved_url: str | None = None

        self.ffmpeg: subprocess.Popen | None = None

        self.frames_done = 0

        self.grids_done = 0

        self.started_at: float | None = None

        # ------------------------------------------------------
        # THREADS / LOCKS
        # ------------------------------------------------------

        self._stop = (
            threading.Event()
        )

        self._threads: list[
            threading.Thread
        ] = []

        self._ffmpeg_log = None

        self._process_lock = (
            threading.RLock()
        )

        self._meta_lock = (
            threading.Lock()
        )

        # ------------------------------------------------------
        # RECUPERAÇÃO
        # ------------------------------------------------------

        self.recovery_attempts = 0

        # ------------------------------------------------------
        # CRIAR DIRETÓRIOS
        # ------------------------------------------------------

        for d in (
            self.hls_dir,
            self.frames_dir,
            self.grids_dir,
            LOGS,
        ):

            d.mkdir(
                parents=True,
                exist_ok=True,
            )

    # ==========================================================
    # LOG
    # ==========================================================

    def log(
        self,
        msg: str,
    ) -> None:

        line = (
            f"[{_now()}] "
            f"[{self.id}] "
            f"{msg}"
        )

        self._log(
            line
        )

        try:

            with open(
                LOGS
                / f"{self.id}.log",
                "a",
                encoding="utf-8",
            ) as f:

                f.write(
                    line
                    + "\n"
                )

        except Exception:

            pass

    # ==========================================================
    # META
    # ==========================================================

    def _save_meta(
        self,
    ) -> None:

        meta = {

            "id":
                self.id,

            "url":
                self.url,

            "name":
                self.name,

            "generate_grids":
                self.generate_grids,

            "grid_size":
                self.grid_size,

            "grid_label":
                (
                    f"{self.grid_size}"
                    f"x"
                    f"{self.grid_size}"
                ),

            "frames_per_grid":
                (
                    self.grid_size
                    * self.grid_size
                ),

            "process_batch":
                self.process_batch,

            "status":
                self.status,

            "error":
                self.error,

            "recovery_attempts":
                self.recovery_attempts,

            "resolved_url":
                (
                    self.resolved_url
                    or ""
                )[:200],

            "frames_done":
                self.frames_done,

            "grids_done":
                self.grids_done,

            "started_at":
                self.started_at,

            "m3u8":
                str(
                    self.m3u8
                ).replace(
                    "\\",
                    "/",
                ),

            "hls_relative":
                (
                    f"/media/{self.id}"
                    "/hls/live.m3u8"
                ),

            "grids_relative":
                (
                    f"/media/{self.id}"
                    "/grids/"
                ),
        }

        with self._meta_lock:

            self.meta_path.write_text(

                json.dumps(
                    meta,
                    indent=2,
                    ensure_ascii=False,
                ),

                encoding="utf-8",
            )

    # ==========================================================
    # RESOLVE STREAM
    # ==========================================================

    def resolve_stream(
        self,
    ) -> list[str]:

        u = self.url

        low = u.lower()

        # ------------------------------------------------------
        # STREAM DIRETO
        # ------------------------------------------------------

        if (
            low.endswith(
                ".m3u8"
            )
            or ".m3u8?" in low
            or low.endswith(
                ".ts"
            )
        ):

            self.log(
                "URL já é stream direto: "
                f"{u[:120]}"
            )

            return [
                u
            ]

        # ------------------------------------------------------
        # YT-DLP
        # ------------------------------------------------------

        ytdlp = (
            shutil.which(
                "yt-dlp"
            )
            or shutil.which(
                "yt-dlp.exe"
            )
        )

        if not ytdlp:

            cmd_base = [

                os.sys.executable,

                "-m",

                "yt_dlp",
            ]

        else:

            cmd_base = [
                ytdlp
            ]

        self.log(
            "Resolvendo stream "
            "com yt-dlp (-g)…"
        )

        cmd = (
            cmd_base
            + [
                "-g",

                "-f",

                "bv+ba/b",

                "--no-playlist",

                "--no-warnings",

                u,
            ]
        )

        try:

            r = subprocess.run(

                cmd,

                capture_output=True,

                text=True,

                timeout=90,
            )

        except FileNotFoundError:

            raise RuntimeError(
                "yt-dlp não encontrado. "
                "pip install -r "
                "requirements.txt"
            )

        except subprocess.TimeoutExpired:

            raise RuntimeError(
                "yt-dlp timeout (90s)"
            )

        out = (
            r.stdout
            or ""
        ).strip()

        err = (
            r.stderr
            or ""
        ).strip()

        if (
            r.returncode != 0
            or not out
        ):

            raise RuntimeError(

                "yt-dlp falhou "
                f"(code={r.returncode}): "
                f"{err[:400] or out[:400]}"
            )

        lines = [

            ln.strip()

            for ln
            in out.splitlines()

            if ln.strip()
        ]

        if (
            len(lines)
            not in (
                1,
                2,
            )
            or any(
                not ln.startswith(
                    (
                        "http://",
                        "https://",
                    )
                )
                for ln
                in lines
            )
        ):

            raise RuntimeError(
                "yt-dlp não devolveu "
                "uma ou duas URLs HTTP válidas"
            )

        self.log(

            "Stream resolvido: "
            f"{len(lines)} entrada(s); "
            + (
                "vídeo=0, áudio=1"
                if len(lines) == 2
                else
                "vídeo e áudio na entrada 0"
            )
        )

        return lines

    # ==========================================================
    # COMANDO FFMPEG PRINCIPAL
    # ==========================================================

    def _ffmpeg_cmd(
        self,
        stream_urls: list[str],
    ) -> list[str]:

        if len(
            stream_urls
        ) not in (
            1,
            2,
        ):

            raise ValueError(
                "Esperadas uma ou duas "
                "entradas de mídia"
            )

        # ------------------------------------------------------
        # NOME DOS SEGMENTOS
        # ------------------------------------------------------

        if self.recovery_attempts:

            segment_name = (

                f"seg_"
                f"{self.recovery_attempts:03d}_"
                "%05d.ts"
            )

        else:

            segment_name = (
                "seg_%05d.ts"
            )

        seg_pat = str(
            self.hls_dir
            / segment_name
        )

        cmd = [

            "ffmpeg",

            "-hide_banner",

            "-loglevel",
            "warning",

            "-nostdin",
        ]

        # ------------------------------------------------------
        # ENTRADAS
        # ------------------------------------------------------

        for url in stream_urls:

            cmd += [
                "-i",
                url,
            ]

        audio_input = (

            1

            if len(
                stream_urls
            ) == 2

            else 0
        )

        # ------------------------------------------------------
        # SAÍDA HLS
        # ------------------------------------------------------

        cmd += [

            "-map",
            "0:v:0",

            "-map",
            f"{audio_input}:a:0",

            "-fflags",
            "+genpts",

            # VIDEO

            "-c:v",
            "libx264",

            "-preset",
            "veryfast",

            "-tune",
            "zerolatency",

            "-pix_fmt",
            "yuv420p",

            "-g",
            "120",

            "-keyint_min",
            "1",

            "-sc_threshold",
            "0",

            "-force_key_frames",
            (
                "expr:gte("
                "t,"
                f"n_forced*{HLS_SEG_SEC}"
                ")"
            ),

            # AUDIO

            "-c:a",
            "aac",

            "-b:a",
            "128k",

            "-ar",
            "48000",

            "-ac",
            "2",

            # HLS

            "-f",
            "hls",

            "-hls_time",
            str(
                HLS_SEG_SEC
            ),

            "-hls_list_size",
            "0",

            "-hls_playlist_type",
            "event",

            "-hls_flags",
            (
                "temp_file"
                "+independent_segments"
                "+omit_endlist"
                + ("+append_list" if self.recovery_attempts else "")
            ),

            "-start_number",
            "0",

            "-hls_segment_filename",
            seg_pat,

            str(
                self.m3u8
            ),
        ]

        return cmd

    # ==========================================================
    # START FFMPEG
    # ==========================================================

    def start_ffmpeg(
        self,
        stream_urls: list[str],
    ) -> None:

        with self._process_lock:

            if self._stop.is_set():

                raise RuntimeError(
                    "Sessão parada durante "
                    "a resolução da URL"
                )

            self.resolved_url = (
                stream_urls[0]
            )

            # --------------------------------------------------
            # FECHAR LOG ANTERIOR
            # --------------------------------------------------

            if self._ffmpeg_log:

                try:

                    self._ffmpeg_log.close()

                except Exception:

                    pass

            log_path = (

                LOGS

                / (
                    f"{self.id}"
                    "_ffmpeg.log"
                )
            )

            self._ffmpeg_log = open(

                log_path,

                "a",

                encoding="utf-8",

                errors="replace",
            )

            cmd = self._ffmpeg_cmd(
                stream_urls
            )

            self.log(
                "ffmpeg HLS → "
                f"{self.m3u8}"
            )

            self.ffmpeg = (
                subprocess.Popen(

                    cmd,

                    stdin=subprocess.DEVNULL,

                    stdout=subprocess.DEVNULL,

                    stderr=self._ffmpeg_log,
                )
            )

            self.log(
                "ffmpeg pid="
                f"{self.ffmpeg.pid}"
            )

    # ==========================================================
    # SEGMENTOS PRONTOS
    # ==========================================================

    def _ready_segments(
        self,
    ) -> list[Path]:
        """
        Retorna somente segmentos .ts finalizados.

        Como o FFmpeg usa temp_file, o arquivo .ts só aparece
        com o nome definitivo depois que terminou de ser escrito.

        Mesmo assim deixamos o segmento mais novo de fora como
        margem extra de segurança.
        """

        if not self.hls_dir.is_dir():

            return []

        segs = sorted(

            self.hls_dir.glob(
                "seg_*.ts"
            ),

            key=lambda p:
                p.stat().st_mtime,
        )

        # Precisamos de pelo menos dois.
        #
        # O segmento mais novo fica reservado como margem
        # extra para nunca disputar o arquivo com o FFmpeg.

        if len(
            segs
        ) <= 1:

            return []

        return segs[:-1]

    def _grid_source_mode(self) -> str:
        try:
            if self.player_dvr_grid_flag.is_file():
                return "dvr"
        except OSError:
            pass
        return "live"

    def _ready_grid_segments(self) -> tuple[str, list[Path]]:
        mode = self._grid_source_mode()
        if mode == "dvr":
            try:
                segs = sorted(
                    self.player_dvr_chunks_dir.glob("dvr_*.ts"),
                    key=lambda p: p.stat().st_mtime,
                )
            except OSError:
                return mode, []
            return mode, segs
        return mode, self._ready_segments()

    # ==========================================================
    # FRAME DIRETO DO .TS
    # ==========================================================

    def _grab_frame_from_segment(
        self,
        segment: Path,
        dst: Path,
    ) -> bool:

        if not segment.is_file():

            return False

        # ------------------------------------------------------
        # GARANTIR QUE O WINDOWS LIBEROU O SEGMENTO
        # ------------------------------------------------------

        try:

            size1 = (
                segment.stat().st_size
            )

            time.sleep(
                0.15
            )

            size2 = (
                segment.stat().st_size
            )

            if (
                size1 <= 0
                or size1 != size2
            ):

                return False

        except OSError:

            return False

        # ------------------------------------------------------
        # PEGAR PRIMEIRO FRAME
        # ------------------------------------------------------

        cmd = [

            "ffmpeg",

            "-hide_banner",

            "-loglevel",
            "error",

            "-nostdin",

            "-i",
            str(
                segment
            ),

            "-vf",
            "select=eq(n\\,0)",

            "-frames:v",
            "1",

            "-q:v",
            "3",

            "-y",

            str(
                dst
            ),
        ]

        try:

            r = subprocess.run(

                cmd,

                capture_output=True,

                timeout=8,
            )

            ok = (

                r.returncode == 0

                and dst.is_file()

                and dst.stat().st_size
                > 400
            )

            if not ok:

                err = (
                    r.stderr
                    or b""
                )[:250]

                self.log(
                    "frame de "
                    f"{segment.name} "
                    "falhou: "
                    f"{err!r}"
                )

            return ok

        except subprocess.TimeoutExpired:

            self.log(
                "frame de "
                f"{segment.name} "
                "timeout"
            )

            try:

                if dst.exists():

                    dst.unlink()

            except OSError:

                pass

            return False

        except Exception as e:

            self.log(
                "frame de "
                f"{segment.name} "
                f"erro: {e}"
            )

            return False

    # ==========================================================
    # GRID NxN
    # ==============================================================

    def _make_grid(
        self,
        frames: list[Path],
        out: Path,
        grid_size: int,
    ) -> bool:
        """
        Monta um grid quadrado NxN.

        grid_size:
            2 -> 2x2 -> 4 imagens
            3 -> 3x3 -> 9 imagens
            4 -> 4x4 -> 16 imagens
            5 -> 5x5 -> 25 imagens
        """

        grid_size = (
            _clamp_grid_size(
                grid_size
            )
        )

        expected = (
            grid_size
            * grid_size
        )

        if len(
            frames
        ) != expected:

            self.log(
                f"grid "
                f"{grid_size}x{grid_size}: "
                f"esperava {expected} frames, "
                f"recebeu {len(frames)}"
            )

            return False

        for p in frames:

            if not p.is_file():

                self.log(
                    "frame ausente "
                    "no grid: "
                    f"{p.name}"
                )

                return False

        # ------------------------------------------------------
        # TAMANHO DE CADA QUADRINHO
        # ------------------------------------------------------
        #
        # 320x180 mantém 16:9.
        #
        # Resultado:
        #
        # 2x2 = 640x360
        # 3x3 = 960x540
        # 4x4 = 1280x720
        # 5x5 = 1600x900
        #
        # ------------------------------------------------------

        cell_w = 320

        cell_h = 180

        cmd = [

            "ffmpeg",

            "-hide_banner",

            "-loglevel",
            "error",

            "-nostdin",
        ]

        # ------------------------------------------------------
        # ENTRADAS
        # ------------------------------------------------------

        for frame in frames:

            cmd += [
                "-i",
                str(
                    frame
                ),
            ]

        # ------------------------------------------------------
        # FILTROS DE ESCALA
        # ------------------------------------------------------

        filters: list[str] = []

        labels: list[str] = []

        for i in range(
            expected
        ):

            filters.append(

                f"[{i}:v]"

                f"scale="
                f"{cell_w}:{cell_h}:"
                "force_original_aspect_ratio="
                "decrease,"

                f"pad="
                f"{cell_w}:{cell_h}:"
                "(ow-iw)/2:"
                "(oh-ih)/2"

                f"[v{i}]"
            )

            labels.append(
                f"[v{i}]"
            )

        # ------------------------------------------------------
        # POSIÇÕES
        # ------------------------------------------------------

        layout: list[str] = []

        for i in range(
            expected
        ):

            row = (
                i
                // grid_size
            )

            col = (
                i
                % grid_size
            )

            x = (
                col
                * cell_w
            )

            y = (
                row
                * cell_h
            )

            layout.append(
                f"{x}_{y}"
            )

        # ------------------------------------------------------
        # XSTACK
        # ------------------------------------------------------

        filter_complex = (

            ";".join(
                filters
            )

            + ";"

            + "".join(
                labels
            )

            + (
                f"xstack="
                f"inputs={expected}:"
                "layout="
            )

            + "|".join(
                layout
            )

            + "[grid]"
        )

        cmd += [

            "-filter_complex",
            filter_complex,

            "-map",
            "[grid]",

            "-frames:v",
            "1",

            "-q:v",
            "3",

            "-y",

            str(
                out
            ),
        ]

        try:

            # Grids maiores podem levar um pouco mais.
            timeout = (
                20
                + expected
                * 2
            )

            r = subprocess.run(

                cmd,

                capture_output=True,

                timeout=timeout,
            )

            ok = (

                r.returncode == 0

                and out.is_file()

                and out.stat().st_size
                > 800
            )

            if not ok:

                self.log(
                    "xstack "
                    f"{grid_size}x{grid_size} "
                    "falhou: "
                    f"{(r.stderr or b'')[:400]!r}"
                )

            return ok

        except subprocess.TimeoutExpired:

            self.log(
                "xstack "
                f"{grid_size}x{grid_size} "
                "timeout"
            )

            try:

                if out.exists():

                    out.unlink()

            except OSError:

                pass

            return False

        except Exception as e:

            self.log(
                "xstack "
                f"{grid_size}x{grid_size} "
                f"erro: {e}"
            )

            return False

    # ==========================================================
    # GRID LOOP
    # ==========================================================

    def _grid_loop(
        self,
    ) -> None:

        self.log(
            "grid loop ON="
            f"{self.generate_grids} "
            "(segmentos .ts prontos; "
            f"grid="
            f"{self.grid_size}x{self.grid_size}; "
            f"lote={self.process_batch})"
        )

        # ------------------------------------------------------
        # FRAMES QUE AINDA NÃO VIRARAM GRID
        # ------------------------------------------------------

        pending_frames: list[
            Path
        ] = []

        # ------------------------------------------------------
        # TAMANHO DO GRID ATUAL
        # ------------------------------------------------------
        #
        # Se a pessoa mudar de 5x5 para 2x2 enquanto já existem
        # frames acumulados, terminamos primeiro o grid antigo.
        #
        # O tamanho novo entra no PRÓXIMO grid.
        #
        # Assim nenhum frame é jogado fora.
        # ------------------------------------------------------

        pending_grid_size = (
            self.grid_size
        )

        # ------------------------------------------------------
        # SEGMENTOS JÁ PROCESSADOS
        # ------------------------------------------------------

        processed_segments: set[
            str
        ] = set()

        source_mode = "live"

        while not self._stop.is_set():

            # --------------------------------------------------
            # SESSÃO TERMINOU
            # --------------------------------------------------

            if self.status in (
                "ended",
                "dead",
                "error",
                "stopped",
            ):

                break

            # --------------------------------------------------
            # GRIDS DESLIGADOS
            # --------------------------------------------------

            if not self.generate_grids:

                time.sleep(
                    1.0
                )

                continue

            # --------------------------------------------------
            # PEGAR SOMENTE .TS FINALIZADOS
            # --------------------------------------------------

            current_source, segments = (
                self._ready_grid_segments()
            )

            if current_source != source_mode:
                self.log(
                    f"fonte dos grids: {source_mode.upper()} → "
                    f"{current_source.upper()}"
                )
                pending_frames = []

                if current_source == "dvr":
                    processed_segments = {
                        key for key in processed_segments
                        if "player_dvr" not in key
                    }
                else:
                    # Voltando ao vivo: pula o backlog criado enquanto
                    # o usuário estava assistindo ao histórico.
                    for old_segment in segments:
                        try:
                            processed_segments.add(str(old_segment.resolve()))
                        except OSError:
                            pass

                source_mode = current_source

            if not segments:

                time.sleep(
                    0.5
                )

                continue

            # --------------------------------------------------
            # DESCOBRIR NOVOS SEGMENTOS
            # --------------------------------------------------

            new_segments: list[
                Path
            ] = []

            for segment in segments:

                try:

                    key = str(
                        segment.resolve()
                    )

                except OSError:

                    continue

                if key not in processed_segments:

                    new_segments.append(
                        segment
                    )

            if not new_segments:

                time.sleep(
                    0.5
                )

                continue

            # --------------------------------------------------
            # PROCESSAMENTO EM LOTE
            # --------------------------------------------------
            #
            # IMPORTANTE:
            #
            # process_batch NÃO altera o tamanho do grid.
            #
            # grid_size NÃO altera quantos segmentos são
            # processados por rodada.
            #
            # São controles independentes.
            # --------------------------------------------------

            current_batch = (
                _clamp_process_batch(
                    self.process_batch
                )
            )

            segments_to_process = (
                new_segments[
                    :current_batch
                ]
            )

            self.log(
                "processando lote: "
                f"{len(segments_to_process)} "
                "segmento(s) "
                f"(máximo={current_batch})"
            )

            # --------------------------------------------------
            # PROCESSAR SEGMENTOS
            # --------------------------------------------------

            for segment in (
                segments_to_process
            ):

                if self._stop.is_set():

                    break

                try:

                    key = str(
                        segment.resolve()
                    )

                except OSError:

                    continue

                if not pending_frames:
                    pending_grid_size = self.grid_size

                frame_idx = (
                    self.frames_done
                )

                dst = (

                    self.frames_dir

                    / (
                        f"f_"
                        f"{frame_idx:05d}"
                        ".jpg"
                    )
                )

                ok = (
                    self
                    ._grab_frame_from_segment(
                        segment,
                        dst,
                    )
                )

                if not ok:

                    # Não marca como processado.
                    #
                    # Assim o segmento pode ser tentado
                    # novamente na próxima rodada.

                    self.log(
                        "aguardando segmento "
                        f"{segment.name}"
                    )

                    time.sleep(
                        0.3
                    )

                    continue

                # --------------------------------------------------
                # FRAME CRIADO
                # --------------------------------------------------

                processed_segments.add(
                    key
                )

                self.frames_done += 1

                pending_frames.append(
                    dst
                )

                self.log(
                    f"frame "
                    f"#{self.frames_done} "
                    f"← {segment.name} "
                    f"→ {dst.name}"
                )

                # --------------------------------------------------
                # QUANTOS FRAMES O GRID ATUAL PRECISA?
                # --------------------------------------------------

                frames_needed = (

                    pending_grid_size

                    * pending_grid_size
                )

                # --------------------------------------------------
                # FORMAR GRID
                # --------------------------------------------------

                if len(
                    pending_frames
                ) >= frames_needed:

                    batch = (

                        pending_frames[
                            :frames_needed
                        ]
                    )



                    gidx = (
                        self.grids_done
                        + 1
                    )

                    gout = (

                        self.grids_dir

                        / (
                            f"grid_"
                            f"{gidx:05d}_"
                            f"{pending_grid_size}"
                            "x"
                            f"{pending_grid_size}"
                            ".jpg"
                        )
                    )

                    # ------------------------------------------
                    # MONTAR GRID
                    # ------------------------------------------

                    if self._make_grid(
                        batch,
                        gout,
                        pending_grid_size,
                    ):

                        self.grids_done = (
                            gidx
                        )
                        pending_frames = pending_frames[frames_needed:]

                        self.log(
                            f"GRID #{gidx} "
                            f"{pending_grid_size}"
                            "x"
                            f"{pending_grid_size} "
                            f"→ {gout.name}"
                        )

                        if (
                            gidx
                            % GRIDS_BATCH
                            == 0
                        ):

                            self.log(
                                "— checkpoint: "
                                f"{gidx} "
                                "grids gerados —"
                            )

                    else:

                        self.log(
                            f"GRID #{gidx} "
                            "FALHOU"
                        )

                    # ------------------------------------------
                    # APLICAR NOVO TAMANHO NO PRÓXIMO GRID
                    # ------------------------------------------

                    if not pending_frames:
                        pending_grid_size = self.grid_size

                # --------------------------------------------------
                # SALVAR META
                # --------------------------------------------------

                self._save_meta()

                # Pequena folga entre subprocessos.

                time.sleep(
                    0.1
                )

            # ------------------------------------------------------
            # FOLGA ENTRE LOTES
            # ------------------------------------------------------

            time.sleep(
                0.25
            )

        self.log(
            "grid loop encerrado"
        )

    # ==========================================================
    # FECHAR PLAYLIST
    # ==========================================================

    def _close_playlist(
        self,
    ) -> None:

        if not self.m3u8.is_file():

            return

        try:

            txt = (
                self.m3u8.read_text(
                    encoding="utf-8"
                )
            )

            if (
                "#EXT-X-ENDLIST"
                not in txt
            ):

                self.m3u8.write_text(

                    txt.rstrip()
                    + "\n"
                    + "#EXT-X-ENDLIST"
                    + "\n",

                    encoding="utf-8",
                )

        except Exception as e:

            self.log(
                "Não foi possível "
                "fechar playlist: "
                f"{e}"
            )

    # ==========================================================
    # FINALIZAÇÃO
    # ==========================================================

    def _finish_capture(
        self,
        status: str,
        error: str | None = None,
    ) -> None:

        with self._process_lock:

            if self._stop.is_set():

                return

            self.status = (
                status
            )

            self.error = (
                error
            )

            self._close_playlist()

            if self._ffmpeg_log:

                try:

                    self._ffmpeg_log.close()

                except Exception:

                    pass

            self._save_meta()

    # ==========================================================
    # WATCHDOG
    # ==========================================================

    def _watch_ffmpeg(
        self,
    ) -> None:

        while not self._stop.wait(
            1.0
        ):

            process = (
                self.ffmpeg
            )

            if (
                not process
                or process.poll()
                is None
            ):

                continue

            code = (
                process.returncode
            )

            self.log(
                "ffmpeg terminou "
                f"code={code}"
            )

            # --------------------------------------------------
            # FIM NORMAL
            # --------------------------------------------------

            if code == 0:

                self._finish_capture(
                    "ended"
                )

                return

            # --------------------------------------------------
            # TENTAR RECUPERAR
            # --------------------------------------------------

            while not self._stop.is_set():

                with self._process_lock:

                    if self._stop.is_set():

                        return

                    if (
                        self.recovery_attempts
                        >= MAX_RECOVERY_ATTEMPTS
                    ):

                        self._finish_capture(

                            "dead",

                            "Limite de recuperação "
                            "atingido; gravação preservada",
                        )

                        return

                    self.recovery_attempts += 1

                    self.status = (
                        "reconnecting"
                    )

                    self.error = (

                        "Reconectando "

                        f"("
                        f"{self.recovery_attempts}"
                        "/"
                        f"{MAX_RECOVERY_ATTEMPTS}"
                        ")"
                    )

                    self._save_meta()

                self.log(
                    self.error
                )

                delay = min(

                    RECOVERY_DELAY_SEC

                    * self.recovery_attempts,

                    15,
                )

                if self._stop.wait(
                    delay
                ):

                    return

                try:

                    streams = (
                        self.resolve_stream()
                    )

                    with self._process_lock:

                        if self._stop.is_set():

                            return

                        self.start_ffmpeg(
                            streams
                        )

                        self.status = (
                            "live"
                        )

                        self.error = (
                            None
                        )

                        self._save_meta()

                    break

                except Exception as exc:

                    if self._stop.is_set():

                        return

                    self.log(
                        "Recuperação falhou: "
                        f"{exc}"
                    )

                break

            # Continua monitorando o FFmpeg novo.

    # ==========================================================
    # START
    # ==========================================================

    def start(
        self,
    ) -> None:

        if not self.url:

            raise ValueError(
                "URL vazia"
            )

        if self.status in (
            "starting",
            "live",
            "reconnecting",
        ):

            return

        self.status = (
            "starting"
        )

        self.error = (
            None
        )

        self.started_at = (
            time.time()
        )

        self._stop.clear()

        self.log(

            f"START "
            f"url={self.url[:160]} "

            f"grids="
            f"{self.generate_grids} "

            f"grid="
            f"{self.grid_size}"
            "x"
            f"{self.grid_size} "

            f"process_batch="
            f"{self.process_batch}"
        )

        self._save_meta()

        try:

            streams = (
                self.resolve_stream()
            )

            if self._stop.is_set():

                raise RuntimeError(
                    "Sessão parada durante "
                    "a resolução da URL"
                )

            self.start_ffmpeg(
                streams
            )

            self.status = (
                "live"
            )

            self.error = (
                None
            )

            self._save_meta()

            # --------------------------------------------------
            # THREAD DOS GRIDS
            # --------------------------------------------------

            grid_thread = (
                threading.Thread(

                    target=self._grid_loop,

                    name=(
                        f"grid-"
                        f"{self.id}"
                    ),

                    daemon=True,
                )
            )

            grid_thread.start()

            self._threads.append(
                grid_thread
            )

            # --------------------------------------------------
            # WATCHDOG
            # --------------------------------------------------

            watch_thread = (
                threading.Thread(

                    target=self._watch_ffmpeg,

                    name=(
                        f"watch-"
                        f"{self.id}"
                    ),

                    daemon=True,
                )
            )

            watch_thread.start()

            self._threads.append(
                watch_thread
            )

            self.log(
                "sessão LIVE — player: "
                "/media/.../hls/live.m3u8"
            )

        except Exception as exc:

            self.status = (
                "error"
            )

            self.error = (
                str(exc)
            )

            self._save_meta()

            self.log(
                "START FALHOU: "
                f"{exc}"
            )

            raise

    # ==========================================================
    # STOP
    # ==========================================================

    def stop(
        self,
    ) -> None:

        if self._stop.is_set():

            return

        self.log(
            "STOP pedido"
        )

        self._stop.set()

        with self._process_lock:

            process = (
                self.ffmpeg
            )

            if (
                process
                and process.poll()
                is None
            ):

                try:

                    process.terminate()

                    process.wait(
                        timeout=8
                    )

                except subprocess.TimeoutExpired:

                    self.log(
                        "ffmpeg não encerrou "
                        "a tempo; forçando"
                    )

                    try:

                        process.kill()

                    except Exception:

                        pass

                    try:

                        process.wait(
                            timeout=3
                        )

                    except Exception:

                        pass

                except Exception as exc:

                    self.log(
                        "erro ao encerrar "
                        "ffmpeg: "
                        f"{exc}"
                    )

            self.ffmpeg = (
                None
            )

        # ------------------------------------------------------
        # ESPERAR THREADS
        # ------------------------------------------------------

        current = (
            threading.current_thread()
        )

        for thread in list(
            self._threads
        ):

            if thread is current:

                continue

            if thread.is_alive():

                try:

                    thread.join(
                        timeout=3
                    )

                except Exception:

                    pass

        self._close_playlist()

        if self._ffmpeg_log:

            try:

                self._ffmpeg_log.close()

            except Exception:

                pass

            self._ffmpeg_log = (
                None
            )

        self.status = (
            "stopped"
        )

        self.error = (
            None
        )

        self._save_meta()

        self.log(
            "sessão parada"
        )

    # ==========================================================
    # LIGAR / DESLIGAR GRIDS
    # ==========================================================

    def set_grids(
        self,
        on: bool,
    ) -> None:

        self.generate_grids = bool(
            on
        )

        self.log(

            "grids "

            + (
                "ATIVADOS"

                if self.generate_grids

                else "DESATIVADOS"
            )
        )

        self._save_meta()

    # ==========================================================
    # ALTERAR TAMANHO DO GRID
    # ==========================================================

    def set_grid_size(
        self,
        value: int,
    ) -> int:
        """
        Altera o tamanho visual dos próximos grids.

        2 -> 2x2 -> 4 imagens
        3 -> 3x3 -> 9 imagens
        4 -> 4x4 -> 16 imagens
        5 -> 5x5 -> 25 imagens

        Se um grid já estiver parcialmente acumulado,
        ele termina no tamanho anterior.

        O tamanho novo passa a valer no próximo grid.
        """

        value = (
            _clamp_grid_size(
                value
            )
        )

        old = (
            self.grid_size
        )

        self.grid_size = (
            value
        )

        if old != value:

            self.log(

                "tamanho do grid alterado: "

                f"{old}x{old}"

                " → "

                f"{value}x{value}"
            )

        self._save_meta()

        return (
            self.grid_size
        )

    # ==========================================================
    # ALTERAR LOTE DE PROCESSAMENTO
    # ==========================================================

    def set_process_batch(
        self,
        value: int,
    ) -> int:
        """
        Permite alterar o tamanho do lote durante a sessão.

        O valor é limitado entre 1 e 10.

        Isso NÃO altera o tamanho visual do grid.
        Apenas controla quantos segmentos .ts novos podem ser
        processados por rodada.
        """

        value = (
            _clamp_process_batch(
                value
            )
        )

        old = (
            self.process_batch
        )

        self.process_batch = (
            value
        )

        if old != value:

            self.log(

                "lote de processamento "
                "alterado: "

                f"{old} → {value}"
            )

        self._save_meta()

        return (
            self.process_batch
        )

    # ==========================================================
    # INFO
    # ==========================================================

    def info(
        self,
    ) -> dict:

        duration = (
            None
        )

        if self.started_at:

            duration = max(

                0,

                time.time()
                - self.started_at,
            )

        ffmpeg_alive = (
            False
        )

        process = (
            self.ffmpeg
        )

        if process:

            try:

                ffmpeg_alive = (
                    process.poll()
                    is None
                )

            except Exception:

                ffmpeg_alive = (
                    False
                )

        grid_files = sorted(
            (p for p in self.grids_dir.glob("grid_*.jpg")
             if re.fullmatch(r"grid_\d+(?:_[2-5]x[2-5])?\.jpg", p.name)),
            key=lambda p: int(p.name.split("_")[1].split(".")[0]), reverse=True,
        )[:12]
        segments = list(self.hls_dir.glob("seg_*.ts"))
        return {
            "grid_files": [p.name for p in grid_files],
            "segments": len(segments),
            "duration_approx_sec": len(segments) * HLS_SEG_SEC,
            "m3u8": f"/media/{self.id}/hls/live.m3u8",


            "id":
                self.id,

            "name":
                self.name,

            "url":
                self.url,

            "status":
                self.status,

            "error":
                self.error,

            "generate_grids":
                self.generate_grids,

            "grid_size":
                self.grid_size,

            "grid_label":
                (
                    f"{self.grid_size}"
                    "x"
                    f"{self.grid_size}"
                ),

            "frames_per_grid":
                (
                    self.grid_size
                    * self.grid_size
                ),

            "process_batch":
                self.process_batch,

            "frames_done":
                self.frames_done,

            "grids_done":
                self.grids_done,

            "started_at":
                self.started_at,

            "duration":
                duration,

            "ffmpeg_alive":
                ffmpeg_alive,

            "recovery_attempts":
                self.recovery_attempts,

            "player_url":
                (
                    f"/media/{self.id}"
                    "/hls/live.m3u8"
                ),

            "hls_url":
                (
                    f"/media/{self.id}"
                    "/hls/live.m3u8"
                ),

            "grids_url":
                (
                    f"/media/{self.id}"
                    "/grids/"
                ),

            "directory":
                str(
                    self.dir
                ),

            "hls_directory":
                str(
                    self.hls_dir
                ),

            "frames_directory":
                str(
                    self.frames_dir
                ),

            "grids_directory":
                str(
                    self.grids_dir
                ),
        }


# ==============================================================
# SESSÕES GLOBAIS
# ==============================================================

SESSIONS: dict[
    str,
    LiveSession,
] = {}


ACTIVE: LiveSession | None = None


_lock = (
    threading.RLock()
)


# ==============================================================
# START SESSION
# ==============================================================

def start_session(
    url: str,
    *,
    name: str = "teste",
    generate_grids: bool = True,
    grid_size: int = DEFAULT_GRID_SIZE,
    process_batch: int = DEFAULT_PROCESS_BATCH,
    log=None,
) -> LiveSession:
    """
    Cria e inicia uma nova sessão.

    grid_size:

        2 = 2x2 = 4 imagens
        3 = 3x3 = 9 imagens
        4 = 4x4 = 16 imagens
        5 = 5x5 = 25 imagens

    process_batch:

        Quantidade máxima de segmentos .ts novos
        processados por rodada.

        Mínimo: 1
        Máximo: 10
        Padrão: 4
    """

    global ACTIVE

    grid_size = (
        _clamp_grid_size(
            grid_size
        )
    )

    process_batch = (
        _clamp_process_batch(
            process_batch
        )
    )

    # ----------------------------------------------------------
    # TROCAR SESSÃO ATIVA
    # ----------------------------------------------------------

    with _lock:

        previous = (
            ACTIVE
        )

        if (
            previous
            and previous.status
            in (
                "live",
                "starting",
                "reconnecting",
            )
        ):

            previous.stop()

        session = (
            LiveSession(

                url,

                name=name,

                generate_grids=
                    generate_grids,

                grid_size=
                    grid_size,

                process_batch=
                    process_batch,

                log=log,
            )
        )

        SESSIONS[
            session.id
        ] = session

        ACTIVE = (
            session
        )

    # ----------------------------------------------------------
    # START FORA DO LOCK GLOBAL
    # ----------------------------------------------------------

    try:

        session.start()

    except Exception:

        # Mantemos a sessão registrada.
        #
        # Isso preserva logs/session.json para diagnóstico.

        raise

    return session


# ==============================================================
# STOP ACTIVE
# ==============================================================

def stop_active(
) -> None:
    """
    Para a sessão ativa, caso exista.
    """

    global ACTIVE

    with _lock:

        session = (
            ACTIVE
        )

    if not session:

        return

    try:

        session.stop()

    finally:

        # Não apagamos ACTIVE imediatamente.
        #
        # O dashboard ainda pode consultar info() da sessão
        # parada e a gravação continua disponível.

        pass


# ==============================================================
# GET SESSION
# ==============================================================

def get_session(
    session_id: str,
) -> LiveSession | None:
    """
    Retorna uma sessão pelo ID.
    """

    if not session_id:

        return None

    with _lock:

        return SESSIONS.get(
            str(
                session_id
            )
        )


# ==============================================================
# GET ACTIVE
# ==============================================================

def get_active(
) -> LiveSession | None:
    """
    Retorna a sessão atualmente apontada por ACTIVE.
    """

    with _lock:

        return ACTIVE
