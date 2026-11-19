"""Check prerequisites without printing credentials or modifying state."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import docker_engine_is_recent


def check(command: list[str]) -> bool:
    try:
        subprocess.run(
            command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def main() -> int:
    items = {
        "Python 3.10+": sys.version_info >= (3, 10),
        "Docker CLI": shutil.which("docker") is not None,
        "Docker daemon": check(["docker", "info"]),
        "Docker Compose": check(["docker", "compose", "version"]),
        "Docker Engine 28+": docker_engine_is_recent(),
    }
    for name, ok in items.items():
        print(f"{'OK' if ok else 'MISSING'}  {name}")
    print("OpenCode and Node.js 22+ are required only to use the OpenCode plugin.")
    print("Use Docker Engine 28 or newer to avoid older localhost publishing issues.")
    return 0 if all(items.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
