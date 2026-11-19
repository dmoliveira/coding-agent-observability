"""Install the private, default-on OpenCode integration without changing its config."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import CONFIG, ROOT, load_config

from integrations.opencode import private_directory, validate_url, verify_key, write_new

SOURCE = ROOT / "integrations" / "opencode-global"
SOURCE_FILES = ("index.js", "package.json", "package-lock.json", "prepare.js")
MARKER = ".ai-observatory-install.json"


def validate_global_url(value: str) -> str:
    validate_url(value)
    url = urlsplit(value)
    if url.port in (80, 443) or value != f"{url.scheme}://{url.hostname}:{url.port}":
        raise ValueError(
            "Langfuse URL must use a canonical host and explicit non-default port."
        )
    return value


def config_root() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    if not root.is_absolute():
        raise ValueError("XDG_CONFIG_HOME must be an absolute path.")
    return root


def private_config_path() -> Path:
    return config_root() / "ai-observatory" / "opencode-global.json"


def read_private_json(path: Path) -> dict[str, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, encoding="utf-8") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
                or metadata.st_size > 8192
            ):
                raise ValueError("OpenCode credentials file is not private and safe.")
            value = json.load(stream)
    except (OSError, json.JSONDecodeError):
        raise ValueError("Could not safely read OpenCode credentials.") from None
    if not isinstance(value, dict) or set(value) != {
        "publicKey",
        "secretKey",
        "baseUrl",
    }:
        raise ValueError("Invalid OpenCode credentials file.")
    if not all(isinstance(item, str) for item in value.values()):
        raise ValueError("Invalid OpenCode credentials file.")
    if not re.fullmatch(r"pk-lf-[A-Za-z0-9_-]+", value["publicKey"]):
        raise ValueError("Invalid Langfuse public key format.")
    if not re.fullmatch(r"sk-lf-[A-Za-z0-9_-]+", value["secretKey"]):
        raise ValueError("Invalid Langfuse secret key format.")
    validate_global_url(value["baseUrl"])
    return value


def safe_opencode_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"OpenCode plugin directory must not be a symlink: {path}")
    path.mkdir(parents=True, mode=0o755, exist_ok=True)
    info = path.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise ValueError(
            f"OpenCode plugin directory must be owned and not writable by others: {path}"
        )


def file_hashes(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Unexpected symlink in managed plugin: {path}")
        if path.is_file() and path.name != MARKER:
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        elif not path.is_file() and not path.is_dir():
            raise ValueError(f"Unexpected file in managed plugin: {path}")
    return result


def source_hashes() -> dict[str, str]:
    return {
        name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest()
        for name in SOURCE_FILES
    }


def managed_installation(path: Path) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    try:
        marker = json.loads((path / MARKER).read_text(encoding="utf-8"))
        return (
            marker.get("version") == 1
            and marker.get("source") == source_hashes()
            and marker.get("files") == file_hashes(path)
        )
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        return False


def make_plugin(path: Path) -> None:
    for name in SOURCE_FILES:
        shutil.copyfile(SOURCE / name, path / name)
    for command in (
        ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        ["npm", "run", "prepare-instrumentation"],
    ):
        try:
            subprocess.run(
                command,
                cwd=path,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise ValueError(
                "Could not prepare the pinned OpenCode plugin; check Node.js, npm, and network access."
            ) from None
    marker = {"version": 1, "source": source_hashes(), "files": file_hashes(path)}
    write_new(path / MARKER, json.dumps(marker, sort_keys=True) + "\n")


def install_plugin(home: Path) -> None:
    managed = home / "ai-observatory"
    private_directory(managed)
    opencode = home / "opencode"
    plugins = opencode / "plugins"
    safe_opencode_directory(opencode)
    safe_opencode_directory(plugins)
    entry = plugins / "ai-observatory.js"

    def entry_content(target: Path) -> str:
        return (
            f"export {{ default }} from {json.dumps((target / 'index.js').as_uri())};\n"
        )

    if entry.exists() or entry.is_symlink():
        if not entry.is_symlink() and entry.is_file():
            content = entry.read_text(encoding="utf-8")
            for target in managed.glob("opencode-plugin-*"):
                if content == entry_content(target) and managed_installation(target):
                    return
        raise ValueError(f"Existing OpenCode plugin entry was left unchanged: {entry}")

    # Each build gets a unique private directory. Interrupted builds are never
    # published and do not block a retry or overwrite another installation.
    target = Path(tempfile.mkdtemp(prefix="opencode-plugin-", dir=managed))
    make_plugin(target)
    content = entry_content(target)
    # A hard link publishes the complete file atomically and never replaces a user file.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=".ai-observatory-",
        suffix=".tmp",
        dir=plugins,
        delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(content)
    try:
        os.link(temporary, entry, follow_symlinks=False)
    finally:
        temporary.unlink()


def connect() -> None:
    home = config_root()
    path = private_config_path()
    private_directory(path.parent)
    if path.exists() or path.is_symlink():
        credentials = read_private_json(path)
        requested_url = os.environ.get("AI_OBSERVATORY_URL")
        if (
            requested_url
            and validate_global_url(requested_url) != credentials["baseUrl"]
        ):
            raise ValueError(
                "Existing global credentials use a different Langfuse destination; they were not changed."
            )
    else:
        default_url = (
            load_config()["NEXTAUTH_URL"]
            if CONFIG.exists()
            else "http://127.0.0.1:3000"
        )
        base_url = validate_global_url(
            os.environ.get("AI_OBSERVATORY_URL", default_url)
        )
        if not sys.stdin.isatty():
            raise ValueError(
                "No private credentials exist; create a Langfuse project key and run this command in a terminal."
            )
        print(f"Langfuse destination: {base_url}")
        public = getpass.getpass("Langfuse public key: ")
        secret = getpass.getpass("Langfuse secret key: ")
        if not re.fullmatch(r"pk-lf-[A-Za-z0-9_-]+", public) or not re.fullmatch(
            r"sk-lf-[A-Za-z0-9_-]+", secret
        ):
            raise ValueError("Invalid Langfuse key format.")
        verify_key(base_url, public, secret)
        write_new(
            path,
            json.dumps({"publicKey": public, "secretKey": secret, "baseUrl": base_url})
            + "\n",
        )
        credentials = read_private_json(path)
    verify_key(
        credentials["baseUrl"], credentials["publicKey"], credentials["secretKey"]
    )
    install_plugin(home)
    print("OpenCode is configured to trace by default for this user.")
    print(
        "To disable this adapter in a new standalone session: AI_OBSERVATORY_DISABLED=1 opencode --standalone"
    )
    print("Other configured telemetry exporters are not disabled.")
    print(
        "Traces can contain prompts, prior context, reasoning, source code and tool output."
    )


if __name__ == "__main__":
    try:
        connect()
    except (OSError, ValueError) as exc:
        print(f"OpenCode global integration failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
