"""Create a fresh instance's secrets, once, without exposing them in arguments."""

from __future__ import annotations

import getpass
import os
import re
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import CONFIG, load_config


def initialize() -> int:
    if CONFIG.exists() or CONFIG.is_symlink():
        load_config(CONFIG)
        print("Configuration already exists; secrets and data were not changed.")
        return 0

    email = os.environ.get("AI_OBSERVATORY_ADMIN_EMAIL", "admin@ai-observatory.local")
    password = os.environ.get("AI_OBSERVATORY_ADMIN_PASSWORD")
    if not password:
        if not sys.stdin.isatty():
            raise ValueError("Run make setup in a terminal to enter an admin password.")
        print(f"Create the local administrator: {email}")
        print("Use at least 16 letters, digits, underscores or hyphens.")
        password = getpass.getpass("Administrator password: ")
        if password != getpass.getpass("Confirm password: "):
            raise ValueError("Passwords do not match; no configuration was created.")
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+", email):
        raise ValueError("Invalid administrator email.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,}", password):
        raise ValueError(
            "Password must have at least 16 letters, digits, underscores or hyphens."
        )

    values = {
        "COMPOSE_PROJECT_NAME": f"aiobs-{secrets.token_hex(6)}",
        "WEB_PORT": "3000",
        "MEDIA_PORT": "9090",
        "NEXTAUTH_URL": "http://127.0.0.1:3000",
        "MEDIA_URL": "http://127.0.0.1:9090",
        "ADMIN_EMAIL": email,
        "ADMIN_PASSWORD": password,
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "CLICKHOUSE_PASSWORD": secrets.token_hex(24),
        "REDIS_AUTH": secrets.token_hex(24),
        "MINIO_ROOT_PASSWORD": secrets.token_hex(24),
        "NEXTAUTH_SECRET": secrets.token_hex(32),
        "SALT": secrets.token_hex(32),
        "ENCRYPTION_KEY": secrets.token_hex(32),
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(CONFIG, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.writelines(f"{key}={value}\n" for key, value in values.items())
    print(
        "Created private .env (mode 600). Keep it safe: it contains the instance's secrets."
    )
    print(f"Administrator: {email}. Run make up to start the server.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(initialize())
    except (OSError, ValueError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
