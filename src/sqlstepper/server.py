"""Local web server: serves the UI and a small JSON API (standard library only)."""

from __future__ import annotations

import json
import mimetypes
import tempfile
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import duckdb

from .db import Session, quote_ident, sanitize_name
from .engine import Tracer, panel
from .samples import EXAMPLES

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 500 * 1024 * 1024
PREVIEW_ROWS = 200


class App:
    def __init__(self, session: Session):
        self.session = session
        self.tracer = Tracer(session)

    def state(self) -> dict:
        with self.session.lock:
            return {"tables": self.session.tables(), "examples": EXAMPLES, "db": self.session.path}

    def preview(self, name: str) -> dict:
        with self.session.lock:
            rows = self.session.query(f"SELECT * FROM {quote_ident(name)} LIMIT {PREVIEW_ROWS}")
            total = self.session.con.execute(f"SELECT COUNT(*) FROM {quote_ident(name)}").fetchone()[0]
        p = panel(name, rows)
        p["total"] = total
        return p

    def import_upload(self, filename: str, name: str | None, data: bytes) -> dict:
        suffix = Path(filename).suffix.lower()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / (sanitize_name(filename) + suffix)
            path.write_bytes(data)
            with self.session.lock:
                created = self.session.import_file(path, name or None)
        return {"created": created, **self.state()}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SQLStepper"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        # ---- helpers ------------------------------------------------------
        def send_json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def fail(self, message, status=HTTPStatus.BAD_REQUEST):
            self.send_json({"error": message}, status)

        def body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD:
                raise ValueError("檔案太大（上限 500 MB）")
            return self.rfile.read(n) if n else b""

        def json_body(self) -> dict:
            raw = self.body()
            return json.loads(raw.decode("utf-8")) if raw else {}

        # ---- routes -------------------------------------------------------------
        def do_GET(self):
            url = urlparse(self.path)
            try:
                if url.path == "/api/state":
                    return self.send_json(app.state())
                if url.path == "/api/preview":
                    name = parse_qs(url.query).get("name", [""])[0]
                    return self.send_json(app.preview(name))
                return self.static(url.path)
            except duckdb.Error as e:
                return self.fail(str(e))
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self.fail(f"{type(e).__name__}: {e}", HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self):
            url = urlparse(self.path)
            try:
                if url.path == "/api/trace":
                    req = self.json_body()
                    return self.send_json(app.tracer.run(req.get("sql", ""), sandbox=bool(req.get("sandbox", True))))
                if url.path == "/api/import":
                    q = parse_qs(url.query)
                    filename = q.get("filename", ["upload.csv"])[0]
                    name = q.get("name", [""])[0].strip()
                    return self.send_json(app.import_upload(filename, name, self.body()))
                if url.path == "/api/reset":
                    with app.session.lock:
                        app.session.reset_samples()
                    return self.send_json(app.state())
                if url.path == "/api/drop":
                    with app.session.lock:
                        app.session.drop(self.json_body().get("name", ""))
                    return self.send_json(app.state())
                return self.fail("not found", HTTPStatus.NOT_FOUND)
            except (duckdb.Error, ValueError) as e:
                return self.fail(str(e))
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                return self.fail(f"{type(e).__name__}: {e}", HTTPStatus.INTERNAL_SERVER_ERROR)

        def static(self, path: str):
            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            target = (STATIC / rel).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                return self.fail("not found", HTTPStatus.NOT_FOUND)
            data = target.read_bytes()
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def serve(session: Session, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(App(session)))
