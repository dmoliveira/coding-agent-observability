"""Shared configuration and Docker Compose helpers."""

from __future__ import annotations

import fcntl
import os
import re
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".env"
COMPOSE = ROOT / "compose.yaml"
LMSTUDIO_COMPOSE = ROOT / "compose.lmstudio.yaml"
LMSTUDIO_MARKER = ".ai-observatory.lmstudio"
LMSTUDIO_MARKER_CONTENT = b"ai-observatory-lmstudio-v1\n"
_LOCK_FD: int | None = None
_REPORT_LOCK_FD: int | None = None
CONFIG_KEYS = {
    "COMPOSE_PROJECT_NAME",
    "WEB_PORT",
    "MEDIA_PORT",
    "NEXTAUTH_URL",
    "MEDIA_URL",
    "ADMIN_EMAIL",
    "ADMIN_PASSWORD",
    "POSTGRES_PASSWORD",
    "CLICKHOUSE_PASSWORD",
    "REDIS_AUTH",
    "MINIO_ROOT_PASSWORD",
    "NEXTAUTH_SECRET",
    "SALT",
    "ENCRYPTION_KEY",
}


@contextmanager
def maintenance_lock() -> Iterator[None]:
    """Serialize this checkout's state-changing stack, sharing and backup commands."""
    global _LOCK_FD
    if _LOCK_FD is not None:
        raise ValueError("A maintenance lock is already held in this process.")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(ROOT / ".ai-observatory.lock", flags, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError(
                "Maintenance lock must be a private file owned by the current user."
            )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Another observatory maintenance command is running; retry when it finishes."
            ) from None
        _LOCK_FD = fd
        yield
    finally:
        _LOCK_FD = None
        # Do not explicitly unlock: a child may still be using this descriptor.
        os.close(fd)


@contextmanager
def report_query_lock() -> Iterator[None]:
    """Admit one structural report process per checkout without waiting."""
    global _REPORT_LOCK_FD
    if _REPORT_LOCK_FD is not None:
        raise ValueError("Another structural report query is already running.")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(ROOT / ".ai-observatory.report.lock", flags, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError(
                "Report lock must be a private file owned by the current user."
            )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                "Another structural report query is running in this checkout."
            ) from None
        _REPORT_LOCK_FD = fd
        yield
    finally:
        _REPORT_LOCK_FD = None
        os.close(fd)


def maintenance_fds() -> tuple[int, ...]:
    return (_LOCK_FD,) if _LOCK_FD is not None else ()


def load_config(path: Path = CONFIG) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Missing or unsafe configuration: {path}. Run make setup.")
    metadata = path.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError(
            "Configuration must be owned by the current user and private (mode 600)."
        )
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z_][A-Z_0-9]*", key) or key in result:
            raise ValueError("Invalid configuration; refusing to continue.")
        result[key] = value
    if not re.fullmatch(r"aiobs-[a-f0-9]{12}", result.get("COMPOSE_PROJECT_NAME", "")):
        raise ValueError("Invalid Compose project name; refusing to continue.")
    if set(result) != CONFIG_KEYS:
        raise ValueError(
            "Configuration has missing or unexpected fields; refusing to continue."
        )
    for key in ("WEB_PORT", "MEDIA_PORT"):
        if not result[key].isdigit() or not 1 <= int(result[key]) <= 65535:
            raise ValueError(f"Invalid {key}.")
    if result["WEB_PORT"] == result["MEDIA_PORT"]:
        raise ValueError("Web and media ports must differ.")
    for key in (
        "POSTGRES_PASSWORD",
        "CLICKHOUSE_PASSWORD",
        "REDIS_AUTH",
        "MINIO_ROOT_PASSWORD",
    ):
        if not re.fullmatch(r"[a-f0-9]{48}", result[key]):
            raise ValueError(f"Invalid {key} format.")
    for key in ("SALT", "NEXTAUTH_SECRET", "ENCRYPTION_KEY"):
        if not re.fullmatch(r"[a-f0-9]{64}", result[key]):
            raise ValueError(f"Invalid {key} format.")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+", result["ADMIN_EMAIL"]):
        raise ValueError("Invalid administrator email.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,}", result["ADMIN_PASSWORD"]):
        raise ValueError("Invalid administrator password format.")
    web = urlsplit(result["NEXTAUTH_URL"])
    media = urlsplit(result["MEDIA_URL"])
    if any(
        url.username or url.password or url.path or url.query or url.fragment
        for url in (web, media)
    ):
        raise ValueError("Invalid web or media URL.")
    local = (
        result["NEXTAUTH_URL"] == f"http://127.0.0.1:{result['WEB_PORT']}"
        and result["MEDIA_URL"] == f"http://127.0.0.1:{result['MEDIA_PORT']}"
    )
    tailnet = (
        web.scheme == media.scheme == "https"
        and web.hostname == media.hostname
        and web.hostname is not None
        and web.hostname.endswith(".ts.net")
        and web.port is not None
        and media.port is not None
        and web.port != media.port
    )
    if not (local or tailnet):
        raise ValueError("URLs must target loopback or one HTTPS Tailscale hostname.")
    return result


