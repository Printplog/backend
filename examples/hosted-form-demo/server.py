#!/usr/bin/env python3
"""Tiny customer-side server for the SharpToolz hosted-form example.

It intentionally uses only Python's standard library. The SharpToolz API key
stays in this process and is never returned to the browser.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/styles.css": "styles.css",
    "/app.js": "app.js",
}
MAX_BODY_BYTES = 32_768


class DemoServer(ThreadingHTTPServer):
    api_key: str
    api_base: str
    frontend_url: str
    public_origin: str
    result_receipts: dict[str, dict]
    receipt_lock: threading.Lock


class DemoHandler(BaseHTTPRequestHandler):
    server: DemoServer

    def log_message(self, format, *args):
        # Never include request headers, which contain the server-side key.
        super().log_message(format, *args)

    def security_headers(self):
        frontend_origin = self.server.frontend_url
        api_origin = f"{urlsplit(self.server.api_base).scheme}://{urlsplit(self.server.api_base).netloc}"
        self.send_header("Content-Security-Policy", "; ".join((
            "default-src 'self'",
            f"script-src 'self' {frontend_origin}",
            "style-src 'self'",
            f"img-src 'self' data: blob: {api_origin}",
            f"frame-src {frontend_origin}",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        )))
        self.send_header("Referrer-Policy", "strict-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")

    def send_bytes(self, status, body, content_type, *, cache_control="no-store"):
        self.send_response(status)
        self.security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8")

    def proxy_api(self, method, path, payload=None, extra_headers=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.server.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        headers.update(extra_headers or {})
        request = Request(
            f"{self.server.api_base}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urlopen(request, timeout=20) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()
        except URLError:
            return 502, json.dumps({"detail": "The demo backend could not reach SharpToolz."}).encode("utf-8")

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/demo-config.js":
            config = json.dumps({"frontendUrl": self.server.frontend_url})
            self.send_bytes(200, f"window.__PAPERPILOT_DEMO__={config};".encode("utf-8"), "application/javascript; charset=utf-8")
            return
        if path == "/api/templates":
            status, body = self.proxy_api("GET", "/templates")
            self.send_bytes(status, body, "application/json; charset=utf-8")
            return
        filename = STATIC_FILES.get(path)
        if filename:
            body = (ROOT / filename).read_bytes()
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            self.send_bytes(200, body, f"{content_type}; charset=utf-8", cache_control="no-cache")
            return
        self.send_json(404, {"detail": "Not found."})

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"detail": "Invalid content length."})
            return None
        if length < 1 or length > MAX_BODY_BYTES:
            self.send_json(413, {"detail": "Invalid request size."})
            return None
        try:
            incoming = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {"detail": "A JSON object is required."})
            return None
        if not isinstance(incoming, dict):
            self.send_json(400, {"detail": "A JSON object is required."})
            return None
        return incoming

    def get_receipt(self, token):
        if not isinstance(token, str) or len(token) > 100:
            return None
        now = time.time()
        with self.server.receipt_lock:
            expired = [key for key, value in self.server.result_receipts.items() if value["expires_at"] <= now]
            for key in expired:
                self.server.result_receipts.pop(key, None)
            return self.server.result_receipts.get(token)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path not in {"/api/session", "/api/render", "/api/render-status"}:
            self.send_json(404, {"detail": "Not found."})
            return
        incoming = self.read_json_body()
        if incoming is None:
            return

        if path == "/api/render":
            self.create_result_render(incoming)
            return
        if path == "/api/render-status":
            self.poll_result_render(incoming)
            return

        template_id = incoming.get("templateId") if isinstance(incoming, dict) else None
        external_user_id = incoming.get("externalUserId") if isinstance(incoming, dict) else None
        preview_mode = incoming.get("previewMode", "standard") if isinstance(incoming, dict) else "standard"
        if not isinstance(template_id, str) or len(template_id) > 50:
            self.send_json(400, {"detail": "Choose a valid template."})
            return
        if not isinstance(external_user_id, str) or not external_user_id.strip() or len(external_user_id) > 255:
            self.send_json(400, {"detail": "Add a user reference up to 255 characters."})
            return
        if preview_mode not in {"standard", "protected"}:
            self.send_json(400, {"detail": "Choose a valid preview mode."})
            return

        status, body = self.proxy_api("POST", "/embed-sessions", {
            "template_id": template_id,
            "external_user_id": external_user_id.strip(),
            "origin": self.server.public_origin,
            "mode": "test",
            "preview_mode": preview_mode,
        })
        if status < 200 or status >= 300:
            self.send_bytes(status, body, "application/json; charset=utf-8")
            return
        try:
            session = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(502, {"detail": "SharpToolz returned an invalid session response."})
            return
        result_token = secrets.token_urlsafe(32)
        with self.server.receipt_lock:
            if len(self.server.result_receipts) >= 100:
                oldest = min(self.server.result_receipts, key=lambda key: self.server.result_receipts[key]["created_at"])
                self.server.result_receipts.pop(oldest, None)
            self.server.result_receipts[result_token] = {
                "session_id": session["id"],
                "template_id": template_id,
                "external_user_id": external_user_id.strip(),
                "render_key": secrets.token_urlsafe(24),
                "render_job_id": None,
                "created_at": time.time(),
                "expires_at": time.time() + 3600,
            }
        session["result_token"] = result_token
        self.send_json(status, session)

    def create_result_render(self, incoming):
        result_token = incoming.get("resultToken")
        document_id = incoming.get("documentId")
        session_id = incoming.get("sessionId")
        receipt = self.get_receipt(result_token)
        try:
            uuid.UUID(str(document_id))
            uuid.UUID(str(session_id))
        except (ValueError, TypeError, AttributeError):
            receipt = None
        if not receipt or receipt["session_id"] != session_id:
            self.send_json(403, {"detail": "This result request is not valid for the hosted session."})
            return

        if receipt["render_job_id"]:
            status, body = self.proxy_api("GET", f"/renders/{receipt['render_job_id']}")
            self.send_bytes(status, body, "application/json; charset=utf-8")
            return

        status, document_body = self.proxy_api("GET", f"/documents/{document_id}")
        if status < 200 or status >= 300:
            self.send_bytes(status, document_body, "application/json; charset=utf-8")
            return
        try:
            document = json.loads(document_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(502, {"detail": "SharpToolz returned an invalid document response."})
            return
        if (
            document.get("template_id") != receipt["template_id"]
            or document.get("external_user_id") != receipt["external_user_id"]
        ):
            self.send_json(403, {"detail": "This document does not belong to the hosted session."})
            return

        status, body = self.proxy_api(
            "POST",
            f"/documents/{document_id}/render",
            {"format": "png"},
            {"Idempotency-Key": receipt["render_key"]},
        )
        if 200 <= status < 300:
            try:
                job = json.loads(body)
                receipt["render_job_id"] = job["id"]
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError):
                self.send_json(502, {"detail": "SharpToolz returned an invalid render response."})
                return
        self.send_bytes(status, body, "application/json; charset=utf-8")

    def poll_result_render(self, incoming):
        result_token = incoming.get("resultToken")
        job_id = incoming.get("jobId")
        receipt = self.get_receipt(result_token)
        try:
            uuid.UUID(str(job_id))
        except (ValueError, TypeError, AttributeError):
            receipt = None
        if not receipt or receipt["render_job_id"] != job_id:
            self.send_json(403, {"detail": "This render is not valid for the hosted session."})
            return
        status, body = self.proxy_api("GET", f"/renders/{job_id}")
        self.send_bytes(status, body, "application/json; charset=utf-8")


def main():
    host = os.getenv("DEMO_HOST", "127.0.0.1")
    port = int(os.getenv("DEMO_PORT", "4188"))
    api_key = os.getenv("SHARPTOOLZ_API_KEY", "").strip()
    if not api_key.startswith("stz_live_"):
        raise SystemExit("Set SHARPTOOLZ_API_KEY to a development API key before starting the demo.")

    server = DemoServer((host, port), DemoHandler)
    server.api_key = api_key
    server.api_base = os.getenv("SHARPTOOLZ_API_BASE", "http://127.0.0.1:8137/api/v1").rstrip("/")
    server.frontend_url = os.getenv("SHARPTOOLZ_FRONTEND_URL", "http://127.0.0.1:5173").rstrip("/")
    server.public_origin = os.getenv("DEMO_ORIGIN", f"http://{host}:{port}").rstrip("/")
    server.result_receipts = {}
    server.receipt_lock = threading.Lock()
    print(f"PaperPilot demo: {server.public_origin}")
    print("The SharpToolz API key is held only by this server process.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
