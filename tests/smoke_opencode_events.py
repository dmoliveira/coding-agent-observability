"""Exercise both plugins against isolated synthetic OpenCode, model and collector."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import ClassVar

import smoke_opencode_global as fixture

ROOT = Path(__file__).resolve().parents[1]


class Collector(fixture.Collector):
    requests: ClassVar[list[tuple[str, dict[str, str], bytes]]] = []

    def do_GET(self) -> None:
        if self.path == "/api/public/projects":
            data = {"data": [{"id": "synthetic-project"}]}
        elif self.path.startswith("/api/public/v2/evaluation-rules?"):
            data = {
                "data": [
                    {
                        "enabled": True,
                        "filter": [
                            {
                                "column": "type",
                                "type": "stringOptions",
                                "operator": "any of",
                                "value": ["SPAN"],
                            }
                        ],
                    }
                ],
                "meta": {},
            }
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


def spans() -> tuple[list[dict], list[bytes]]:
    result = []
    raw_events = []
    for _, _, raw in Collector.requests:
        document = json.loads(raw)
        for resource in document.get("resourceSpans", []):
            attrs = resource.get("resource", {}).get("attributes", [])
            is_events = any(
                item.get("key") == "service.name"
                and item.get("value", {}).get("stringValue")
                == "ai-observatory-structural-events"
                for item in attrs
            )
            if is_events:
                raw_events.append(raw)
                for scope in resource.get("scopeSpans", []):
                    result.extend(scope.get("spans", []))
    return result, raw_events


def run() -> None:
    collector, model = fixture.server(Collector), fixture.server(fixture.Model)
    try:
        with tempfile.TemporaryDirectory(prefix="aiobs-events-sandbox-") as tmp:
            root = Path(tmp)
            work = root / "project"
            work.mkdir()
            fixture.Model.sample = str(work / "sample.txt")
            fixture.Model.sent_tool = False
            (work / "sample.txt").write_text("SYNTHETIC_TOOL_CONTENT\n")
            config = root / "config"
            private = config / "ai-observatory"
            private.mkdir(parents=True, mode=0o700)
            credentials = private / "opencode-global.json"
            credentials.write_text(
                json.dumps(
                    {
                        "publicKey": fixture.PUBLIC,
                        "secretKey": fixture.SECRET,
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
            env.update(
                {
                    "HOME": str(root),
                    "XDG_CONFIG_HOME": str(config),
                    "XDG_DATA_HOME": str(root / "data"),
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "MOCK_KEY": "synthetic",
                }
            )
            for name in (
                "opencode_global.py",
                "opencode_events.py",
                "opencode_events.py",
            ):
                args = ["connect"] if name == "opencode_events.py" else []
                result = subprocess.run(
                    ["python3", str(ROOT / "scripts" / "integrations" / name), *args],
                    cwd=work,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
                if result.returncode:
                    raise AssertionError(f"Synthetic installation failed: {name}")
                assert fixture.SECRET not in result.stdout + result.stderr
            result = subprocess.run(
                [
                    "opencode",
                    "--standalone",
                    "run",
                    "--auto",
                    "--model",
                    "mock/mock-model",
                    "SYNTHETIC_PROMPT: read sample.txt, then reply.",
                ],
                cwd=work,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if result.returncode:
                raise AssertionError("Synthetic OpenCode run failed")
            assert fixture.Model.sent_tool
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not {
                "aiobs.execution.started",
                "aiobs.execution.succeeded",
            } <= {span.get("name") for span in spans()[0]}:
                time.sleep(0.1)
            captured, raw = spans()
            names = {span.get("name") for span in captured}
            assert {"aiobs.execution.started", "aiobs.execution.succeeded"} <= names, (
                names
            )
            assert b"SYNTHETIC_PROMPT" not in b"".join(raw)
            assert b"SYNTHETIC_TOOL_CONTENT" not in b"".join(raw)
            assert all(
                headers.get("Authorization") == fixture.AUTH
                for _, headers, _ in Collector.requests
            )
            before = len(captured)
            for _ in range(2):
                result = subprocess.run(
                    [
                        "python3",
                        str(ROOT / "scripts" / "integrations" / "opencode_events.py"),
                        "disconnect",
                    ],
                    cwd=work,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if result.returncode:
                    raise AssertionError("Synthetic structural event disconnect failed")
            assert (config / "opencode" / "plugins" / "ai-observatory.js").is_file()
            fixture.Model.sent_tool = False
            result = subprocess.run(
                [
                    "opencode",
                    "--standalone",
                    "run",
                    "--auto",
                    "--model",
                    "mock/mock-model",
                    "SYNTHETIC_SECOND: read sample.txt.",
                ],
                cwd=work,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if result.returncode:
                raise AssertionError("Synthetic OpenCode run after disconnect failed")
            assert fixture.Model.sent_tool
            assert len(spans()[0]) == before, (
                "disconnected structural plugin exported events"
            )
            print(
                "PASS: structural plugin installs beside tracing, exports content-free events, and disconnects without disabling tracing"
            )
    finally:
        for instance in (collector, model):
            instance.shutdown()
            instance.server_close()


if __name__ == "__main__":
    run()
