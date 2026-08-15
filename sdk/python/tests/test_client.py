import json
import unittest

import httpx

from sharptoolz import SharpToolz


class FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def recv(self, timeout=None):
        return json.dumps({
            "type": "render.updated",
            "data": {"id": "job-1", "status": "completed"},
        })


class SharpToolzClientTests(unittest.TestCase):
    def test_api_key_stays_in_authorization_header(self):
        captured = {}

        def handler(request):
            captured["request"] = request
            return httpx.Response(200, json={"results": []})

        with SharpToolz(
            api_key="stz_live_test.key",
            transport=httpx.MockTransport(handler),
        ) as client:
            client.templates.list()

        request = captured["request"]
        self.assertEqual(request.headers["Authorization"], "Bearer stz_live_test.key")
        self.assertNotIn("stz_live_test.key", str(request.url))

    def test_render_wait_uses_websocket_then_fetches_final_job_once(self):
        calls = []

        def handler(request):
            calls.append((request.method, request.url.path))
            if request.url.path.endswith("/watch"):
                return httpx.Response(200, json={"websocket_url": "wss://api.sharptoolz.com/ws/job"})
            return httpx.Response(200, json={
                "id": "job-1",
                "status": "completed",
                "download_url": "https://signed.example/file",
            })

        with SharpToolz(
            api_key="stz_live_test.key",
            transport=httpx.MockTransport(handler),
            websocket_connect=lambda *args, **kwargs: FakeSocket(),
        ) as client:
            result = client.renders.wait({"id": "job-1", "status": "queued"})

        self.assertEqual(result["download_url"], "https://signed.example/file")
        self.assertEqual(calls, [
            ("POST", "/api/v1/renders/job-1/watch"),
            ("GET", "/api/v1/renders/job-1"),
        ])

    def test_creation_and_editing_are_hosted_session_only(self):
        calls = []

        def handler(request):
            calls.append((request.method, request.url.path))
            return httpx.Response(201, json={"id": "session-1"})

        with SharpToolz(
            api_key="stz_live_test.key",
            transport=httpx.MockTransport(handler),
        ) as client:
            client.hosted_forms.create(
                template_id="template-1",
                external_user_id="user-1",
                origin="https://app.example",
            )
            client.hosted_forms.edit("document-1", origin="https://app.example")
            self.assertFalse(hasattr(client.templates, "schema"))
            self.assertFalse(hasattr(client.documents, "create"))

        self.assertEqual(calls, [
            ("POST", "/api/v1/embed-sessions"),
            ("POST", "/api/v1/documents/document-1/session"),
        ])

    def test_document_list_accepts_api_next_url_as_cursor(self):
        captured = {}

        def handler(request):
            captured["query"] = request.url.query.decode()
            return httpx.Response(200, json={"results": [], "next": None, "previous": None})

        with SharpToolz(
            api_key="stz_live_test.key",
            transport=httpx.MockTransport(handler),
        ) as client:
            client.documents.list(
                cursor="https://api.sharptoolz.com/api/v1/documents?cursor=cD0yMDI2"
            )

        self.assertEqual(captured["query"], "cursor=cD0yMDI2")


if __name__ == "__main__":
    unittest.main()
