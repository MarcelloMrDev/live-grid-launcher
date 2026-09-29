"""Independent, disk-backed live-from-start jobs. No imports from the DVR engine."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "historical_downloads"
ACTIVE_STATES = {"starting", "downloading", "stopping", "finalizing"}
ALLOWED_FORMATS = {"mkv", "mp4"}


def read_state(folder):
    try:
        return json.loads(
            (folder / "status.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}


def write_state(folder, state):
    temp = folder / "status.tmp"

    temp.write_text(
        json.dumps(state, ensure_ascii=False),
        encoding="utf-8"
    )

    os.replace(
        temp,
        folder / "status.json"
    )


def folder_for(job_id):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Download inválido")

    folder = STORE / job_id

    if not folder.is_dir():
        raise ValueError("Download não encontrado")

    return folder


class HistoricalDownloads:

    def __init__(self):
        self.lock = threading.RLock()
        self.process = None
        self.current = None


    def start(self, url, output_format="mkv"):

        # -----------------------------
        # valida URL
        # -----------------------------

        if (
            not isinstance(url, str)
            or urlparse(url).scheme not in ("http", "https")
            or not urlparse(url).hostname
        ):
            raise ValueError(
                "Informe uma URL HTTP/HTTPS da live"
            )


        # -----------------------------
        # valida formato
        # -----------------------------

        output_format = str(
            output_format or "mkv"
        ).strip().lower()

        if output_format not in ALLOWED_FORMATS:
            raise ValueError(
                "Formato inválido. Escolha MKV ou MP4."
            )


        # -----------------------------
        # dependências
        # -----------------------------

        if importlib.util.find_spec("yt_dlp") is None:
            raise ValueError(
                "Instale as dependências: "
                "python -m pip install -r requirements.txt"
            )

        if not all(
            shutil.which(x)
            for x in ("ffmpeg", "ffprobe")
        ):
            raise ValueError(
                "FFmpeg e ffprobe precisam estar no PATH"
            )


        # -----------------------------
        # cria download
        # -----------------------------

        with self.lock:

            if (
                self.process
                and self.process.poll() is None
            ):
                raise ValueError(
                    "Já há um download histórico ativo; "
                    "pare e aguarde a finalização"
                )


            # Uma worker antiga pode ainda estar
            # finalizando depois de reiniciar o dashboard.

            if any(
                s.get("status") in ACTIVE_STATES
                for s in self.list_jobs()
            ):
                raise ValueError(
                    "Há um download em andamento ou finalizando"
                )


            job_id = uuid.uuid4().hex

            folder = STORE / job_id

            folder.mkdir(parents=True)


            state = {

                "id": job_id,

                "url": url,

                # NOVO:
                # formato escolhido pelo usuário
                "format": output_format,

                "status": "starting",

                "created": datetime.now(
                    timezone.utc
                ).isoformat(),

                "tracks": {},

                "file": None,

                "error": None,

                "heartbeat": time.time()
            }


            write_state(
                folder,
                state
            )


            try:

                with (
                    folder / "download.log"
                ).open(
                    "ab",
                    buffering=0
                ) as log:

                    self.process = subprocess.Popen(

                        [
                            sys.executable,
                            "-u",
                            str(
                                ROOT
                                / "historical_worker.py"
                            ),
                            str(folder)
                        ],

                        cwd=ROOT,

                        stdin=subprocess.DEVNULL,

                        stdout=log,

                        stderr=subprocess.STDOUT,

                        creationflags=(
                            subprocess.CREATE_NO_WINDOW
                            if os.name == "nt"
                            else 0
                        )
                    )


                self.current = job_id


            except Exception as exc:

                state.update(
                    status="error",
                    error=str(exc)
                )

                write_state(
                    folder,
                    state
                )

                raise


            return state


    def stop(self, job_id):

        with self.lock:

            folder = folder_for(
                job_id
            )

            state = read_state(
                folder
            )


            if state.get("status") in ACTIVE_STATES:

                (
                    folder
                    / "STOP"
                ).touch()


            return state


    def list_jobs(self):

        rows = []


        with self.lock:

            folders = (
                sorted(
                    STORE.iterdir(),
                    reverse=True
                )
                if STORE.exists()
                else []
            )


            for folder in folders:

                if not folder.is_dir():
                    continue


                state = read_state(
                    folder
                )


                if not state:
                    continue


                # Compatibilidade com downloads
                # criados antes da opção MP4.

                if state.get("format") not in ALLOWED_FORMATS:

                    state["format"] = "mkv"


                if state.get("status") in ACTIVE_STATES:


                    if (
                        folder.name == self.current
                        and self.process
                        and self.process.poll() is not None
                    ):

                        state.update(
                            status="error",
                            error=(
                                "Processo encerrado; "
                                "arquivos parciais preservados"
                            )
                        )

                        write_state(
                            folder,
                            state
                        )


                    elif (
                        state.get("heartbeat")
                        and
                        time.time()
                        - state["heartbeat"]
                        > 120
                    ):

                        state.update(
                            status="interrupted",
                            error=(
                                "Processo sem resposta; "
                                "arquivos preservados. "
                                "Confira o log."
                            )
                        )


                    elif (
                        (
                            folder
                            / "STOP"
                        ).exists()
                        and
                        state["status"]
                        != "finalizing"
                    ):

                        state["status"] = "stopping"


                # -----------------------------
                # logs
                # -----------------------------

                try:

                    with (
                        folder
                        / "download.log"
                    ).open("rb") as f:

                        f.seek(
                            0,
                            2
                        )

                        f.seek(
                            max(
                                0,
                                f.tell()
                                - 24000
                            )
                        )

                        state["logs"] = (
                            f.read()
                            .decode(
                                "utf-8",
                                "replace"
                            )
                            .splitlines()[-100:]
                        )


                except OSError:

                    state["logs"] = []


                # -----------------------------
                # espaço usado
                # -----------------------------

                state["bytes_on_disk"] = sum(

                    p.stat().st_size

                    for p in folder.glob(
                        "live.*"
                    )

                    if p.is_file()
                )


                rows.append(
                    state
                )


        return sorted(
            rows,
            key=lambda s: s.get(
                "created",
                ""
            ),
            reverse=True
        )


    def shutdown(self):

        for state in self.list_jobs():

            if state.get("status") in ACTIVE_STATES:

                self.stop(
                    state["id"]
                )


DOWNLOADS = HistoricalDownloads()