def lmstudio_enabled() -> bool:
    """Recognize only this checkout's private, explicitly enabled integration."""
    marker = ROOT / LMSTUDIO_MARKER
    try:
        fd = os.open(marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return False
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size != len(LMSTUDIO_MARKER_CONTENT)
            or stream.read() != LMSTUDIO_MARKER_CONTENT
        ):
            raise ValueError("LM Studio integration marker is unsafe or unrecognized.")
    return True


def compose_command(config: dict[str, str], *args: str) -> list[str]:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(CONFIG),
        "--file",
        str(COMPOSE),
    ]
    if lmstudio_enabled():
        command.extend(("--file", str(LMSTUDIO_COMPOSE)))
    return [
        *command,
        "--project-name",
        config["COMPOSE_PROJECT_NAME"],
        *args,
    ]


def bitwarden_password(config: dict[str, str]) -> str | None:
    """Resolve an optional bootstrap password without displaying vault output."""
    item = os.environ.get("AI_OBSERVATORY_BITWARDEN_ITEM")
    if not item:
        return None

    try:
        if not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            item,
        ):
            raise ValueError("Invalid item ID")
        result = subprocess.run(
            ["bw", "get", "password", item, "--nointeraction"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=True,
            timeout=15,
        )
        password = result.stdout
        if password.endswith("\r\n"):
            password = password[:-2]
        elif password.endswith("\n"):
            password = password[:-1]
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,}", password):
            raise ValueError("Invalid password format")
    except (OSError, subprocess.SubprocessError, ValueError):
        print(
            "Bitwarden lookup unavailable or invalid; using .env fallback. "
            "Check the CLI session, item ID and password format.",
            file=sys.stderr,
        )
        return None

    if password != config["ADMIN_PASSWORD"]:
        print(
            "Bitwarden password differs from .env; existing Langfuse logins "
            "are not changed by startup.",
            file=sys.stderr,
        )
    print("Using Bitwarden administrator password for initialization.", file=sys.stderr)
    return password


def compose_environment(
    config: dict[str, str], *, admin_password: str | None = None
) -> dict[str, str]:
    # Compose otherwise gives inherited shell variables priority over our .env.
    env = os.environ.copy()
    for key in (*config, "COMPOSE_FILE", "COMPOSE_ENV_FILES", "COMPOSE_PROFILES"):
        env.pop(key, None)
    for key in tuple(env):
        if key.startswith(("BW_", "BWS_", "BITWARDENCLI_")) or key in (
            "AI_OBSERVATORY_ADMIN_PASSWORD",
            "AI_OBSERVATORY_BITWARDEN_ITEM",
            "AI_OBSERVATORY_LM_STUDIO_TOKEN",
        ):
            env.pop(key)
    if admin_password is not None:
        env["ADMIN_PASSWORD"] = admin_password
    return env


def run_compose(
    config: dict[str, str], *args: str, **kwargs: object
) -> subprocess.CompletedProcess:
    marker = ROOT / ".ai-observatory.starting"
    admin_password = None
    if args and args[0] == "up":
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(marker, flags, 0o600)
        except FileExistsError:
            raise ValueError(
                "A previous startup did not complete; run make down before retrying."
            ) from None
        os.close(fd)
        admin_password = bitwarden_password(config)
    result = subprocess.run(
        compose_command(config, *args),
        env=compose_environment(config, admin_password=admin_password),
        check=True,
        pass_fds=maintenance_fds(),
        **kwargs,
    )
    if args and args[0] in ("up", "down"):
        marker.unlink(missing_ok=True)
    return result


def docker_engine_is_recent() -> bool:
    try:
        output = subprocess.check_output(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    match = re.match(r"(\d+)\.", output)
    return bool(match and int(match.group(1)) >= 28)
