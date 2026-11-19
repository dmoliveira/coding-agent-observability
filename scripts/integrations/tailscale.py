"""Opt-in Tailscale Serve using conditional updates to preserve other routes."""

from __future__ import annotations

import base64
import copy
import http.client
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import CONFIG, load_config, maintenance_lock, run_compose

WEB_PORT = 15443
MEDIA_PORT = 15444
API_PATH = "/localapi/v0/serve-config"


class ConcurrentUpdate(ValueError):
    """A conditional Tailscale update was definitely not applied."""


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("local-tailscaled.sock", timeout=10)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class LocalAPI:
    """Use Tailscale's local credentials without printing or persisting them."""

    def __init__(self) -> None:
        try:
            raw = subprocess.check_output(
                ["tailscale", "debug", "local-creds"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            args = shlex.split(raw)
            if len(args) == 4 and args[:2] == ["curl", "--unix-socket"]:
                self.socket_path = args[2]
                self.port = None
                self.authorization = None
            elif len(args) == 3 and args[0] == "curl" and args[1].startswith("-u:"):
                target = urlsplit(args[2])
                if (
                    target.scheme != "http"
                    or target.hostname != "localhost"
                    or not target.port
                ):
                    raise ValueError("Unexpected Tailscale local API endpoint.")
                token = args[1][3:]
                if not token:
                    raise ValueError("Missing Tailscale local API credential.")
                self.port = target.port
                self.socket_path = None
                encoded = base64.b64encode(f":{token}".encode()).decode()
                self.authorization = f"Basic {encoded}"
            else:
                raise ValueError("Unsupported Tailscale local API transport.")
        except (OSError, subprocess.SubprocessError):
            raise ValueError("Could not access the Tailscale local API.") from None

    def request(
        self, method: str, body: dict | None = None, etag: str | None = None
    ) -> tuple[dict, str]:
        if method == "POST" and not etag:
            raise ValueError(
                "A Tailscale configuration version is required for updates."
            )
        connection = (
            UnixConnection(self.socket_path)
            if self.socket_path
            else http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        )
        headers = {"Content-Type": "application/json"}
        if self.authorization:
            headers["Authorization"] = self.authorization
        if etag:
            headers["If-Match"] = etag
        try:
            connection.request(
                method,
                API_PATH,
                body=json.dumps(body) if body is not None else None,
                headers=headers,
            )
            response = connection.getresponse()
            data = response.read()
            if response.status == 412:
                raise ConcurrentUpdate(
                    "Tailscale configuration changed concurrently; nothing was overwritten."
                )
            if response.status != 200:
                raise ValueError(
                    f"Tailscale local API returned HTTP {response.status}."
                )
            if method == "GET":
                version = response.getheader("ETag")
                if not version:
                    raise ValueError(
                        "Tailscale did not provide a configuration version; refusing changes."
                    )
                value = json.loads(data) if data else {}
                if value is None:
                    value = {}
                if not isinstance(value, dict):
                    raise ValueError("Unexpected Tailscale configuration format.")
                return value, version
            return {}, ""
        except (OSError, http.client.HTTPException, json.JSONDecodeError):
            raise ValueError(
                "Tailscale local API request failed; inspect Serve status before retrying."
            ) from None
        finally:
            connection.close()


def tailscale_hostname() -> str:
    try:
        raw = subprocess.check_output(
            ["tailscale", "status", "--json"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        status = json.loads(raw)
        host = status["Self"]["DNSName"].rstrip(".").lower()
        domains = [item.rstrip(".").lower() for item in status.get("CertDomains", [])]
    except (
        OSError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ):
        raise ValueError(
            "Tailscale status is unavailable; sign in and check MagicDNS."
        ) from None
    if status.get("BackendState") != "Running" or not re.fullmatch(
        r"[a-z0-9.-]+\.ts\.net", host
    ):
        raise ValueError("Tailscale must be connected with a valid MagicDNS hostname.")
    if host not in domains:
        raise ValueError(
            "Enable Tailscale HTTPS certificates in your tailnet before sharing."
        )
    return host


def port_used(config: dict, port: int) -> bool:
    for field in ("TCP", "Web", "AllowFunnel"):
        entries = config.get(field) or {}
        if not isinstance(entries, dict):
            raise ValueError("Unrecognized Tailscale configuration; refusing changes.")  # noqa: TRY004
        for key in entries:
            if (field == "TCP" and str(key) == str(port)) or (
                field != "TCP" and str(key).endswith(f":{port}")
            ):
                return True
    for field in ("Foreground", "Services"):
        children = config.get(field) or {}
        if not isinstance(children, dict):
            raise ValueError("Unrecognized Tailscale configuration; refusing changes.")  # noqa: TRY004
        for child in children.values():
            if not isinstance(child, dict) or port_used(child, port):
                return True
    return False


def route(target_port: str) -> dict:
    return {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{target_port}"}}}


def add_routes(config: dict, hostname: str, local: dict[str, str]) -> dict:
    if any(port_used(config, port) for port in (WEB_PORT, MEDIA_PORT)):
        raise ValueError(
            "Tailscale port 15443 or 15444 is already in use; no route was changed."
        )
    result = copy.deepcopy(config)
    tcp = result.setdefault("TCP", {})
    web = result.setdefault("Web", {})
    if not isinstance(tcp, dict) or not isinstance(web, dict):
        raise ValueError("Unrecognized Tailscale configuration; refusing changes.")  # noqa: TRY004
    for port, target in (
        (WEB_PORT, local["WEB_PORT"]),
        (MEDIA_PORT, local["MEDIA_PORT"]),
    ):
        tcp[str(port)] = {"HTTPS": True}
        web[f"{hostname}:{port}"] = route(target)
    return result


def remove_routes(config: dict, hostname: str, local: dict[str, str]) -> dict:
    result = copy.deepcopy(config)
    tcp = result.get("TCP") or {}
    web = result.get("Web") or {}
    funnel = result.get("AllowFunnel") or {}
    if not all(isinstance(item, dict) for item in (tcp, web, funnel)):
        raise ValueError("Unrecognized Tailscale configuration; refusing changes.")
    children: list[dict] = []
    for field in ("Foreground", "Services"):
        entries = result.get(field) or {}
        if not isinstance(entries, dict) or any(
            not isinstance(item, dict) for item in entries.values()
        ):
            raise ValueError("Unrecognized Tailscale configuration; refusing changes.")
        children.extend(entries.values())
    for port, target in (
        (WEB_PORT, local["WEB_PORT"]),
        (MEDIA_PORT, local["MEDIA_PORT"]),
    ):
        key = f"{hostname}:{port}"
        shared_web = any(name != key and str(name).endswith(f":{port}") for name in web)
        shared_funnel = any(str(name).endswith(f":{port}") for name in funnel)
        shared_nested = any(port_used(child, port) for child in children)
        if (
            tcp.get(str(port)) != {"HTTPS": True}
            or web.get(key) != route(target)
            or shared_web
            or shared_funnel
            or shared_nested
        ):
            raise ValueError(
                "A Tailscale route changed or is public; no route was removed."
            )
    for port in (WEB_PORT, MEDIA_PORT):
        del tcp[str(port)]
        del web[f"{hostname}:{port}"]
    return result


def replace_config(old: dict[str, str], new: dict[str, str]) -> None:
    expected = "".join(f"{key}={value}\n" for key, value in old.items())
    if CONFIG.is_symlink() or CONFIG.read_text(encoding="utf-8") != expected:
        raise ValueError(
            "Configuration changed concurrently; refusing to overwrite it."
        )
    fd, name = tempfile.mkstemp(prefix=".env.", dir=CONFIG.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.writelines(f"{key}={value}\n" for key, value in new.items())
        os.replace(name, CONFIG)
    finally:
        Path(name).unlink(missing_ok=True)


def require_running(config: dict[str, str]) -> None:
    result = run_compose(
        config,
        "ps",
        "--services",
        "--filter",
        "status=running",
        capture_output=True,
        text=True,
    )
    if not {"web", "worker"}.issubset(set(result.stdout.splitlines())):
        raise ValueError(
            "Start the local stack with make up before enabling Tailscale."
        )


def enable() -> None:
    local = load_config()
    if local["NEXTAUTH_URL"].startswith("https://"):
        raise ValueError(
            "Tailscale mode is already configured; run make tailscale-off first."
        )
    require_running(local)
    host = tailscale_hostname()
    api = LocalAPI()
    current, etag = api.request("GET")
    updated = add_routes(current, host, local)
    remote = dict(local)
    remote["NEXTAUTH_URL"] = f"https://{host}:{WEB_PORT}"
    remote["MEDIA_URL"] = f"https://{host}:{MEDIA_PORT}"
    replace_config(local, remote)
    try:
        run_compose(remote, "up", "--detach", "--wait", "--wait-timeout", "600")
    except (OSError, ValueError, subprocess.CalledProcessError):
        replace_config(remote, local)
        print(
            "Configuration restored to localhost. Run make down, then make up to recover.",
            file=sys.stderr,
        )
        raise
    try:
        api.request("POST", updated, etag)
    except ConcurrentUpdate:
        replace_config(remote, local)
        try:
            run_compose(local, "up", "--detach", "--wait", "--wait-timeout", "600")
        except (OSError, subprocess.CalledProcessError):
            print(
                "Configuration is localhost; run make up to recover the server.",
                file=sys.stderr,
            )
            raise
        print(
            "Concurrent Tailscale change rejected; localhost configuration restored.",
            file=sys.stderr,
        )
        raise
    except ValueError:
        # A transport failure can leave the outcome unknown; never blindly replace shared Serve state.
        print(
            "Check tailscale serve status before retrying; the remote configuration may be active.",
            file=sys.stderr,
        )
        raise
    print(f"Available only to permitted tailnet devices: {remote['NEXTAUTH_URL']}")
    print(
        "Review tailnet grants for both ports. Run make tailscale-off to stop sharing."
    )


def disable(*, restart: bool = True) -> None:
    remote = load_config()
    if not remote["NEXTAUTH_URL"].startswith("https://"):
        print("Tailscale sharing is not configured for this instance.")
        return
    host = tailscale_hostname()
    if urlsplit(remote["NEXTAUTH_URL"]).hostname != host:
        raise ValueError(
            "The configured Tailscale hostname changed; refusing to remove routes."
        )
    api = LocalAPI()
    current, etag = api.request("GET")
    if any(port_used(current, port) for port in (WEB_PORT, MEDIA_PORT)):
        updated = remove_routes(current, host, remote)
        api.request("POST", updated, etag)
    else:
        # A previous conditional update may have failed or succeeded without
        # returning a response. An entirely unused pair needs no API change.
        print("Neither Tailscale route is present; restoring localhost configuration.")
    local = dict(remote)
    local["NEXTAUTH_URL"] = f"http://127.0.0.1:{local['WEB_PORT']}"
    local["MEDIA_URL"] = f"http://127.0.0.1:{local['MEDIA_PORT']}"
    replace_config(remote, local)
    if restart:
        run_compose(local, "up", "--detach", "--wait", "--wait-timeout", "600")
    print("Tailscale routes removed; this instance is configured for localhost only.")


def main(action: str) -> None:
    with maintenance_lock():
        if action == "on":
            enable()
        elif action == "off":
            disable()
        else:
            raise ValueError("Expected on or off.")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ("on", "off"):
            raise ValueError("Expected on or off.")
        main(sys.argv[1])
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Tailscale command failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
