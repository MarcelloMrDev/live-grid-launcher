#!/usr/bin/env python3
"""
Teste Transmissao — dashboard local + logs no terminal.

  python run.py
  → http://127.0.0.1:8877

Cola YouTube / M3U8 / Sports / qualquer URL que o yt-dlp resolva.
"""

from __future__ import annotations

import json
import mimetypes
import sys
import traceback
import shutil
import re
import recording_exports

from historical_download import DOWNLOADS, folder_for, read_state
from youtube_player_dvr import PLAYER_DVR

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from live_engine import (  # noqa: E402
    ACTIVE,
    DATA,
    SESSIONS,
    start_session,
    stop_active,
)


HOST = "127.0.0.1"
PORT = 8877
DASH = ROOT / "dashboard"


def term(msg: str) -> None:
    print(msg, flush=True)


DISK_WARNING_BYTES = 10_000_000_000
DISK_CRITICAL_BYTES = 2_000_000_000


# ==============================================================
# DISCO
# ==============================================================

def disk_status() -> dict:
    """
    Consulta o volume que contém data,
    sem percorrer as gravações.
    """

    target = DATA.resolve()

    try:
        existing = target

        while (
            not existing.exists()
            and existing != existing.parent
        ):
            existing = existing.parent

        usage = shutil.disk_usage(existing)

        if usage.total <= 0:
            raise OSError(
                "Capacidade do disco indisponível"
            )

        if usage.free < DISK_CRITICAL_BYTES:
            level = "critical"

        elif usage.free < DISK_WARNING_BYTES:
            level = "warning"

        else:
            level = "ok"

        return {
            "level": level,
            "path": str(target),
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_percent": round(
                100
                * usage.free
                / usage.total,
                1,
            ),
        }

    except OSError:

        return {
            "level": "unknown",
            "path": str(target),
            "error":
                "Não foi possível consultar "
                "o espaço em disco.",
        }


# ==============================================================
# HTTP
# ==============================================================

