"""Exercise Tailscale's conditional-update protocol using a fake local daemon."""

from __future__ import annotations

import copy
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from integrations import tailscale


class FakeDaemon(BaseHTTPRequestHandler):
    config: ClassVar[dict] = {}
    version = 1
    posts: ClassVar[list[dict]] = []

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("ETag", f'"{self.version}"')
        self.end_headers()
        self.wfile.write(json.dumps(self.config).encode())

    def do_POST(self) -> None:
        if self.headers.get("If-Match") != f'"{self.version}"':
            self.send_response(412)
            self.end_headers()
            return
        config = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).posts.append(copy.deepcopy(config))
        type(self).config = config
        type(self).version += 1
        self.send_response(200)
        self.end_headers()


class TailscaleProtocolTests(unittest.TestCase):
    def test_enable_and_disable_preserve_existing_funnel(self) -> None:
        host = "node.example.ts.net"
        local = {
            "WEB_PORT": "3000",
            "MEDIA_PORT": "9090",
            "NEXTAUTH_URL": "http://127.0.0.1:3000",
            "MEDIA_URL": "http://127.0.0.1:9090",
        }
        existing = {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {f"{host}:443": {"Handlers": {"/other": {"Text": "keep"}}}},
            "AllowFunnel": {f"{host}:443": True},
        }
        FakeDaemon.config, FakeDaemon.version, FakeDaemon.posts = (
            copy.deepcopy(existing),
            1,
            [],
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeDaemon)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, timeout=5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        api = object.__new__(tailscale.LocalAPI)
        api.socket_path, api.port, api.authorization = None, server.server_port, None
        with (
            mock.patch.object(tailscale, "load_config", return_value=local),
            mock.patch.object(tailscale, "require_running"),
            mock.patch.object(tailscale, "tailscale_hostname", return_value=host),
            mock.patch.object(tailscale, "LocalAPI", return_value=api),
            mock.patch.object(tailscale, "replace_config") as replace,
            mock.patch.object(tailscale, "run_compose"),
        ):
            tailscale.enable()
        remote = replace.call_args.args[1]
        self.assertEqual(FakeDaemon.config["AllowFunnel"], existing["AllowFunnel"])
        self.assertNotIn(f"{host}:15443", FakeDaemon.config["AllowFunnel"])
        self.assertEqual(
            FakeDaemon.config["Web"][f"{host}:15443"], tailscale.route("3000")
        )
        with (
            mock.patch.object(tailscale, "load_config", return_value=remote),
            mock.patch.object(tailscale, "tailscale_hostname", return_value=host),
            mock.patch.object(tailscale, "LocalAPI", return_value=api),
            mock.patch.object(tailscale, "replace_config"),
        ):
            tailscale.disable(restart=False)
        self.assertEqual(FakeDaemon.config, existing)
        self.assertEqual(len(FakeDaemon.posts), 2)


if __name__ == "__main__":
    unittest.main()
