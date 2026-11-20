"""Exercise two synthetic OpenCode locations through an in-flight reload."""

import base64
import gzip
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

base = Path(os.environ.get("TMPDIR", tempfile.gettempdir())).resolve()
adapter = Path(__file__).resolve().parents[1] / "integrations/opencode-global/index.js"
events_adapter = (
    Path(__file__).resolve().parents[1] / "integrations/opencode-events/index.js"
)


class Sink(BaseHTTPRequestHandler):
    items: ClassVar[list[dict]] = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        chunks = []
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            while True:
                size = int(self.rfile.readline().split(b";")[0], 16)
                if not size:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            raw = b"".join(chunks)
        else:
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        Sink.items.append(json.loads(raw))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")


class Model(BaseHTTPRequestHandler):
    started = threading.Event()
    release = threading.Event()

    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = json.dumps(body.get("messages", []))
        hold = "AI-OBS-SYNTHETIC-SESSION-A" in messages
        compacting = "You MUST use this format" in messages
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if hold:
            Model.started.set()
            if not Model.release.wait(60):
                return
        for delta, finish in (
            (
                {
                    "role": "assistant",
                    "content": "## Objective\n- synthetic"
                    if compacting
                    else "synthetic",
                },
                None,
            ),
            ({}, "stop"),
        ):
            event = {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "mock-model",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if compacting and finish:
                event["usage"] = {
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "total_tokens": 15,
                }
            self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")


def listen(cls):
    s = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


def readspans():
    return [
        s
        for doc in Sink.items
        for resource in doc.get("resourceSpans", [])
        for scope in resource.get("scopeSpans", [])
        for s in scope.get("spans", [])
    ]


sink, model = listen(Sink), listen(Model)
server = None
clients = []
try:
    with tempfile.TemporaryDirectory(prefix="aiobs-shared-", dir=base) as tmp:
        root = Path(tmp)
        cfg = root / "config"
        private = cfg / "ai-observatory"
        private.mkdir(parents=True, mode=0o700)
        (private / "opencode-global.json").write_text(
            json.dumps(
                {
                    "publicKey": "pk-lf-synthetic",
                    "secretKey": "sk-lf-synthetic",
                    "baseUrl": f"http://127.0.0.1:{sink.server_port}",
                }
            )
        )
        (private / "opencode-global.json").chmod(0o600)
        plug = cfg / "opencode" / "plugins"
        plug.mkdir(parents=True)
        (plug / "ai-observatory.js").write_text(
            f"export {{ default }} from {json.dumps(adapter.as_uri())};\n"
        )
        (plug / "ai-observatory-events.js").write_text(
            f"export {{ default }} from {json.dumps(events_adapter.as_uri())};\n"
        )
        probe = root / "probe.txt"
        (plug / "sandbox-probe.js").write_text(
            """import { appendFileSync } from "node:fs";
const file = """
            + json.dumps(str(probe))
            + """;
export default { id: "aiobs.sandbox.probe", setup(ctx) {
  appendFileSync(file, `load\\t${ctx.location.directory}\\n`);
  return () => appendFileSync(file, `unload\\t${ctx.location.directory}\\n`);
} };"""
        )
        config = {
            "providers": {
                "mock": {
                    "name": "Synthetic",
                    "env": ["MOCK_KEY"],
                    "package": "@opencode/ai/providers/openai-compatible",
                    "settings": {"baseURL": f"http://127.0.0.1:{model.server_port}/v1"},
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
        a = root / "project-a"
        b = root / "project-b"
        a.mkdir()
        b.mkdir()
        for p in (a, b):
            (p / "opencode.json").write_text(json.dumps(config))
        env = {
            k: v
            for k, v in os.environ.items()
            if k in {"PATH", "TMPDIR", "LANG", "SSL_CERT_FILE"}
        }
        env.update(
            {
                "HOME": str(root),
                "XDG_CONFIG_HOME": str(cfg),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "MOCK_KEY": "local-test",
                "OPENCODE_PASSWORD": "sandbox-password",
                "LANGFUSE_PUBLIC_KEY": "pk-lf-other",
                "LANGFUSE_SECRET_KEY": "sk-lf-other",
                "LANGFUSE_BASE_URL": "http://127.0.0.1:1",
                "NO_PROXY": "127.0.0.1,localhost",
            }
        )
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        server = subprocess.Popen(
            ["opencode", "serve", "--hostname", "127.0.0.1", "--port", str(port)],
            cwd=a,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 30
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise AssertionError("sandbox server exited")
            try:
                with opener.open(url + "/api/info", timeout=1):
                    break
            except urllib.error.HTTPError as error:
                if error.code != 401:
                    raise
                break
            except OSError:
                time.sleep(0.2)
        else:
            raise AssertionError("sandbox server did not start")

        def client(path, text):
            p = subprocess.Popen(
                [
                    "opencode",
                    "--server",
                    url,
                    "run",
                    "--auto",
                    "--model",
                    "mock/mock-model",
                    text,
                ],
                cwd=path,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            clients.append(p)
            return p

        pa = client(a, "AI-OBS-SYNTHETIC-SESSION-A")
        if not Model.started.wait(45):
            if pa.poll() is not None:
                o, e = pa.communicate()
                raise AssertionError("session A did not start: " + (o + e)[-1500:])
            raise AssertionError("session A did not start")
        pb = client(b, "AI-OBS-SYNTHETIC-SESSION-B")
        outb, errb = pb.communicate(timeout=60)
        if pb.returncode:
            raise AssertionError("session B failed " + (outb + errb)[-1000:])
        req = urllib.request.Request(
            url + "/api/location/reload",
            data=b"",
            method="POST",
            headers={
                "Authorization": "Basic "
                + base64.b64encode(b"opencode:sandbox-password").decode()
            },
        )
        with opener.open(req, timeout=45) as response:
            assert response.status == 204
        deadline = time.monotonic() + 10
        expected = "unload\t" + str(b)
        while time.monotonic() < deadline:
            if probe.exists() and expected in probe.read_text():
                break
            time.sleep(0.2)
        else:
            raise AssertionError(
                "did not observe B plugin reload; "
                + (probe.read_text() if probe.exists() else "no probe")
            )
        Model.release.set()
        outa, erra = pa.communicate(timeout=60)
        if pa.returncode:
            raise AssertionError("session A failed " + (outa + erra)[-1000:])
        time.sleep(1)
        spans = readspans()
        session_ids = {}
        for span in spans:
            attrs = {
                a["key"]: next(iter(a["value"].values()))
                for a in span.get("attributes", [])
            }
            text = json.dumps(attrs)
            for label in ("A", "B"):
                if (
                    "AI-OBS-SYNTHETIC-SESSION-" + label in text
                    and "session.id" in attrs
                ):
                    session_ids[label] = attrs["session.id"]
        if set(session_ids) != {"A", "B"}:
            raise AssertionError("both sessions not found")
        for label, sid in session_ids.items():
            session_spans = [
                s
                for s in spans
                if any(
                    a.get("key") == "session.id"
                    and a.get("value", {}).get("stringValue") == sid
                    for a in s.get("attributes", [])
                )
            ]
            names = [s["name"] for s in session_spans]
            if (
                names.count("opencode.turn") != 1
                or names.count("opencode.generation") != 1
            ):
                raise AssertionError(f"session {label} unexpected span names: {names}")
            if (
                names.count("aiobs.execution.started") != 1
                or names.count("aiobs.execution.succeeded") != 1
            ):
                raise AssertionError(
                    f"session {label} unexpected structural event counts: {names}"
                )
            for item in session_spans:
                if item["name"].startswith(
                    "aiobs."
                ) and "AI-OBS-SYNTHETIC-SESSION" in json.dumps(item):
                    raise AssertionError("structural event leaked synthetic prompt")
            generation = next(
                s for s in session_spans if s["name"] == "opencode.generation"
            )
            generation_keys = {a["key"] for a in generation.get("attributes", [])}
            if "langfuse.observation.output" not in generation_keys:
                raise AssertionError(
                    f"session {label} generation output missing after reload; keys: {sorted(generation_keys)}; events:\n"
                    + probe.read_text()
                )
            output = next(
                a["value"]["stringValue"]
                for a in generation.get("attributes", [])
                if a["key"] == "langfuse.observation.output"
            )
            if "synthetic" not in output:
                raise AssertionError(
                    f"session {label} generation output was incomplete"
                )
        # Exercise an actual manual compaction in the isolated server, not just a fabricated event.
        request = urllib.request.Request(
            url + f"/api/session/{session_ids['A']}/compact",
            data=b"{}",
            method="POST",
            headers={
                "Authorization": "Basic "
                + base64.b64encode(b"opencode:sandbox-password").decode(),
                "Content-Type": "application/json",
            },
        )
        with opener.open(request, timeout=30) as response:
            assert response.status in (200, 201, 202)
        continuation = subprocess.run(
            [
                "opencode",
                "--server",
                url,
                "run",
                "--auto",
                "--session",
                session_ids["A"],
                "--model",
                "mock/mock-model",
                "AI-OBS-SYNTHETIC-AFTER-COMPACTION",
            ],
            cwd=a,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if continuation.returncode:
            raise AssertionError("synthetic continuation after compaction failed")
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            compact_spans = [
                s
                for s in readspans()
                if s["name"].startswith("aiobs.compaction.")
                and any(
                    a.get("key") == "session.id"
                    and a.get("value", {}).get("stringValue") == session_ids["A"]
                    for a in s.get("attributes", [])
                )
            ]
            if any(s["name"] == "aiobs.compaction.ended.manual" for s in compact_spans):
                break
            time.sleep(0.1)
        else:
            raise AssertionError(
                "synthetic manual compaction did not complete: "
                + str([s["name"] for s in compact_spans])
            )
        assert (
            sum(s["name"] == "aiobs.compaction.started.manual" for s in compact_spans)
            == 1
        )
        ended = next(
            s for s in compact_spans if s["name"] == "aiobs.compaction.ended.manual"
        )
        attrs = {
            a["key"]: a["value"]["stringValue"] for a in ended.get("attributes", [])
        }
        usage = json.loads(attrs["langfuse.observation.usage_details"])
        assert set(usage) == {
            "input",
            "output",
            "reasoning",
            "cache_read",
            "cache_write",
        }
        assert all(isinstance(value, int) and value >= 0 for value in usage.values())
        assert (usage["input"], usage["output"]) == (12, 3), (
            "compaction token counts were not preserved"
        )
        assert "AI-OBS-SYNTHETIC-SESSION" not in json.dumps(compact_spans)
        # Create and reply to a real permission request in the isolated server.
        auth = "Basic " + base64.b64encode(b"opencode:sandbox-password").decode()

        def api_json(path, method, payload):
            req = urllib.request.Request(
                url + path,
                method=method,
                data=json.dumps(payload).encode(),
                headers={"Authorization": auth, "Content-Type": "application/json"},
            )
            with opener.open(req, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}

        sid = session_ids["A"]
        api_json(
            f"/api/session/{sid}",
            "PATCH",
            {
                "permissions": [
                    {
                        "action": "SYNTHETIC_PERMISSION_ACTION",
                        "resource": "*",
                        "effect": "ask",
                    }
                ]
            },
        )
        request_id = "per_synthetic_private"
        response = api_json(
            f"/api/session/{sid}/permission",
            "POST",
            {
                "id": request_id,
                "action": "SYNTHETIC_PERMISSION_ACTION",
                "resources": ["SYNTHETIC_PERMISSION_PATH"],
                "metadata": {"secret": "SYNTHETIC_PERMISSION_METADATA"},
            },
        )
        assert response["data"]["effect"] == "ask"
        api_json(
            f"/api/session/{sid}/permission/{request_id}/reply",
            "POST",
            {"decision": "once", "message": "SYNTHETIC_PERMISSION_FEEDBACK"},
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            permission_spans = [
                s for s in readspans() if s["name"].startswith("aiobs.permission.")
            ]
            if {s["name"] for s in permission_spans} >= {
                "aiobs.permission.asked",
                "aiobs.permission.replied.once",
            }:
                break
            time.sleep(0.1)
        else:
            raise AssertionError("isolated permission ask/reply events not captured")
        asked = next(
            s for s in permission_spans if s["name"] == "aiobs.permission.asked"
        )
        replied = next(
            s for s in permission_spans if s["name"] == "aiobs.permission.replied.once"
        )
        assert (
            asked["traceId"] == replied["traceId"]
            and asked["spanId"] != replied["spanId"]
        )
        assert int(replied["startTimeUnixNano"]) >= int(asked["startTimeUnixNano"])
        assert "SYNTHETIC_PERMISSION_" not in json.dumps(permission_spans)
        assert request_id not in json.dumps(permission_spans)
        print(
            "PASS: two locations, reload, compaction usage and content-free permission ask/reply correlation"
        )
finally:
    Model.release.set()
    for p in clients:
        if p.poll() is None:
            p.terminate()
            p.wait(timeout=15)
    if server and server.poll() is None:
        server.terminate()
        server.wait(timeout=15)
    for s in (sink, model):
        s.shutdown()
        s.server_close()