class Handler(BaseHTTPRequestHandler):

    def log_message(
        self,
        fmt,
        *args,
    ):
        term(
            f"  http {self.address_string()} "
            f"{args[0] if args else fmt}"
        )

    # ----------------------------------------------------------
    # CORS
    # ----------------------------------------------------------

    def _cors(self):

        self.send_header(
            "Access-Control-Allow-Origin",
            "*",
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, OPTIONS",
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type",
        )

    # ----------------------------------------------------------
    # JSON
    # ----------------------------------------------------------

    def _json(
        self,
        code: int,
        obj: dict,
    ):

        body = json.dumps(
            obj,
            ensure_ascii=False,
        ).encode("utf-8")

        self.send_response(code)

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8",
        )

        self.send_header(
            "Content-Length",
            str(len(body)),
        )

        self._cors()

        self.end_headers()

        self.wfile.write(body)

    # ----------------------------------------------------------
    # ARQUIVO
    # ----------------------------------------------------------

    def _file(
        self,
        path: Path,
        *,
        cache=False,
    ):

        if not path.is_file():
            self.send_error(404)
            return

        data = path.read_bytes()

        ctype, _ = mimetypes.guess_type(
            str(path)
        )

        if path.suffix == ".m3u8":

            ctype = (
                "application/"
                "vnd.apple.mpegurl"
            )

        elif path.suffix == ".ts":

            ctype = "video/mp2t"

        self.send_response(200)

        self.send_header(
            "Content-Type",
            ctype
            or "application/octet-stream",
        )

        self.send_header(
            "Content-Length",
            str(len(data)),
        )

        if path.suffix == ".m3u8":

            self.send_header(
                "Cache-Control",
                "no-cache, no-store",
            )

        elif cache:

            self.send_header(
                "Cache-Control",
                "public, max-age=30",
            )

        self._cors()

        self.end_headers()

        self.wfile.write(data)

    # ----------------------------------------------------------
    # OPTIONS
    # ----------------------------------------------------------

    def do_OPTIONS(self):

        self.send_response(204)

        self._cors()

        self.end_headers()

    # ==========================================================
    # GET
    # ==========================================================

    def do_GET(self):

        u = urlparse(self.path)

        path = u.path

        # ------------------------------------------------------
        # DOWNLOAD HISTÓRICO - STATUS
        # ------------------------------------------------------

        if path == "/api/history/status":

            return self._json(
                200,
                {
                    "ok": True,
                    "jobs": DOWNLOADS.list_jobs(),
                },
            )

        # ------------------------------------------------------
        # DOWNLOAD HISTÓRICO - ARQUIVO FINAL
        # SUPORTA MKV E MP4
        # ------------------------------------------------------

        if path.startswith("/history-download/"):

            try:

                folder = folder_for(
                    path[len("/history-download/"):]
                )

                state = read_state(folder)

                if state.get("status") not in (
                    "completed",
                    "stopped",
                ):
                    raise ValueError(
                        "Arquivo ainda não finalizado"
                    )

                filename = str(
                    state.get("file")
                    or ""
                )

                if filename not in (
                    "live.mkv",
                    "live.mp4",
                ):
                    raise ValueError(
                        "Arquivo final inválido"
                    )

                fp = folder / filename

                if not fp.is_file():
                    raise ValueError(
                        "Arquivo final não encontrado"
                    )

                source = fp.open("rb")

            except (ValueError, OSError):

                return self.send_error(404)

            with source:

                self.send_response(200)

                if fp.suffix.lower() == ".mp4":

                    content_type = "video/mp4"

                else:

                    content_type = (
                        "video/x-matroska"
                    )

                self.send_header(
                    "Content-Type",
                    content_type,
                )

                self.send_header(
                    "Content-Length",
                    str(fp.stat().st_size),
                )

                self.send_header(
                    "Content-Disposition",
                    (
                        'attachment; filename="'
                        f'live_{folder.name}'
                        f'{fp.suffix.lower()}"'
                    ),
                )

                self.end_headers()

                try:

                    shutil.copyfileobj(
                        source,
                        self.wfile,
                        length=1024 * 1024,
                    )

                except (
                    BrokenPipeError,
                    ConnectionResetError,
                    ConnectionAbortedError,
                ):
                    pass

            return

        # ------------------------------------------------------
        # DASHBOARD
        # ------------------------------------------------------

        if path in (
            "/",
            "/index.html",
        ):

            return self._file(
                DASH / "index.html"
            )

        # ------------------------------------------------------
        # STATIC
        # ------------------------------------------------------

        if path.startswith("/static/"):

            return self._file(
                DASH
                / path[len("/static/"):],
                cache=True,
            )

        # ------------------------------------------------------
        # STATUS
        # ------------------------------------------------------

        if path == "/api/status":

            from live_engine import ACTIVE as A

            if A:

                return self._json(
                    200,
                    {
                        "ok": True,
                        "session": A.info(),
                        "disk": disk_status(),
                    },
                )

            return self._json(
                200,
                {
                    "ok": True,
                    "session": None,
                    "disk": disk_status(),
                },
            )

        # ------------------------------------------------------
        # PLAYER DVR STATUS
        # ------------------------------------------------------

        if path == "/api/player-dvr/status":

            return self._json(
                200,
                {
                    "ok": True,
                    "dvr": PLAYER_DVR.status(),
                },
            )

        # ------------------------------------------------------
        # RECORDINGS
        # ------------------------------------------------------

        if path == "/api/recordings":

            return self._json(
                200,
                {
                    "ok": True,
                    "recordings":
                        recording_exports
                        .list_recordings(),
                },
            )

        # ------------------------------------------------------
        # DOWNLOAD MP4
        # ------------------------------------------------------

        if path.startswith("/download/"):

            parts = path.split("/")

            try:

                if (
                    len(parts) != 4
                    or not re.fullmatch(
                        r"gravacao_"
                        r"[a-f0-9]{12}\.mp4",
                        parts[3],
                    )
                ):
                    raise ValueError(
                        "Arquivo inválido"
                    )

                folder = (
                    recording_exports
                    .recording_dir(
                        parts[2]
                    )
                )

                fp = (
                    folder
                    / "exports"
                    / parts[3]
                )

                if not fp.is_file():
                    raise ValueError(
                        "MP4 não encontrado"
                    )

            except ValueError:

                self.send_error(404)

                return

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "video/mp4",
            )

            self.send_header(
                "Content-Length",
                str(fp.stat().st_size),
            )

            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{fp.name}"',
            )

            self.end_headers()

            with fp.open("rb") as source:

                shutil.copyfileobj(
                    source,
                    self.wfile,
                    length=1024 * 1024,
                )

            return

        # ------------------------------------------------------
        # SESSIONS
        # ------------------------------------------------------

        if path == "/api/sessions":

            return self._json(
                200,
                {
                    "ok": True,
                    "sessions": [
                        s.info()
                        for s
                        in SESSIONS.values()
                    ],
                },
            )

        # ------------------------------------------------------
        # MEDIA
        # ------------------------------------------------------

        if path.startswith("/media/"):

            rel = path[len("/media/"):]

            fp = (
                DATA
                / rel
            ).resolve()

            if not str(fp).startswith(
                str(DATA.resolve())
            ):

                self.send_error(403)

                return

            return self._file(fp)

        # ------------------------------------------------------
        # LOGS
        # ------------------------------------------------------

        if path == "/api/logs":

            from live_engine import ACTIVE as A

            if not A:

                return self._json(
                    200,
                    {
                        "ok": True,
                        "lines": [],
                    },
                )

            logf = (
                ROOT
                / "logs"
                / f"{A.id}.log"
            )

            lines = []

            if logf.is_file():

                lines = (
                    logf.read_text(
                        encoding="utf-8",
                        errors="replace",
                    )
                    .splitlines()[-200:]
                )

            return self._json(
                200,
                {
                    "ok": True,
                    "lines": lines,
                },
            )

        # ------------------------------------------------------

        self.send_error(404)

    # ==========================================================
    # POST
    # ==========================================================

    def do_POST(self):

        u = urlparse(self.path)

        length = int(
            self.headers.get(
                "Content-Length"
            )
            or 0
        )

        raw = (
            self.rfile.read(length)
            if length
            else b"{}"
        )

        try:

            body = json.loads(
                raw.decode("utf-8")
                or "{}"
            )

        except Exception:

            body = {}

        # ======================================================
        # DOWNLOAD HISTÓRICO - START / STOP
        # ======================================================

        if u.path in (
            "/api/history/start",
            "/api/history/stop",
        ):

            try:

                if not isinstance(
                    body,
                    dict,
                ):
                    raise ValueError(
                        "Pedido inválido"
                    )

                if u.path.endswith("/start"):

                    state = DOWNLOADS.start(
                        body.get("url", ""),
                        body.get(
                            "format",
                            "mkv",
                        ),
                    )

                else:

                    state = DOWNLOADS.stop(
                        str(
                            body.get(
                                "id",
                                "",
                            )
                        )
                    )

                return self._json(
                    202,
                    {
                        "ok": True,
                        "job": state,
                    },
                )

            except (
                ValueError,
                OSError,
            ) as exc:

                return self._json(
                    400,
                    {
                        "ok": False,
                        "error": str(exc),
                    },
                )

        # ======================================================
        # START
        # ======================================================

        if u.path == "/api/start":

            url = (
                body.get("url")
                or ""
            ).strip()

            name = (
                body.get("name")
                or "teste"
            ).strip() or "teste"

            grids = body.get(
                "generate_grids",
                True,
            )

            from live_engine import _clamp_grid_size

            try:

                grid_size = _clamp_grid_size(
                    body.get(
                        "grid_size",
                        2,
                    )
                )

            except ValueError as exc:

                return self._json(
                    400,
                    {
                        "ok": False,
                        "error": str(exc),
                    },
                )

            # recebe o tamanho do lote
            # enviado pelo dashboard.

            process_batch = body.get(
                "process_batch",
                4,
            )

            try:

                process_batch = int(
                    process_batch
                )

            except (
                TypeError,
                ValueError,
            ):

                process_batch = 4

            process_batch = max(
                1,
                min(
                    10,
                    process_batch,
                ),
            )

            if not url:

                return self._json(
                    400,
                    {
                        "ok": False,
                        "error": "url vazia",
                    },
                )

            term(
                "\n>>> START pedido: "
                f"{url[:120]}  "
                f"grids={grids}  "
                f"grid={grid_size}x{grid_size} "
                f"lote={process_batch}"
            )

            try:

                s = start_session(
                    url,
                    name=name,
                    generate_grids=bool(
                        grids
                    ),
                    grid_size=grid_size,
                    process_batch=
                        process_batch,
                    log=term,
                )

                return self._json(
                    200,
                    {
                        "ok": True,
                        "session": s.info(),
                    },
                )

            except Exception as e:

                term(
                    f">>> START FALHOU: {e}"
                )

                traceback.print_exc()

                return self._json(
                    500,
                    {
                        "ok": False,
                        "error": str(e),
                    },
                )

        # ======================================================
        # ALTERAR GRID DURANTE A LIVE
        # ======================================================

        if u.path == "/api/grid-size":

            from live_engine import (
                ACTIVE as A,
                _clamp_grid_size,
            )

            if (
                not A
                or A.status not in (
                    "starting",
                    "live",
                    "reconnecting",
                )
            ):

                return self._json(
                    400,
                    {
                        "ok": False,
                        "error":
                            "nenhuma sessão ativa",
                    },
                )

            try:

                value = _clamp_grid_size(
                    body.get("grid_size")
                )

                A.set_grid_size(value)

            except ValueError as exc:

                return self._json(
                    400,
                    {
                        "ok": False,
                        "error": str(exc),
                    },
                )

            return self._json(
                200,
                {
                    "ok": True,
                    "grid_size": value,
                    "session": A.info(),
                    "disk": disk_status(),
                },
            )

        # ======================================================
        # ALTERAR LOTE DURANTE A LIVE
        # ======================================================

        if u.path == "/api/process-batch":

            from live_engine import ACTIVE as A

            if not A:

                return self._json(
                    400,
                    {
                        "ok": False,
                        "error":
                            "nenhuma sessão",
                    },
                )

            value = body.get(
                "process_batch",
                4,
            )

            try:

                value = int(value)

            except (
                TypeError,
                ValueError,
            ):

                return self._json(
                    400,
                    {
                        "ok": False,
                        "error":
                            "lote inválido",
                    },
                )

            if not 1 <= value <= 10:

                return self._json(
                    400,
                    {
                        "ok": False,
                        "error":
                            "o lote deve ficar "
                            "entre 1 e 10",
                    },
                )

            value = (
                A.set_process_batch(
                    value
                )
            )

            term(
                "\n>>> LOTE alterado: "
                f"{value}"
            )

            return self._json(
                200,
                {
                    "ok": True,
                    "process_batch":
                        value,
                    "session":
                        A.info(),
                    "disk":
                        disk_status(),
                },
            )

        # ======================================================
        # PLAYER DVR - INÍCIO DA LIVE
        # ======================================================

        if u.path == "/api/player-dvr/start":

            from live_engine import ACTIVE as A

            if (
                not A
                or A.status not in (
                    "starting",
                    "live",
                    "reconnecting",
                )
            ):

                return self._json(
                    400,
                    {
                        "ok": False,
                        "error":
                            "nenhuma live ativa",
                    },
                )

            try:

                state = PLAYER_DVR.start(
                    A.id,
                    A.url,
                    A.dir,
                    log=A.log,
                )

                return self._json(
                    202,
                    {
                        "ok": True,
                        "dvr": state,
                    },
                )

            except Exception as exc:

                return self._json(
                    400,
                    {
                        "ok": False,
                        "error": str(exc),
                    },
                )

        if u.path == (
            "/api/player-dvr/activate-grids"
        ):

            try:

                state = (
                    PLAYER_DVR
                    .activate_grids()
                )

                return self._json(
                    200,
                    {
                        "ok": True,
                        "dvr": state,
                    },
                )

            except Exception as exc:

                return self._json(
                    400,
                    {
                        "ok": False,
                        "error": str(exc),
                    },
                )

        if u.path == "/api/player-dvr/stop":

            return self._json(
                200,
                {
                    "ok": True,
                    "dvr":
                        PLAYER_DVR.stop(),
                },
            )

        # ======================================================
        # EXPORT
        # ======================================================

        if u.path == "/api/export":

            try:

                job = (
                    recording_exports
                    .start_export(
                        str(
                            body.get(
                                "session_id"
                            )
                            or ""
                        )
                    )
                )

                return self._json(
                    202,
                    {
                        "ok": True,
                        "export": job,
                    },
                )

            except (
                ValueError,
                OSError,
            ) as exc:

                return self._json(
                    400,
                    {
                        "ok": False,
                        "error": str(exc),
                    },
                )

        # ======================================================
        # STOP
        # ======================================================

        if u.path == "/api/stop":

            term("\n>>> STOP")

            PLAYER_DVR.stop()

            stop_active()

            return self._json(
                200,
                {
                    "ok": True,
                },
            )

        # ======================================================
        # GRIDS ON/OFF
        # ======================================================

        if u.path == "/api/grids":

            from live_engine import ACTIVE as A

            on = bool(
                body.get(
                    "generate_grids",
                    True,
                )
            )

            if not A:

                return self._json(
                    400,
                    {
                        "ok": False,
                        "error":
                            "nenhuma sessão",
                    },
                )

            A.set_grids(on)

            return self._json(
                200,
                {
                    "ok": True,
                    "session":
                        A.info(),
                    "disk":
                        disk_status(),
                },
            )

        # ------------------------------------------------------

        self.send_error(404)


# ==============================================================
# MAIN
# ==============================================================

def main():

    DASH.mkdir(
        parents=True,
        exist_ok=True,
    )

    (ROOT / "data").mkdir(
        exist_ok=True
    )

    (ROOT / "logs").mkdir(
        exist_ok=True
    )

    term("=" * 60)

    term(
        "  TESTE TRANSMISSAO"
    )

    term(
        f"  Dashboard: "
        f"http://{HOST}:{PORT}"
    )

    term(
        "  Cole YouTube / M3U8 / "
        "Sports no browser."
    )

    term(
        "  Logs desta sessão "
        "saem AQUI no terminal."
    )

    term(
        "  Grids: processamento "
        "em lote configurável 1–10."
    )

    term("=" * 60)

    httpd = ThreadingHTTPServer(
        (HOST, PORT),
        Handler,
    )

    try:

        httpd.serve_forever()

    except KeyboardInterrupt:

        term(
            "\nCtrl+C — encerrando…"
        )

        PLAYER_DVR.stop()

        stop_active()

        httpd.shutdown()

    finally:

        DOWNLOADS.shutdown()

        httpd.server_close()


if __name__ == "__main__":
    main()