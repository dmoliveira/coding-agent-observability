"""Exercise the fixed local gateway with synthetic HTTP requests only."""

from __future__ import annotations

import contextlib
import http.client
import http.server
import io
import json
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import common

from integrations import lmstudio


class SyntheticProvider(http.server.BaseHTTPRequestHandler):
    calls: ClassVar[list[tuple[str, str, dict[str, str], bytes]]] = []
    redirect = False

    def respond(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.calls.append((self.command, self.path, dict(self.headers), body))
        if self.redirect:
            self.send_response(302)
            self.send_header("Location", "http://example.invalid/private")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = b'{"synthetic":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Set-Cookie", "synthetic-cookie")
        self.end_headers()
        self.wfile.write(payload)

    do_GET = respond
    do_POST = respond

    def log_message(self, format: str, *args: object) -> None:
        pass


class SyntheticLangfuse(http.server.BaseHTTPRequestHandler):
    stored: ClassVar[dict | None] = None
    writes: ClassVar[int] = 0

    def do_GET(self) -> None:
        data = [] if self.stored is None else [self.stored]
        self.respond({"data": data, "meta": {"totalPages": 1 if data else 0}})

    def do_PUT(self) -> None:
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.__class__.writes += 1
        self.__class__.stored = {
            "id": "synthetic-connection",
            "provider": payload["provider"],
            "adapter": payload["adapter"],
            "baseURL": payload["baseURL"],
            "customModels": payload["customModels"],
            "withDefaultModels": payload["withDefaultModels"],
            "extraHeaderKeys": [],
            "config": None,
            "displaySecretKey": "..." + payload["secretKey"][-4:],
            "updatedAt": f"synthetic-version-{self.writes}",
        }
        self.respond(self.stored)

    def respond(self, value: dict) -> None:
        payload = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass


class EndToEndSyncTests(unittest.TestCase):
    def test_real_http_create_and_additive_sync(self) -> None:
        SyntheticLangfuse.stored = None
        SyntheticLangfuse.writes = 0
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SyntheticLangfuse)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(common, "ROOT", Path(directory)),
        ):
            state = {
                "version": 1,
                "projectId": "synthetic-project",
                "provider": "ai-observatory-lmstudio-123456abcdef",
                "connectionId": None,
                "fingerprint": None,
                "usesToken": None,
            }
            lmstudio.write_state(state, None)
            state, raw = lmstudio.read_state()
            base = f"http://127.0.0.1:{server.server_port}"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                lmstudio.sync_connection(
                    base,
                    "Basic synthetic-secret",
                    state,
                    raw,
                    "lm-studio",
                    False,
                    ["local/model"],
                )
                state, raw = lmstudio.read_state()
                lmstudio.sync_connection(
                    base,
                    "Basic synthetic-secret",
                    state,
                    raw,
                    "lm-studio",
                    False,
                    ["other/model"],
                )
                state, raw = lmstudio.read_state()
                lmstudio.sync_connection(
                    base,
                    "Basic synthetic-secret",
                    state,
                    raw,
                    "lm-studio",
                    False,
                    ["other/model"],
                )
            self.assertEqual(
                SyntheticLangfuse.stored["customModels"],
                ["local/model", "other/model"],
            )
            self.assertEqual(SyntheticLangfuse.writes, 2)
            self.assertNotIn("synthetic-secret", output.getvalue())


@unittest.skipUnless(shutil.which("node"), "Node.js not installed")
class GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        SyntheticProvider.calls = []
        SyntheticProvider.redirect = False
        self.provider = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), SyntheticProvider
        )
        self.thread = threading.Thread(target=self.provider.serve_forever, daemon=True)
        self.thread.start()
        code = (
            "const g=require(process.argv[1]);"
            "const s=g.createGateway('127.0.0.1',Number(process.argv[2]));"
            "s.listen(0,'127.0.0.1',()=>console.log(s.address().port));"
        )
        self.process = subprocess.Popen(
            [
                "node",
                "-e",
                code,
                str(ROOT / "integrations/lmstudio/gateway.cjs"),
                str(self.provider.server_port),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(self.cleanup_server)
        ready, _, _ = select.select([self.process.stdout], [], [], 10)
        self.assertTrue(ready, "Gateway did not start")
        self.port = int(self.process.stdout.readline().strip())

    def cleanup_server(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
        try:
            _, stderr = self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            _, stderr = self.process.communicate(timeout=5)
        self.provider.shutdown()
        self.provider.server_close()
        self.thread.join(timeout=3)
        self.assertNotIn("synthetic-secret", stderr)

    def call(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict | None = None,
    ) -> tuple[int, dict, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_allows_only_model_list_and_chat_without_extra_headers(self) -> None:
        self.assertEqual(self.call("GET", "/healthz")[0], 204)
        self.assertEqual(SyntheticProvider.calls, [])
        status, headers, body = self.call(
            "GET",
            "/v1/models",
            headers={
                "Authorization": "Bearer synthetic-secret",
                "X-Unrelated": "private",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"synthetic": True})
        self.assertNotIn("set-cookie", {key.lower() for key in headers})
        _, path, forwarded, _ = SyntheticProvider.calls[-1]
        self.assertEqual(path, "/v1/models")
        self.assertEqual(forwarded.get("authorization"), "Bearer synthetic-secret")
        self.assertNotIn("x-unrelated", {key.lower() for key in forwarded})
        status, _, _ = self.call(
            "POST",
            "/v1/chat/completions",
            body=b'{"model":"synthetic"}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(SyntheticProvider.calls[-1][3], b'{"model":"synthetic"}')

    def test_blocks_other_paths_methods_queries_and_oversized_requests(self) -> None:
        for method, path in (
            ("GET", "/api/v1/models/download"),
            ("POST", "/v1/models"),
            ("GET", "/v1/models?target=private"),
            ("GET", "/v1/chat/completions"),
        ):
            with self.subTest(method=method, path=path):
                self.assertEqual(self.call(method, path)[0], 404)
        status, _, _ = self.call(
            "POST",
            "/v1/chat/completions",
            body=b"",
            headers={"Content-Length": str(32 * 1024 * 1024 + 1)},
        )
        self.assertEqual(status, 413)
        self.assertEqual(SyntheticProvider.calls, [])

    def test_provider_redirect_is_not_forwarded_or_followed(self) -> None:
        SyntheticProvider.redirect = True
        status, headers, _ = self.call("GET", "/v1/models")
        self.assertEqual(status, 502)
        self.assertNotIn("location", {key.lower() for key in headers})
        self.assertEqual(len(SyntheticProvider.calls), 1)

    def test_control_plane_refuses_redirects_and_does_not_follow_them(self) -> None:
        SyntheticProvider.redirect = True
        url = f"http://127.0.0.1:{self.provider.server_port}/v1/models"
        with self.assertRaises(lmstudio.HttpFailure) as caught:
            lmstudio.request_json(
                url, service="synthetic", authorization="Bearer synthetic-secret"
            )
        self.assertEqual(caught.exception.status, 302)
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertEqual(len(SyntheticProvider.calls), 1)


if __name__ == "__main__":
    unittest.main()
