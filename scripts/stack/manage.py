"""Operate this installation without touching other Compose projects."""

from __future__ import annotations

import subprocess
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import docker_engine_is_recent, load_config, maintenance_lock, run_compose


def main(action: str) -> int:
    if action in ("up", "down"):
        with maintenance_lock():
            return operate(action)
    return operate(action)


def operate(action: str) -> int:
    config = load_config()
    if action == "up":
        if not docker_engine_is_recent():
            raise ValueError(
                "Docker Engine 28 or newer is required for safe localhost binding."
            )
        run_compose(config, "up", "--detach", "--wait", "--wait-timeout", "600")
    elif action == "down":
        if config["NEXTAUTH_URL"].startswith("https://"):
            from integrations.tailscale import disable

            try:
                disable(restart=False)
            except (OSError, ValueError, subprocess.CalledProcessError):
                # Even if a shared Serve route cannot safely be changed, stop
                # the local backends so the service is no longer reachable.
                run_compose(config, "down")
                print(
                    "The stack stopped, but Tailscale route cleanup failed.",
                    file=sys.stderr,
                )
                raise
            config = load_config()
        # No --volumes: data must survive stop/start.
        run_compose(config, "down")
    elif action == "status":
        run_compose(config, "ps", "--all")
    elif action == "logs":
        run_compose(config, "logs", "--follow", "web", "worker")
    elif action == "open":
        webbrowser.open(config["NEXTAUTH_URL"])
    else:
        raise ValueError("Unknown stack action.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1]))
    except (
        IndexError,
        KeyError,
        OSError,
        ValueError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"Stack command failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
