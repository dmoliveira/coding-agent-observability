"""Check effective local configuration without printing credentials."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import load_config, run_compose
from integrations.opencode import NoRedirect


def main() -> None:
    config = load_config()
    rendered = run_compose(
        config, "config", "--format", "json", capture_output=True, text=True
    )
    services = json.loads(rendered.stdout)["services"]
    published = {
        name: service["ports"]
        for name, service in services.items()
        if service.get("ports")
    }
    if set(published) != {"web", "minio"} or any(
        port.get("host_ip") != "127.0.0.1"
        for ports in published.values()
        for port in ports
    ):
        raise ValueError("Rendered Compose configuration is not loopback-only.")
    print("OK  Rendered Compose ports are loopback-only")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    base_url = f"http://127.0.0.1:{config['WEB_PORT']}"
    with opener.open(
        f"{base_url}/api/public/health?failIfDatabaseUnavailable=true", timeout=10
    ) as response:
        if response.status != 200:
            raise ValueError("Langfuse health check failed.")
    print("OK  Langfuse web and database health")
    request = urllib.request.Request(
        f"{base_url}/api/public/otel/v1/traces",
        data=b'{"resourceSpans":[]}',
        headers={
            "Content-Type": "application/json",
            "x-langfuse-ingestion-version": "4",
        },
        method="POST",
    )
    try:
        opener.open(request, timeout=10).close()
    except urllib.error.HTTPError as exc:
        if exc.code not in (401, 403):
            raise ValueError(
                f"Unexpected unauthenticated response: HTTP {exc.code}."
            ) from None
    else:
        raise ValueError("Unauthenticated ingestion unexpectedly succeeded.")
    print("OK  Ingestion rejects missing credentials")


if __name__ == "__main__":
    try:
        main()
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
