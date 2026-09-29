"""yt-dlp --live-from-start com finalização segura de áudio/vídeo."""
from __future__ import annotations

import _thread
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from historical_download import read_state, write_state


def arguments(url, folder, output_format="mkv"):
    output_format = str(output_format or "mkv").strip().lower()

    if output_format not in ("mkv", "mp4"):
        output_format = "mkv"

    return [
        "--ignore-config",
        "--no-playlist",
        "--live-from-start",
        "--format", "bv+ba/b",
        "--merge-output-format", output_format,
        "--remux-video", output_format,
        "--keep-video",
        "--continue",
        "--no-overwrites",
        "--newline",
        "--no-colors",
        "--socket-timeout", "20",
        "--retries", "10",
        "--fragment-retries", "10",
        "--abort-on-unavailable-fragments",
        "--output", str(folder / "live.%(ext)s"),
        url,
    ]


def ffprobe_info(path):
    path = Path(path)

    if not path.is_file():
        return None

    result = subprocess.run(
        [
            shutil.which("ffprobe") or "ffprobe",
            "-v", "error",
            "-show_streams",
            "-show_format",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )

    if result.returncode:
        return None

    try:
        info = json.loads(result.stdout)
    except ValueError:
        return None

    streams = info.get("streams", []) or []

    kinds = {
        stream.get("codec_type")
        for stream in streams
    }

    duration = 0.0

    try:
        duration = float(
            info.get("format", {}).get("duration", 0)
            or 0
        )
    except (TypeError, ValueError):
        duration = 0.0

    if duration <= 0:
        durations = []

        for stream in streams:
            try:
                value = float(
                    stream.get("duration", 0)
                    or 0
                )

                if value > 0:
                    durations.append(value)

            except (TypeError, ValueError):
                pass

        if durations:
            duration = max(durations)

    return {
        "audio": "audio" in kinds,
        "video": "video" in kinds,
        "duration": duration,
    }


def validate_media(path):
    info = ffprobe_info(path)

    if not info:
        raise RuntimeError(
            f"Não foi possível analisar {Path(path).name}"
        )

    if not info["audio"] or not info["video"]:
        raise RuntimeError(
            "Arquivo final sem áudio e vídeo completos"
        )

    if info["duration"] <= 0:
        raise RuntimeError(
            "Arquivo final sem duração válida"
        )

    return info["duration"]


def find_separate_tracks(folder):
    """
    Procura a maior faixa somente de vídeo
    e a maior faixa somente de áudio.
    """

    folder = Path(folder)

    video = None
    audio = None

    for path in folder.glob("live.*"):
        if not path.is_file():
            continue

        if path.name in (
            "live.mkv",
            "live.mp4",
            "live_fixed.mkv",
            "live_fixed.mp4",
            "live_converting.mp4",
        ):
            continue

        info = ffprobe_info(path)

        if not info:
            continue

        if info["duration"] <= 0:
            continue

        if info["video"] and not info["audio"]:
            if (
                video is None
                or info["duration"] > video[0]
            ):
                video = (
                    info["duration"],
                    path,
                )

        if info["audio"] and not info["video"]:
            if (
                audio is None
                or info["duration"] > audio[0]
            ):
                audio = (
                    info["duration"],
                    path,
                )

    return video, audio


def rebuild_from_common_tracks(
    folder,
    final,
    output_format,
):
    """
    Monta o arquivo final somente até onde
    áudio e vídeo existem juntos.

    Exemplo:
      vídeo = 11 min
      áudio = 20 min

      final = 11 min
    """

    video, audio = find_separate_tracks(folder)

    if not video or not audio:
        print(
            "Faixas separadas não encontradas. "
            "Usando o arquivo final criado pelo yt-dlp.",
            flush=True,
        )

        return validate_media(final)

    video_duration, video_file = video
    audio_duration, audio_file = audio

    safe_duration = min(
        video_duration,
        audio_duration,
    )

    print(
        "",
        flush=True,
    )

    print(
        "==========================================",
        flush=True,
    )

    print(
        "DURACOES RECUPERADAS",
        flush=True,
    )

    print(
        "==========================================",
        flush=True,
    )

    print(
        f"Video recuperado: {video_duration:.1f}s",
        flush=True,
    )

    print(
        f"Audio recuperado: {audio_duration:.1f}s",
        flush=True,
    )

    print(
        f"Trecho comum seguro: {safe_duration:.1f}s",
        flush=True,
    )

    if safe_duration <= 0:
        raise RuntimeError(
            "Não há duração comum válida entre áudio e vídeo"
        )

    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"

    temp = (
        Path(folder)
        / f"live_fixed.{output_format}"
    )

    if temp.exists():
        try:
            temp.unlink()
        except OSError:
            pass

    if output_format == "mkv":
        command = [
            ffmpeg,
            "-y",

            "-i", str(video_file),
            "-i", str(audio_file),

            "-map", "0:v:0",
            "-map", "1:a:0",

            "-t", f"{safe_duration:.3f}",

            "-c", "copy",

            str(temp),
        ]

    else:
        command = [
            ffmpeg,
            "-y",

            "-i", str(video_file),
            "-i", str(audio_file),

            "-map", "0:v:0",
            "-map", "1:a:0",

            "-t", f"{safe_duration:.3f}",

            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",

            "-c:a", "aac",
            "-b:a", "160k",

            "-movflags", "+faststart",

            str(temp),
        ]

    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if process.returncode:
        raise RuntimeError(
            "Falha ao montar o arquivo pelo trecho comum: "
            + process.stderr[-2000:]
        )

    duration = validate_media(temp)

    if final.exists():
        final.unlink()

    temp.replace(final)

    print(
        f"Arquivo sincronizado: "
        f"{final.name} ({duration:.1f}s)",
        flush=True,
    )

    return duration


def run(folder):
    for stream in (
        sys.stdout,
        sys.stderr,
    ):
        if hasattr(
            stream,
            "reconfigure",
        ):
            stream.reconfigure(
                encoding="utf-8",
                errors="replace",
            )

    import yt_dlp

    folder = Path(folder).resolve()

    state = read_state(folder)

    output_format = str(
        state.get("format", "mkv")
    ).strip().lower()

    if output_format not in (
        "mkv",
        "mp4",
    ):
        output_format = "mkv"

    state["format"] = output_format

    lock = threading.RLock()

    done = threading.Event()

    interrupt_sent = threading.Event()

    last_write = [0.0]


    def save(force=False):
        with lock:
            if (
                force
                or time.monotonic() - last_write[0] >= 1
            ):
                state["heartbeat"] = time.time()

                write_state(
                    folder,
                    state,
                )

                last_write[0] = time.monotonic()


    def progress(data):
        with lock:
            info = data.get(
                "info_dict",
                {},
            )

            key = str(
                info.get("format_id")
                or Path(
                    data.get(
                        "filename",
                        "media",
                    )
                ).name
            )

            state["tracks"][key] = {
                k: data.get(k)
                for k in (
                    "status",
                    "downloaded_bytes",
                    "total_bytes",
                    "speed",
                    "eta",
                    "fragment_index",
                    "fragment_count",
                )
            }

            state["tracks"][key]["protocol"] = (
                info.get("protocol")
            )

            state["tracks"][key]["from_start"] = (
                info.get("is_from_start")
            )

            if state["status"] != "stopping":
                state["status"] = "downloading"

            save()


    def postprocess(data):
        with lock:
            state["status"] = "finalizing"
            save(True)


    def monitor():
        while not done.wait(0.5):
            with lock:
                save()

                if (
                    (folder / "STOP").exists()
                    and not interrupt_sent.is_set()
                    and state["status"]
                    in (
                        "starting",
                        "downloading",
                    )
                ):
                    state["status"] = "stopping"

                    save(True)

                    interrupt_sent.set()

                    print(
                        "Parada solicitada. "
                        "Encerrando yt-dlp e finalizando.",
                        flush=True,
                    )

                    _thread.interrupt_main()


    watcher = threading.Thread(
        target=monitor,
        daemon=True,
    )

    try:
        parsed = yt_dlp.parse_options(
            arguments(
                state["url"],
                folder,
                output_format,
            )
        )

        options = parsed.ydl_opts

        options["progress_hooks"] = [
            progress
        ]

        options["postprocessor_hooks"] = [
            postprocess
        ]

        print(
            "yt-dlp",
            yt_dlp.version.__version__,
            "--live-from-start |",
            f"formato={output_format.upper()}",
            flush=True,
        )

        save(True)

        watcher.start()

        with yt_dlp.YoutubeDL(
            options
        ) as ydl:
            code = ydl.download(
                parsed.urls
            )

        with lock:
            state["status"] = "finalizing"
            save(True)

        if code:
            raise RuntimeError(
                f"yt-dlp retornou erro {code}; "
                "consulte o log"
            )

        final = (
            folder
            / f"live.{output_format}"
        )

        duration = rebuild_from_common_tracks(
            folder,
            final,
            output_format,
        )

        duration = validate_media(
            final
        )

        video, audio = find_separate_tracks(
            folder
        )

        if video:
            state["video_duration"] = (
                video[0]
            )

        if audio:
            state["audio_duration"] = (
                audio[0]
            )

        if video and audio:
            state["safe_duration"] = min(
                video[0],
                audio[0],
            )

        with lock:
            state.update(
                status=(
                    "stopped"
                    if interrupt_sent.is_set()
                    else "completed"
                ),

                file=final.name,

                duration=duration,

                partial=(
                    interrupt_sent.is_set()
                ),

                format=output_format,

                error=None,
            )

            save(True)

        print(
            f"Arquivo validado com áudio e vídeo: "
            f"{final.name} ({duration:.1f}s)",
            flush=True,
        )

    except KeyboardInterrupt:
        with lock:
            state.update(
                status="stopped",
                error=(
                    "A interrupção chegou antes da "
                    "finalização. Parciais preservados."
                ),
            )

    except BaseException as exc:
        with lock:
            state.update(
                status="error",
                error=str(exc),
            )

        print(
            "ERRO:",
            exc,
            flush=True,
        )

    finally:
        done.set()

        if watcher.is_alive():
            watcher.join(
                timeout=2
            )

        save(True)

    return (
        1
        if state["status"] == "error"
        else 0
    )


if __name__ == "__main__":
    sys.exit(
        run(
            sys.argv[1]
        )
    )