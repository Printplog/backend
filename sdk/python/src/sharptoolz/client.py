from __future__ import annotations

import json
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from websockets.sync.client import connect as websocket_connect_default


DEFAULT_BASE_URL = "https://api.sharptoolz.com/api/v1"
TERMINAL_RENDER_STATUSES = {"completed", "failed"}


def _normalize_cursor(cursor: str | None) -> str | None:
    if not cursor:
        return None
    parsed = urlsplit(cursor)
    if parsed.scheme and parsed.netloc:
        return parse_qs(parsed.query).get("cursor", [cursor])[0]
    return cursor


class SharpToolzError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, data: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.data = data


class TemplatesResource:
    def __init__(self, client: "SharpToolz"):
        self._client = client

    def list(self) -> dict[str, Any]:
        return self._client._request("GET", "/templates")

class HostedFormsResource:
    def __init__(self, client: "SharpToolz"):
        self._client = client

    def create(self, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", "/embed-sessions", json=payload)

    def edit(self, document_id: str, **payload: Any) -> dict[str, Any]:
        return self._client._request("POST", f"/documents/{document_id}/session", json=payload)

    def revoke(self, session_id: str) -> None:
        self._client._request("DELETE", f"/embed-sessions/{session_id}")


class DocumentsResource:
    def __init__(self, client: "SharpToolz"):
        self._client = client

    def get(self, document_id: str) -> dict[str, Any]:
        return self._client._request("GET", f"/documents/{document_id}")

    def list(self, *, external_user_id: str | None = None, cursor: str | None = None) -> dict[str, Any]:
        params = {key: value for key, value in {
            "external_user_id": external_user_id,
            "cursor": _normalize_cursor(cursor),
        }.items() if value}
        return self._client._request("GET", "/documents", params=params)

    def delete(self, document_id: str) -> None:
        self._client._request("DELETE", f"/documents/{document_id}")

    def upgrade(self, document_id: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        return self._client._request(
            "POST",
            f"/documents/{document_id}/upgrade",
            idempotency_key=idempotency_key or str(uuid4()),
        )

    def render(
        self,
        document_id: str,
        *,
        format: str = "pdf",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return self._client._request(
            "POST",
            f"/documents/{document_id}/render",
            json={"format": format},
            idempotency_key=idempotency_key or str(uuid4()),
        )

    def render_and_wait(
        self,
        document_id: str,
        *,
        format: str = "pdf",
        idempotency_key: str | None = None,
        timeout: float = 120,
        poll_fallback: bool = True,
    ) -> dict[str, Any]:
        job = self.render(document_id, format=format, idempotency_key=idempotency_key)
        return self._client.renders.wait(
            job,
            timeout=timeout,
            poll_fallback=poll_fallback,
        )


class RendersResource:
    def __init__(self, client: "SharpToolz"):
        self._client = client

    def get(self, job_id: str) -> dict[str, Any]:
        return self._client._request("GET", f"/renders/{job_id}")

    def wait(
        self,
        job_or_id: dict[str, Any] | str,
        *,
        timeout: float = 120,
        poll_fallback: bool = True,
    ) -> dict[str, Any]:
        job = self.get(job_or_id) if isinstance(job_or_id, str) else job_or_id
        if not job.get("id"):
            raise TypeError("A render job or job ID is required.")
        if job.get("status") in TERMINAL_RENDER_STATUSES:
            return self._finish(job)

        watch = self._client._request("POST", f"/renders/{job['id']}/watch")
        try:
            terminal = self._wait_on_websocket(watch["websocket_url"], timeout=timeout)
            return self._finish(self.get(terminal["id"]))
        except Exception:
            if not poll_fallback:
                raise
            return self._wait_by_polling(job["id"], timeout=timeout)

    def _wait_on_websocket(self, url: str, *, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._client._websocket_connect(url, open_timeout=min(10, timeout)) as socket:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SharpToolzError("Render wait timed out.")
                raw = socket.recv(timeout=remaining)
                message = json.loads(raw)
                data = message.get("data") if message.get("type") == "render.updated" else None
                if data and data.get("status") in TERMINAL_RENDER_STATUSES:
                    return data

    def _wait_by_polling(self, job_id: str, *, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        interval = 0.75
        while time.monotonic() < deadline:
            time.sleep(interval)
            job = self.get(job_id)
            if job.get("status") in TERMINAL_RENDER_STATUSES:
                return self._finish(job)
            interval = min(interval * 1.5, 4)
        raise SharpToolzError("Render wait timed out.")

    @staticmethod
    def _finish(job: dict[str, Any]) -> dict[str, Any]:
        if job.get("status") == "failed":
            code = job.get("error_code")
            raise SharpToolzError(f"Render failed{f': {code}' if code else '.'}", data=job)
        return job


class SharpToolz:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
        websocket_connect: Callable[..., Any] = websocket_connect_default,
    ):
        if not isinstance(api_key, str) or not api_key.startswith("stz_live_"):
            raise TypeError("A SharpToolz server API key is required.")
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "sharptoolz-python/0.2.0",
            },
        )
        self._websocket_connect = websocket_connect
        self.templates = TemplatesResource(self)
        self.hosted_forms = HostedFormsResource(self)
        self.documents = DocumentsResource(self)
        self.renders = RendersResource(self)

    def __enter__(self) -> "SharpToolz":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        response = self._http.request(method, path, json=json, params=params, headers=headers)
        if response.status_code == 204:
            return None
        try:
            data = response.json()
        except ValueError:
            data = None
        if response.is_error:
            message = (
                data.get("detail") if isinstance(data, dict) else None
            ) or f"SharpToolz request failed with HTTP {response.status_code}."
            raise SharpToolzError(message, status_code=response.status_code, data=data)
        return data
