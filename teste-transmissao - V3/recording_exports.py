"""Exporta somente segmentos publicados; não modifica HLS nem captura."""
from __future__ import annotations
import json
import re
import subprocess
import threading
import uuid
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def recording_dir(session_id: str) -> Path:
    if not re.fullmatch(r"[\w-]+", session_id):
        raise ValueError("Sessão inválida")
    folder = (DATA / session_id).resolve()
    if not folder.is_relative_to(DATA.resolve()) or not folder.is_dir():
        raise ValueError("Gravação não encontrada")
    return folder


def list_recordings() -> list[dict]:
    rows = []
    for folder in sorted(DATA.iterdir(), reverse=True) if DATA.is_dir() else []:
        if not folder.is_dir() or not (folder / "hls/live.m3u8").is_file():
            continue
        with _lock:
            job = dict(_jobs.get(folder.name, {}))
        rows.append({"id": folder.name, "export": job,
                     "files": [p.name for p in sorted((folder / "exports").glob("*.mp4"))
                               if not p.name.endswith(".partial.mp4")]})
    return rows


def snapshot_segments(folder: Path) -> list[tuple[Path, float]]:
    # FFmpeg publica a playlist por rename; só lê arquivos já finalizados.
    lines = (folder / "hls/live.m3u8").read_text(encoding="utf-8").splitlines()
    entries = []
    duration = None
    for line in lines:
        if line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line and not line.startswith("#"):
            if duration is None or duration <= 0 or not re.fullmatch(r"seg_(?:\d+_)?\d+\.ts", line):
                raise ValueError("Playlist contém segmento inválido")
            segment = (folder / "hls" / line).resolve()
            if not segment.is_relative_to((folder / "hls").resolve()) or not segment.is_file():
                raise ValueError("Segmento ainda indisponível; tente novamente")
            entries.append((segment, duration))
            duration = None
    if not entries:
        raise ValueError("Ainda não há segmentos concluídos para exportar")
    return entries


def start_export(session_id: str) -> dict:
    folder = recording_dir(session_id)
    with _lock:
        current = _jobs.get(session_id)
        if current and current["status"] == "running":
            return dict(current)
        segments = snapshot_segments(folder)
        job = {"status": "running", "error": None, "file": None,
               "segments": len(segments), "duration_sec": sum(d for _, d in segments)}
        _jobs[session_id] = job
        threading.Thread(target=_export, args=(folder, segments, job), daemon=True).start()
        return dict(job)


def _export(folder: Path, segments: list[tuple[Path, float]], job: dict) -> None:
    export_dir = folder / "exports"
    token = uuid.uuid4().hex[:12]
    playlist = export_dir / f"{token}.ffconcat"
    partial = export_dir / f"gravacao_{token}.partial.mp4"
    output = export_dir / f"gravacao_{token}.mp4"
    try:
        export_dir.mkdir(exist_ok=True)
        # concat normaliza timestamps por segmento, inclusive após reconexões.
        lines = ["ffconcat version 1.0"]
        for segment, duration in segments:
            escaped = segment.as_posix().replace("'", "'\\''")
            lines += [f"file '{escaped}'", f"duration {duration:.6f}"]
        playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with open(export_dir / f"{token}.log", "w", encoding="utf-8") as log:
            result = subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-n",
                "-f", "concat", "-safe", "0", "-i", str(playlist),
                "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(partial),
            ], stdout=subprocess.DEVNULL, stderr=log)
        if result.returncode or not partial.is_file() or partial.stat().st_size == 0:
            raise RuntimeError(f"Exportação falhou. Consulte exports/{token}.log")
        # Só disponibiliza para download depois que o MP4 estiver concluído.
        partial.rename(output)
        with _lock:
            job.update(status="ready", file=output.name)
    except Exception as exc:
        with _lock:
            job.update(status="error", error=str(exc))
    finally:
        playlist.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)
