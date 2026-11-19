"""Exercise the global adapter with a fake model and local OTLP collectors.

Run explicitly after preparing the pinned package; never uses a real model or Langfuse.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = "pk-lf-synthetic-private"
SECRET = "sk-lf-synthetic-private"
AUTH = "Basic " + base64.b64encode(f"{PUBLIC}:{SECRET}".encode()).decode()


class Collector(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, dict[str, str], bytes]]] = []

    def log_message(self, *_: object) -> None:
        pass

    def do_POST(self) -> None:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0], 16)
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            raw = b"".join(chunks)
        else:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        type(self).requests.append((self.path, dict(self.headers), raw))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")


class Decoy(Collector):
    requests: ClassVar[list[tuple[str, dict[str, str], bytes]]] = []


class Model(BaseHTTPRequestHandler):
    sent_tool = False
    requests = 0
    sample = ""

    def log_message(self, *_: object) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/v1/chat/completions"
        Model.requests += 1
        tool_names = {tool["function"]["name"] for tool in body.get("tools", [])}
        if not Model.sent_tool and "read" in tool_names:
            Model.sent_tool = True
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_synthetic",
                        "type": "function",
                        "function": {
                            "name": "read",
                            "arguments": json.dumps({"path": Model.sample}),
                        },
                    }
                ],
            }
            reason = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "Synthetic local response."}
            reason = "stop"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for piece, finish in ((delta, None), ({}, reason)):
            event = {
                "id": "chatcmpl-synthetic",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "mock-model",
                "choices": [{"index": 0, "delta": piece, "finish_reason": finish}],
            }
            self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


def server(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    instance = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=instance.serve_forever, daemon=True).start()
    return instance


def spans() -> list[dict]:
    result = []
    for path, _, raw in Collector.requests:
        assert path == "/api/public/otel/v1/traces"
        document = json.loads(raw)
        for resource in document.get("resourceSpans", []):
            for scope in resource.get("scopeSpans", []):
                result.extend(scope.get("spans", []))
    return result


def run() -> None:
    collector, decoy, model = server(Collector), server(Decoy), server(Model)
    try:
        with tempfile.TemporaryDirectory(prefix="aiobs-opencode-sandbox-") as tmp:
            root = Path(tmp)
            work = root / "project"
            work.mkdir()
            Model.sample = str(work / "sample.txt")
            (work / "sample.txt").write_text("Only synthetic test content.\n")
            config = root / "config"
            private = config / "ai-observatory"
            private.mkdir(parents=True, mode=0o700)
            credentials = private / "opencode-global.json"
            credentials.write_text(
                json.dumps(
                    {
                        "publicKey": PUBLIC,
                        "secretKey": SECRET,
                        "baseUrl": f"http://127.0.0.1:{collector.server_port}",
                    }
                )
            )
            credentials.chmod(0o600)
            (work / "opencode.json").write_text(
                json.dumps(
                    {
                        "providers": {
                            "mock": {
                                "name": "Synthetic",
                                "env": ["MOCK_KEY"],
                                "package": "@opencode/ai/providers/openai-compatible",
                                "settings": {
                                    "baseURL": f"http://127.0.0.1:{model.server_port}/v1"
                                },
                                "models": {
                                    "mock-model": {
                                        "name": "Synthetic",
                                        "capabilities": {"tools": True},
                                        "limit": {"context": 32768, "output": 4096},
                                    }
                                },
                            }
                        }
                    }
                )
            )
            env = {
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "TMPDIR", "LANG", "SSL_CERT_FILE"}
            }
            decoy_url = f"http://127.0.0.1:{decoy.server_port}"
            env.update(
                {
                    "HOME": str(root),
                    "XDG_CONFIG_HOME": str(config),
                    "XDG_DATA_HOME": str(root / "data"),
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "MOCK_KEY": "local-test",
                    "LANGFUSE_PUBLIC_KEY": "pk-lf-unrelated",
                    "LANGFUSE_SECRET_KEY": "sk-lf-unrelated",
                    "LANGFUSE_BASE_URL": decoy_url,
                    "LANGFUSE_BASEURL": decoy_url,
                    "LANGFUSE_DEBUG": "true",
                    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": decoy_url,
                    "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "x-poison=must-not-appear",
                    "OTEL_RESOURCE_ATTRIBUTES": "aiobs.poison=must-not-appear",
                    "HTTP_PROXY": decoy_url,
                    "HTTPS_PROXY": decoy_url,
                    "NO_PROXY": "127.0.0.1,localhost",
                }
            )

            install_env = {
                key: value
                for key, value in env.items()
                if key not in {"HTTP_PROXY", "HTTPS_PROXY"}
            }
            for _ in range(2):
                installed = subprocess.run(
                    [
                        "python3",
                        str(ROOT / "scripts" / "integrations" / "opencode_global.py"),
                    ],
                    cwd=work,
                    env=install_env,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
                if installed.returncode:
                    output = installed.stdout + installed.stderr
                    print(
                        output.replace(PUBLIC, "[redacted]").replace(
                            SECRET, "[redacted]"
                        )
                    )
                    raise AssertionError("Synthetic global installation failed")
                assert SECRET not in installed.stdout + installed.stderr

            def execute(disabled: bool) -> None:
                Model.sent_tool = False
                run_env = env.copy()
                if disabled:
                    run_env["AI_OBSERVATORY_DISABLED"] = "1"
                result = subprocess.run(
                    [
                        "opencode",
                        "--standalone",
                        "run",
                        "--auto",
                        "--model",
                        "mock/mock-model",
                        "Read sample.txt, then reply.",
                    ],
                    cwd=work,
                    env=run_env,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
                output = result.stdout + result.stderr
                if result.returncode:
                    print(
                        output.replace(PUBLIC, "[redacted]").replace(
                            SECRET, "[redacted]"
                        )[-3000:]
                    )
                    raise AssertionError(f"OpenCode exited {result.returncode}")
                assert PUBLIC not in output and SECRET not in output
                assert Model.sent_tool, "mock model did not exercise the read tool"

            execute(disabled=False)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not {
                "opencode.generation",
                "read",
            } <= {span.get("name") for span in spans()}:
                time.sleep(0.2)
            before = spans()
            names = {span.get("name") for span in before}
            assert {"opencode.turn", "opencode.generation", "read"} <= names, names
            assert all(
                headers.get("Authorization") == AUTH
                for _, headers, _ in Collector.requests
            )
            assert all(
                "x-poison" not in {key.lower() for key in headers}
                for _, headers, _ in Collector.requests
            )
            assert b"must-not-appear" not in b"".join(
                body for _, _, body in Collector.requests
            )
            assert not Decoy.requests
            execute(disabled=True)
            time.sleep(1)
            assert len(spans()) == len(before), "disabled session exported observations"
            assert not Decoy.requests
            print(
                "PASS: installer, default tracing, generation, tool, private destination, poisoned environment and startup opt-out"
            )
    finally:
        for instance in (collector, decoy, model):
            instance.shutdown()
            instance.server_close()


if __name__ == "__main__":
    run()
