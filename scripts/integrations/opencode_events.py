"""Install a separate opt-in OpenCode structural lifecycle event plugin."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import ROOT

from integrations import agent_dashboard_definition, lmstudio, opencode_global
from integrations.opencode import private_directory, verify_key, write_new
from integrations.span_dashboard import Client

SOURCE = ROOT / "integrations" / "opencode-events"
FILES = ("index.js", "package.json")
PREFIX = "opencode-structural-events-"
MARKER = ".ai-observatory-events-install.json"
ENVIRONMENT = agent_dashboard_definition.RUNTIME_ENVIRONMENT
NON_EVENT_TYPES = frozenset(
    {
        "SPAN",
        "GENERATION",
        "AGENT",
        "TOOL",
        "CHAIN",
        "RETRIEVER",
        "EVALUATOR",
        "EMBEDDING",
        "GUARDRAIL",
    }
)


def excludes_events(filters: Any) -> bool:
    """Accept only an explicit, recognizable AND filter that excludes our events."""
    if not isinstance(filters, list):
        return False
    for item in filters:
        if not isinstance(item, dict):
            continue
        column, operator, value, kind = (
            item.get(k) for k in ("column", "operator", "value", "type")
        )
        if column != "type":
            continue
        if (
            kind == "stringOptions"
            and operator == "any of"
            and isinstance(value, list)
            and value
            and all(isinstance(v, str) and v in NON_EVENT_TYPES for v in value)
        ):
            return True
    return False


def verify_rules(client: Client) -> None:
    """Do not trigger an existing enabled evaluator whose scope is uncertain."""
    cursor = None
    seen: set[str] = set()
    for _ in range(100):
        suffix = "?limit=100" + (
            "&cursor=" + urllib.parse.quote(cursor, safe="") if cursor else ""
        )
        page = client.request("GET", "/api/public/v2/evaluation-rules" + suffix)
        rows, meta = page.get("data"), page.get("meta")
        if not isinstance(rows, list) or not isinstance(meta, dict):
            raise ValueError("Could not safely inspect Langfuse evaluation rules.")  # noqa: TRY004
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("enabled"), bool):
                raise ValueError(  # noqa: TRY004
                    "A Langfuse evaluation rule has an unrecognized shape."
                )
            if row["enabled"] and not excludes_events(row.get("filter")):
                raise ValueError(
                    "An enabled Langfuse evaluation rule could process structural events; nothing was installed."
                )
        cursor = meta.get("cursor")
        if cursor is None:
            return
        if (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > 4096
            or cursor in seen
        ):
            raise ValueError("Langfuse returned invalid evaluation-rule pagination.")
        seen.add(cursor)
    raise ValueError("Too many evaluation-rule pages to inspect safely.")


def safe_installed(target: Path, *, require_current: bool = False) -> bool:
    try:
        info = target.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            return False
        if {item.name for item in target.iterdir()} != {*FILES, MARKER}:
            return False
        fd = os.open(target / MARKER, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > 4096
            ):
                return False
            marker = json.loads(stream.read(4097))
        if (
            not isinstance(marker, dict)
            or set(marker) != {"version", "files"}
            or marker["version"] != 1
        ):
            return False
        hashes = marker["files"]
        if not isinstance(hashes, dict) or set(hashes) != set(FILES):
            return False
        if any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in hashes.values()
        ):
            return False
        for name in FILES:
            fd = os.open(target / name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_mode & 0o077
                    or info.st_size > 1024 * 1024
                ):
                    return False
                content = stream.read(1024 * 1024 + 1)
                if hashlib.sha256(content).hexdigest() != hashes[name]:
                    return False
                if require_current and content != (SOURCE / name).read_bytes():
                    return False
        return True
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def marker_content(contents: dict[str, bytes]) -> str:
    return (
        json.dumps(
            {
                "version": 1,
                "files": {
                    name: hashlib.sha256(contents[name]).hexdigest() for name in FILES
                },
            },
            sort_keys=True,
        )
        + "\n"
    )


def entry_content(target: Path) -> str:
    return f"export {{ default }} from {json.dumps((target / 'index.js').as_uri())};\n"


def owned_entry(
    entry: Path, managed: Path, *, require_current: bool = False
) -> os.stat_result | None:
    try:
        fd = os.open(entry, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o022
                or info.st_size > 4096
            ):
                return None
            content = stream.read(4097).decode("utf-8")
        for target in managed.glob(PREFIX + "*"):
            if content == entry_content(target) and safe_installed(
                target, require_current=require_current
            ):
                return info
    except (OSError, UnicodeDecodeError):
        pass
    return None


def install(home: Path) -> None:
    managed = home / "ai-observatory"
    private_directory(managed)
    opencode = home / "opencode"
    plugins = opencode / "plugins"
    opencode_global.safe_opencode_directory(opencode)
    opencode_global.safe_opencode_directory(plugins)
    entry = plugins / "ai-observatory-events.js"

    if entry.exists() or entry.is_symlink():
        if owned_entry(entry, managed, require_current=True) is not None:
            return
        raise ValueError(
            "Existing OpenCode structural-events plugin was left unchanged."
        )
    target = Path(tempfile.mkdtemp(prefix=PREFIX, dir=managed))
    contents = {name: (SOURCE / name).read_bytes() for name in FILES}
    for name in FILES:
        write_new(target / name, contents[name].decode("utf-8"))
    write_new(target / MARKER, marker_content(contents))
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=".aiobs-events-",
        suffix=".tmp",
        dir=plugins,
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(entry_content(target))
    try:
        os.link(temporary, entry, follow_symlinks=False)
    finally:
        temporary.unlink()


def disconnect(home: Path) -> None:
    managed = home / "ai-observatory"
    opencode = home / "opencode"
    plugins = opencode / "plugins"
    entry = plugins / "ai-observatory-events.js"
    if not entry.exists() and not entry.is_symlink():
        print("No structural event plugin entry is installed.")
        return
    if not managed.is_dir() or managed.is_symlink():
        raise ValueError(
            "Existing OpenCode structural-events plugin was left unchanged."
        )
    private_directory(managed)
    opencode_global.safe_opencode_directory(opencode)
    opencode_global.safe_opencode_directory(plugins)
    info = owned_entry(entry, managed)
    if info is None:
        raise ValueError(
            "Existing OpenCode structural-events plugin was left unchanged."
        )
    current = entry.lstat()
    if (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size) != (
        current.st_dev,
        current.st_ino,
        current.st_mtime_ns,
        current.st_size,
    ):
        raise ValueError(
            "Structural event plugin entry changed concurrently; it was not removed."
        )
    entry.unlink()
    print(
        "Structural event plugin entry removed; private files and recorded data were preserved."
    )


def connect() -> None:
    path = opencode_global.private_config_path()
    credentials = opencode_global.read_private_json(path)
    base = credentials["baseUrl"]
    encoded = base64.b64encode(
        f"{credentials['publicKey']}:{credentials['secretKey']}".encode()
    ).decode()
    client = Client(base, f"Basic {encoded}")
    lmstudio.project_id(base, client.auth)
    verify_rules(client)
    verify_key(base, credentials["publicKey"], credentials["secretKey"])
    install(opencode_global.config_root())
    print(
        "OpenCode structural event capture is installed; a watched server may load it immediately."
    )
    print(
        "Only execution/compaction lifecycle metadata is sent; task success is not inferred."
    )
    print(
        "Events can be lost during disconnects or restarts; no historical backfill is performed."
    )


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ("connect", "disconnect"):
            raise ValueError("Expected connect or disconnect.")
        if sys.argv[1] == "connect":
            connect()
        else:
            disconnect(opencode_global.config_root())
    except (OSError, ValueError, KeyError) as exc:
        print(f"Structural event setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
