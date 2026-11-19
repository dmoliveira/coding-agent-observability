"""Opt-in OpenCode V2 integration using the official Langfuse plugin."""

from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import re
import shlex
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import CONFIG, load_config

PLUGIN = "@langfuse/opencode-observability-plugin@0.7.0"
VARIABLE = "AI_OBSERVATORY_PLUGIN"
SHELL_KEYS = (
    VARIABLE,
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
    "LANGFUSE_BASEURL",
)


def project_path() -> Path:
    value = os.environ.get("PROJECT")
    if not value:
        raise ValueError("Specify PROJECT=/path/to/project.")
    project = Path(value).expanduser().resolve(strict=True)
    if not project.is_dir():
        raise ValueError("PROJECT must be an existing directory.")
    return project


def credentials_path(project: Path) -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    name = hashlib.sha256(os.fsencode(str(project))).hexdigest()[:24]
    return root / "ai-observatory" / "projects" / f"{name}.env"


def validate_url(value: str) -> str:
    url = urlsplit(value)
    if url.username or url.password or url.path or url.query or url.fragment:
        raise ValueError(
            "Langfuse URL must be a host and port, without a path or credentials."
        )
    if url.scheme == "http" and url.hostname == "127.0.0.1" and url.port:
        return value
    if (
        url.scheme == "https"
        and url.hostname
        and url.hostname.endswith(".ts.net")
        and url.port
    ):
        return value
    raise ValueError(
        "Langfuse URL must use local 127.0.0.1 or an HTTPS Tailscale host."
    )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: object, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


def verify_key(base_url: str, public: str, secret: str) -> None:
    credentials = base64.b64encode(f"{public}:{secret}".encode()).decode()
    request = urllib.request.Request(
        f"{base_url}/api/public/otel/v1/traces",
        data=b'{"resourceSpans":[]}',
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/json",
            "x-langfuse-ingestion-version": "4",
        },
        method="POST",
    )
    # Proxies and redirects could send the API key somewhere unintended.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise ValueError("Langfuse rejected the API key.")
    except urllib.error.HTTPError as exc:
        raise ValueError(
            f"Langfuse rejected the key or request (HTTP {exc.code})."
        ) from None
    except urllib.error.URLError:
        raise ValueError("Could not reach the specified Langfuse endpoint.") from None


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Credentials directory must not be a symlink: {path}")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError(f"Credentials directory must be private (mode 700): {path}")


def write_new(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)


def project_configs(directory: Path) -> list[Path]:
    configs = [directory / "opencode.json", directory / "opencode.jsonc"]
    if any(path.is_symlink() for path in configs):
        raise ValueError("Project OpenCode configuration must not be a symlink.")
    return [path for path in configs if path.exists()]


def has_plugin_gate(paths: list[Path]) -> bool:
    return any(
        path.is_file() and f"{{env:{VARIABLE}}}" in path.read_text(encoding="utf-8")
        for path in paths
    )


def connect(project: Path) -> None:
    directory = project / ".opencode"
    if directory.is_symlink():
        raise ValueError(
            "The project's .opencode directory is a symlink; configure the plugin manually."
        )
    destination = directory / "opencode.json"
    existing = project_configs(directory)
    if existing and not has_plugin_gate(existing):
        raise ValueError(
            "Project configuration already exists; add the plugin gate without overwriting it. See docs/opencode.md."
        )
    credentials = credentials_path(project)
    if credentials.exists() or credentials.is_symlink():
        raise ValueError(
            "Credentials for this project already exist; they were not overwritten."
        )
    default_url = (
        load_config()["NEXTAUTH_URL"] if CONFIG.exists() else "http://127.0.0.1:3000"
    )
    base_url = validate_url(os.environ.get("AI_OBSERVATORY_URL", default_url))
    public = os.environ.get("LANGFUSE_PUBLIC_KEY") or getpass.getpass(
        "Langfuse public key: "
    )
    secret = os.environ.get("LANGFUSE_SECRET_KEY") or getpass.getpass(
        "Langfuse secret key: "
    )
    if not re.fullmatch(r"pk-lf-[A-Za-z0-9_-]+", public):
        raise ValueError("Invalid Langfuse public key format.")
    if not re.fullmatch(r"sk-lf-[A-Za-z0-9_-]+", secret):
        raise ValueError("Invalid Langfuse secret key format.")
    verify_key(base_url, public, secret)
    private_directory(credentials.parent.parent)
    private_directory(credentials.parent)
    values = {
        VARIABLE: PLUGIN,
        "LANGFUSE_PUBLIC_KEY": public,
        "LANGFUSE_SECRET_KEY": secret,
        "LANGFUSE_BASE_URL": base_url,
        "LANGFUSE_BASEURL": base_url,
    }
    write_new(
        credentials,
        "".join(
            f"export {key}={shlex.quote(value)}\n" for key, value in values.items()
        ),
    )
    if not existing:
        directory.mkdir(mode=0o700, exist_ok=True)
        try:
            write_new(
                destination,
                json.dumps(
                    {
                        "$schema": "https://opencode.ai/config.json",
                        "plugins": [f"{{env:{VARIABLE}}}"],
                    },
                    indent=2,
                )
                + "\n",
            )
        except OSError:
            print(
                f"Credentials were saved at {credentials}; project configuration was not changed.",
                file=sys.stderr,
            )
            raise
    print(
        "OpenCode tracing configured for this project. The plugin is inactive until the environment is loaded."
    )
    print("Run: make run-opencode PROJECT=/path/to/project")
    print("The plugin can capture prompts, reasoning, source content and tool outputs.")


def read_credentials(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(
            "No safe credentials file for this project; run make connect-opencode."
        )
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError(
            "Credentials file must be owned by you and private (mode 600)."
        )
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        words = shlex.split(line)
        if len(words) != 2 or words[0] != "export" or "=" not in words[1]:
            raise ValueError("Invalid credentials file.")
        key, value = words[1].split("=", 1)
        if key not in SHELL_KEYS or key in result:
            raise ValueError("Invalid credentials file.")
        result[key] = value
    if set(result) != set(SHELL_KEYS) or result[VARIABLE] != PLUGIN:
        raise ValueError("Incomplete credentials file.")
    validate_url(result["LANGFUSE_BASE_URL"])
    if result["LANGFUSE_BASEURL"] != result["LANGFUSE_BASE_URL"]:
        raise ValueError("Conflicting Langfuse URLs.")
    return result


def main(action: str) -> None:
    project = project_path()
    if action == "connect":
        connect(project)
    elif action == "env":
        path = credentials_path(project)
        read_credentials(path)
        print(path)
    elif action == "run":
        values = read_credentials(credentials_path(project))
        directory = project / ".opencode"
        if directory.is_symlink() or not has_plugin_gate(project_configs(directory)):
            raise ValueError(
                "Project plugin configuration is missing; see the OpenCode guide."
            )
        verify_key(
            values["LANGFUSE_BASE_URL"],
            values["LANGFUSE_PUBLIC_KEY"],
            values["LANGFUSE_SECRET_KEY"],
        )
        env = os.environ.copy()
        env.update(values)
        # This legacy launcher runs the project-specific official plugin; avoid
        # also loading the default-on global adapter in the same process.
        env["AI_OBSERVATORY_DISABLED"] = "1"
        os.chdir(project)
        os.execvpe("opencode", ["opencode", "--standalone"], env)
    else:
        raise ValueError("Expected connect, run, or env.")


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except (IndexError, OSError, ValueError) as exc:
        print(f"OpenCode integration failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
