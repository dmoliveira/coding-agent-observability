"""Opt-in local LM Studio discovery and additive Langfuse connection sync."""

from __future__ import annotations

import base64
import getpass
import hashlib
import http.client
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common

from integrations.opencode import NoRedirect, write_new
from integrations.opencode_global import private_config_path, read_private_json

HOST = "http://127.0.0.1:1234"
GATEWAY = "http://lmstudio-gateway:8787/v1"
STATE_NAME = ".ai-observatory.lmstudio-state.json"
PROVIDER_PREFIX = "ai-observatory-lmstudio-"
DUMMY_TOKEN = "lm-studio"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}\Z")
BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/\-]+=*\Z")


class HttpFailure(ValueError):
    def __init__(self, service: str, status: int) -> None:
        self.status = status
        super().__init__(f"{service} returned HTTP {status}.")


def request_json(
    url: str,
    *,
    service: str,
    authorization: str | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method="PUT" if body is not None else "GET",
    )
    # Neither ambient proxies nor redirects may receive project or model credentials.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError(f"{service} returned an oversized response.")
        data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise HttpFailure(service, exc.code) from None
    except (
        OSError,
        urllib.error.URLError,
        http.client.HTTPException,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        raise ValueError(f"Could not safely read {service}.") from None
    if not isinstance(data, dict):
        raise ValueError(f"{service} returned an unexpected response.")  # noqa: TRY004
    return data


def lms_json(*args: str) -> Any:
    env = os.environ.copy()
    env.pop("AI_OBSERVATORY_LM_STUDIO_TOKEN", None)
    try:
        result = subprocess.run(
            ["lms", *args, "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=20,
            check=True,
            env=env,
        )
        if len(result.stdout) > MAX_RESPONSE_BYTES:
            raise ValueError("LM Studio returned an oversized model list.")
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        raise ValueError(
            "LM Studio CLI is unavailable or returned invalid data."
        ) from None


def models() -> None:
    available = lms_json("ls")
    loaded = lms_json("ps")
    if not isinstance(available, list) or not isinstance(loaded, list):
        raise ValueError("LM Studio returned an unexpected model inventory.")  # noqa: TRY004
    loaded_keys = {item.get("modelKey") for item in loaded if isinstance(item, dict)}
    print("TYPE       MODEL ID                                      SIZE       STATUS")
    for item in available:
        if not isinstance(item, dict):
            raise ValueError("LM Studio returned an invalid model entry.")  # noqa: TRY004
        key = item.get("modelKey")
        kind = item.get("type")
        size = item.get("sizeBytes")
        if (
            not isinstance(key, str)
            or not MODEL_ID.fullmatch(key)
            or kind not in ("llm", "embedding", "decision")
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise ValueError("LM Studio returned an invalid model entry.")
        remote = item.get("deviceIdentifier") is not None
        status = (
            "linked device"
            if remote
            else ("loaded" if key in loaded_keys else "downloaded")
        )
        print(f"{kind:10} {key:45} {size / 1_000_000_000:6.2f} GB  {status}")
    print("Only currently available local language models are added to Langfuse.")


def verify_server() -> None:
    if sys.platform != "darwin":
        raise ValueError(
            "This integration currently requires macOS and a local Docker Desktop or OrbStack engine."
        )
    status = lms_json("server", "status")
    if (
        not isinstance(status, dict)
        or status.get("running") is not True
        or status.get("port") != 1234
    ):
        raise ValueError("Start the LM Studio API server on 127.0.0.1:1234 first.")
    try:
        env = os.environ.copy()
        env.pop("AI_OBSERVATORY_LM_STUDIO_TOKEN", None)
        result = subprocess.run(
            ["lsof", "-nP", "-iTCP:1234", "-sTCP:LISTEN", "-F", "n"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=True,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Could not verify the LM Studio listening address.") from None
    addresses = {
        line[1:] for line in result.stdout.splitlines() if line.startswith("n")
    }
    if addresses != {"127.0.0.1:1234"}:
        raise ValueError(
            "LM Studio must listen only on 127.0.0.1:1234; no settings were changed."
        )
    link = lms_json("link", "status")
    if not isinstance(link, dict) or link.get("status") != "offline":
        raise ValueError(
            "LM Link must be offline to avoid routing judge data to another device."
        )


def verify_docker_engine(config: dict[str, str]) -> None:
    """Do not send this checkout's stack or model data to a remote Docker host."""
    env = common.compose_environment(config)

    def docker_value(*args: str) -> str:
        try:
            result = subprocess.run(
                ["docker", *args],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=15,
                check=True,
                pass_fds=common.maintenance_fds(),
            )
        except (OSError, subprocess.SubprocessError):
            raise ValueError("Could not verify the local Docker engine.") from None
        return result.stdout.strip()

    endpoint = env.get("DOCKER_HOST") or docker_value(
        "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"
    )
    if not endpoint.startswith("unix:///") or "?" in endpoint or "#" in endpoint:
        raise ValueError(
            "LM Studio requires a local Unix-socket Docker engine, not a remote host."
        )
    operating_system = docker_value("info", "--format", "{{.OperatingSystem}}")
    if operating_system not in ("Docker Desktop", "OrbStack"):
        raise ValueError(
            "LM Studio requires a local Docker Desktop or OrbStack engine on macOS."
        )


def model_token() -> tuple[str, bool]:
    token = os.environ.get("AI_OBSERVATORY_LM_STUDIO_TOKEN")
    if token:
        if len(token) > 4096 or not BEARER_TOKEN.fullmatch(token):
            raise ValueError("Invalid LM Studio API token.")
        request_json(
            f"{HOST}/api/v1/models",
            service="LM Studio",
            authorization=f"Bearer {token}",
        )
        return token, True
    try:
        request_json(f"{HOST}/api/v1/models", service="LM Studio")
        return DUMMY_TOKEN, False
    except HttpFailure as exc:
        if exc.status != 401:
            raise
    if not sys.stdin.isatty():
        raise ValueError(
            "LM Studio requires a token; run this command in a terminal or set AI_OBSERVATORY_LM_STUDIO_TOKEN privately."
        )
    token = getpass.getpass("LM Studio API token: ")
    if not token or len(token) > 4096 or not BEARER_TOKEN.fullmatch(token):
        raise ValueError("Invalid LM Studio API token.")
    request_json(
        f"{HOST}/api/v1/models", service="LM Studio", authorization=f"Bearer {token}"
    )
    return token, True


def available_language_models(token: str) -> list[str]:
    authorization = f"Bearer {token}"
    rich = request_json(
        f"{HOST}/api/v1/models", service="LM Studio", authorization=authorization
    )
    compatible = request_json(
        f"{HOST}/v1/models", service="LM Studio", authorization=authorization
    )
    rich_models, wire_models = rich.get("models"), compatible.get("data")
    if not isinstance(rich_models, list) or not isinstance(wire_models, list):
        raise ValueError("LM Studio returned an unexpected model inventory.")  # noqa: TRY004
    local_ids: set[str] = set()
    for item in rich_models:
        if not isinstance(item, dict) or item.get("type") != "llm":
            continue
        candidates = [item.get("key")]
        variants = item.get("variants", [])
        instances = item.get("loaded_instances", [])
        if not isinstance(variants, list) or not isinstance(instances, list):
            raise ValueError("LM Studio returned an invalid language model.")  # noqa: TRY004
        candidates.extend(variants)
        candidates.extend(i.get("id") for i in instances if isinstance(i, dict))
        for candidate in candidates:
            if candidate is not None:
                if not isinstance(candidate, str) or not MODEL_ID.fullmatch(candidate):
                    raise ValueError("LM Studio returned an invalid model identifier.")
                local_ids.add(candidate)
    exposed = set()
    for item in wire_models:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("LM Studio returned an invalid OpenAI model identifier.")  # noqa: TRY004
        identifier = item["id"]
        if not MODEL_ID.fullmatch(identifier):
            raise ValueError("LM Studio returned an invalid model identifier.")
        exposed.add(identifier)
    result = sorted(local_ids & exposed)
    if not result or len(result) > 512:
        raise ValueError(
            "No available language models; load one or enable LM Studio just-in-time loading."
        )
    return result


def project_credentials(config: dict[str, str]) -> tuple[str, str]:
    base = f"http://127.0.0.1:{config['WEB_PORT']}"
    path = private_config_path()
    public = secret = None
    if path.exists() or path.is_symlink():
        credentials = read_private_json(path)
        if credentials["baseUrl"] == base:
            public, secret = credentials["publicKey"], credentials["secretKey"]
        else:
            print("OpenCode credentials target a different URL and were not reused.")
    if public is None or secret is None:
        if not sys.stdin.isatty():
            raise ValueError(
                "No matching local project credentials; run this command in a terminal and enter a Langfuse project key."
            )
        public = getpass.getpass("Langfuse project public key: ")
        secret = getpass.getpass("Langfuse project secret key: ")
    if not re.fullmatch(r"pk-lf-[A-Za-z0-9_-]+", public) or not re.fullmatch(
        r"sk-lf-[A-Za-z0-9_-]+", secret
    ):
        raise ValueError("Invalid Langfuse project key format.")
    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
    return base, f"Basic {auth}"


def project_id(base: str, auth: str) -> str:
    data = request_json(
        f"{base}/api/public/projects", service="Langfuse", authorization=auth
    )
    projects = data.get("data")
    if (
        not isinstance(projects, list)
        or len(projects) != 1
        or not isinstance(projects[0], dict)
    ):
        raise ValueError("A project-scoped Langfuse API key is required.")
    identifier = projects[0].get("id")
    if not isinstance(identifier, str) or not identifier or len(identifier) > 512:
        raise ValueError("Langfuse returned an invalid project identity.")
    return identifier


def connections(base: str, auth: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for page in range(1, 101):
        data = request_json(
            f"{base}/api/public/llm-connections?limit=100&page={page}",
            service="Langfuse",
            authorization=auth,
        )
        rows, meta = data.get("data"), data.get("meta")
        if (
            not isinstance(rows, list)
            or any(not isinstance(row, dict) for row in rows)
            or not isinstance(meta, dict)
        ):
            raise ValueError("Langfuse returned an invalid connection list.")
        total = meta.get("totalPages")
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
            or total > 100
        ):
            raise ValueError("Langfuse returned invalid pagination.")
        result.extend(rows)
        if page >= total:
            return result
    raise ValueError("Langfuse returned too many connection pages.")


def connection_fingerprint(connection: dict[str, Any]) -> str:
    fields = (
        "id",
        "provider",
        "adapter",
        "baseURL",
        "customModels",
        "withDefaultModels",
        "extraHeaderKeys",
        "config",
        "displaySecretKey",
        "updatedAt",
    )
    if any(field not in connection for field in fields):
        raise ValueError("Langfuse returned an incomplete connection.")
    serialized = json.dumps(
        {key: connection[key] for key in fields}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def validate_connection(connection: dict[str, Any], provider: str) -> list[str]:
    if (
        connection.get("provider") != provider
        or connection.get("adapter") != "openai"
        or connection.get("baseURL") != GATEWAY
        or connection.get("withDefaultModels") is not False
        or connection.get("extraHeaderKeys") != []
        or connection.get("config") is not None
        or not isinstance(connection.get("id"), str)
        or not connection["id"]
    ):
        raise ValueError(
            "The managed LM Studio connection was modified; it was not overwritten."
        )
    names = connection.get("customModels")
    if not isinstance(names, list) or any(
        not isinstance(name, str) or not MODEL_ID.fullmatch(name) for name in names
    ):
        raise ValueError(
            "The managed LM Studio connection contains invalid models; it was not overwritten."
        )
    return names


def read_state() -> tuple[dict[str, Any] | None, bytes | None]:
    path = common.ROOT / STATE_NAME
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None, None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 8192
        ):
            raise ValueError("LM Studio state is not private and safe.")
        raw = stream.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("LM Studio state is invalid; it was not changed.") from None
    if (
        not isinstance(data, dict)
        or set(data)
        != {
            "version",
            "projectId",
            "provider",
            "connectionId",
            "fingerprint",
            "usesToken",
        }
        or data["version"] != 1
        or not isinstance(data["projectId"], str)
        or not re.fullmatch(
            r"ai-observatory-lmstudio-[0-9a-f]{12}", str(data["provider"])
        )
        or (
            data["connectionId"] is not None
            and not isinstance(data["connectionId"], str)
        )
        or (
            data["fingerprint"] is not None
            and not re.fullmatch(r"[0-9a-f]{64}", str(data["fingerprint"]))
        )
        or ((data["connectionId"] is None) != (data["fingerprint"] is None))
        or (data["usesToken"] is not None and not isinstance(data["usesToken"], bool))
        or ((data["connectionId"] is None) != (data["usesToken"] is None))
    ):
        raise ValueError("LM Studio state is invalid; it was not changed.")
    return data, raw


def write_state(state: dict[str, Any], previous: bytes | None) -> None:
    path = common.ROOT / STATE_NAME
    content = (json.dumps(state, sort_keys=True) + "\n").encode()
    if previous is None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        return
    _, current = read_state()
    if current != previous:
        raise ValueError(
            "LM Studio state changed concurrently; it was not overwritten."
        )
    fd, name = tempfile.mkstemp(prefix=".ai-observatory-lmstudio-", dir=common.ROOT)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        _, current = read_state()
        if current != previous:
            raise ValueError(
                "LM Studio state changed concurrently; it was not overwritten."
            )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def verify_stack_environment(config: dict[str, str]) -> None:
    code = """const names=['LANGFUSE_LLM_CONNECTION_ALLOWED_HOSTS','LANGFUSE_LLM_CONNECTION_WHITELISTED_IPS','LANGFUSE_LLM_CONNECTION_WHITELISTED_IP_SEGMENTS'];
if(names.some(n=>process.env[n]))process.exit(2);
const host=process.env.LANGFUSE_LLM_CONNECTION_WHITELISTED_HOST;
if(host && host!=='lmstudio-gateway')process.exit(3);"""
    for service in ("web", "worker"):
        result = subprocess.run(
            common.compose_command(config, "exec", "-T", service, "node", "-e", code),
            env=common.compose_environment(config),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            pass_fds=common.maintenance_fds(),
            check=False,
        )
        if result.returncode:
            raise ValueError(
                f"{service} is unavailable or has custom LLM network restrictions; no settings were overwritten."
            )


def verify_gateway_source() -> None:
    for path in (
        common.ROOT / "integrations",
        common.ROOT / "integrations" / "lmstudio",
        common.ROOT / "integrations" / "lmstudio" / "gateway.cjs",
    ):
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            raise ValueError(
                "LM Studio gateway source must be owned and not writable by others."
            )
        if path.name == "gateway.cjs":
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("LM Studio gateway source must be a regular file.")
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("LM Studio gateway parent must be a directory.")


def reject_unmanaged_gateway(config: dict[str, str]) -> None:
    if common.lmstudio_enabled():
        return
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--all",
            "--filter",
            f"label=com.docker.compose.project={config['COMPOSE_PROJECT_NAME']}",
            "--filter",
            "label=com.docker.compose.service=lmstudio-gateway",
            "--format",
            "{{.ID}}",
        ],
        env=common.compose_environment(config),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=15,
        check=True,
        pass_fds=common.maintenance_fds(),
    )
    if result.stdout.strip():
        raise ValueError(
            "An unmanaged LM Studio gateway already exists; it was not changed."
        )


def verify_gateway(config: dict[str, str], token: str) -> None:
    code = """if(process.env.LANGFUSE_LLM_CONNECTION_WHITELISTED_HOST!=='lmstudio-gateway')process.exit(2);
let token='';process.stdin.setEncoding('utf8');process.stdin.on('data',c=>token+=c);process.stdin.on('end',()=>{
const r=require('node:http').get({hostname:'lmstudio-gateway',port:8787,path:'/v1/models',timeout:8000,headers:{authorization:'Bearer '+token}},res=>{res.resume();res.on('end',()=>process.exit(res.statusCode===200?0:1));});
r.on('timeout',()=>r.destroy());r.on('error',()=>process.exit(1));});"""
    for service in ("web", "worker"):
        result = subprocess.run(
            common.compose_command(config, "exec", "-T", service, "node", "-e", code),
            env=common.compose_environment(config),
            input=token,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            pass_fds=common.maintenance_fds(),
            check=False,
        )
        if result.returncode:
            raise ValueError(
                f"{service} could not reach LM Studio through the gateway."
            )


def sync_connection(
    base: str,
    auth: str,
    state: dict[str, Any],
    previous: bytes,
    token: str,
    authenticated: bool,
    available: list[str],
) -> None:
    current = [
        row
        for row in connections(base, auth)
        if row.get("provider") == state["provider"]
    ]
    if len(current) > 1:
        raise ValueError("Duplicate managed connections found; nothing was changed.")
    if state["connectionId"] is None:
        if current:
            raise ValueError(
                "An untracked connection already uses the reserved name; it was not overwritten."
            )
        merged = available
        added = available
    else:
        if not current or current[0].get("id") != state["connectionId"]:
            raise ValueError(
                "The managed connection is missing or was replaced; nothing was changed."
            )
        existing = current[0]
        old = validate_connection(existing, state["provider"])
        if connection_fingerprint(existing) != state["fingerprint"]:
            raise ValueError(
                "The managed connection changed outside this script; it was not overwritten."
            )
        # A masked suffix cannot distinguish a real token ending in "udio" from
        # our placeholder. Track the credential mode rather than guessing.
        if not authenticated and state["usesToken"]:
            raise ValueError(
                "The managed connection uses an API token; provide AI_OBSERVATORY_LM_STUDIO_TOKEN to preserve it."
            )
        added = [name for name in available if name not in old]
        merged = old + added
        if not added and not authenticated:
            print(f"LM Studio connection is up to date ({len(old)} language models).")
            return
    payload = {
        "provider": state["provider"],
        "adapter": "openai",
        "secretKey": token,
        "baseURL": GATEWAY,
        "customModels": merged,
        "withDefaultModels": False,
    }
    updated = request_json(
        f"{base}/api/public/llm-connections",
        service="Langfuse",
        authorization=auth,
        body=payload,
    )
    actual = validate_connection(updated, state["provider"])
    if actual != merged or (
        state["connectionId"] is not None and updated["id"] != state["connectionId"]
    ):
        raise ValueError(
            "Langfuse returned an unexpected connection; inspect it before retrying."
        )
    state = {
        **state,
        "connectionId": updated["id"],
        "fingerprint": connection_fingerprint(updated),
        "usesToken": authenticated,
    }
    write_state(state, previous)
    print(
        f"LM Studio connection ready: {len(actual)} language models ({len(added)} added)."
    )


def integrate(action: str) -> None:
    if action not in ("connect", "sync"):
        raise ValueError("Unknown LM Studio action.")
    with common.maintenance_lock():
        config = common.load_config()
        verify_server()
        verify_docker_engine(config)
        token, authenticated = model_token()
        available = available_language_models(token)
        base, auth = project_credentials(config)
        project = project_id(base, auth)
        state, raw = read_state()
        if state is not None and state["projectId"] != project:
            raise ValueError(
                "LM Studio state belongs to a different Langfuse project; nothing was changed."
            )
        current = connections(base, auth)
        verify_stack_environment(config)
        verify_gateway_source()
        reject_unmanaged_gateway(config)
        if state is None:
            if action == "sync":
                raise ValueError(
                    "No managed LM Studio connection; run make connect-lmstudio."
                )
            existing = {row.get("provider") for row in current}
            provider = PROVIDER_PREFIX + secrets.token_hex(6)
            if provider in existing:
                raise ValueError(
                    "Generated connection name is already in use; retry safely."
                )
            state = {
                "version": 1,
                "projectId": project,
                "provider": provider,
                "connectionId": None,
                "fingerprint": None,
                "usesToken": None,
            }
            write_state(state, None)
            _, raw = read_state()
        elif state["connectionId"] is None and any(
            row.get("provider") == state["provider"] for row in current
        ):
            raise ValueError(
                "An interrupted setup may have created a connection; inspect it before retrying."
            )
        elif state["connectionId"] is not None:
            owned = [row for row in current if row.get("provider") == state["provider"]]
            if len(owned) != 1 or owned[0].get("id") != state["connectionId"]:
                raise ValueError(
                    "The managed connection is missing or was replaced; nothing was changed."
                )
            validate_connection(owned[0], state["provider"])
            if connection_fingerprint(owned[0]) != state["fingerprint"]:
                raise ValueError(
                    "The managed connection changed outside this script; it was not overwritten."
                )
        if raw is None:
            raise ValueError("Could not read LM Studio state.")
        if action == "connect":
            if not common.lmstudio_enabled():
                marker = common.ROOT / common.LMSTUDIO_MARKER
                write_new(marker, common.LMSTUDIO_MARKER_CONTENT.decode())
            print(
                "Enabling the private gateway; Langfuse web and worker may briefly restart."
            )
            common.run_compose(
                config,
                "up",
                "--detach",
                "--wait",
                "--wait-timeout",
                "600",
                "--no-deps",
                "lmstudio-gateway",
                "web",
                "worker",
            )
        elif not common.lmstudio_enabled():
            raise ValueError(
                "LM Studio gateway is not enabled; run make connect-lmstudio."
            )
        verify_gateway(config, token)
        sync_connection(base, auth, state, raw, token, authenticated, available)
        if not authenticated:
            print(
                "LM Studio authentication is off: other local users and Docker workloads may access its API. Enable an API token for stronger protection."
            )
        print(
            "No evaluator or project default was changed. Select this connection when configuring an evaluator."
        )
        print(f"Langfuse provider: {state['provider']}")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Expected models, connect, or sync.")
        if sys.argv[1] == "models":
            models()
        else:
            integrate(sys.argv[1])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"LM Studio integration failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
