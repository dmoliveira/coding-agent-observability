"""Offline snapshots of all Langfuse volumes, restored only into a new instance."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import secrets
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import (
    COMPOSE,
    CONFIG,
    ROOT,
    compose_environment,
    load_config,
    maintenance_fds,
    maintenance_lock,
)

VOLUMES = (
    "postgres_data",
    "clickhouse_data",
    "clickhouse_logs",
    "redis_data",
    "minio_data",
)
VERSION = "4.82.0"
FORMAT = 2
HELPER = "docker.io/library/alpine@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1"


def docker(*args: str, **kwargs: object) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], check=True, pass_fds=maintenance_fds(), **kwargs
    )


def architecture() -> str:
    result = docker(
        "info", "--format", "{{.Architecture}}", capture_output=True, text=True
    )
    return result.stdout.strip()


def image_versions(config_path: Path) -> list[str]:
    config = load_config(config_path)
    result = docker(
        "compose",
        "--env-file",
        str(config_path),
        "--file",
        str(COMPOSE),
        "--project-name",
        config["COMPOSE_PROJECT_NAME"],
        "config",
        "--images",
        env=compose_environment(config),
        capture_output=True,
        text=True,
    )
    images = sorted(set(result.stdout.splitlines()))
    if not images:
        raise ValueError("Could not determine the stack's image versions.")
    return images


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def volume_name(project: str, volume: str) -> str:
    return f"{project}_{volume}"


def require_stopped_volumes(project: str) -> None:
    marker = ROOT / ".ai-observatory.starting"
    if marker.exists() or marker.is_symlink():
        raise ValueError(
            "A previous startup did not complete; run make down before backup."
        )
    for volume in VOLUMES:
        name = volume_name(project, volume)
        docker("volume", "inspect", name, stdout=subprocess.DEVNULL)
        running = docker(
            "ps",
            "--all",
            "--filter",
            f"volume={name}",
            "--format",
            "{{.ID}}",
            capture_output=True,
            text=True,
        )
        if running.stdout.strip():
            raise ValueError(
                "Volumes are in use or attached to containers. Stop producers, then run make down before backup."
            )


def backup() -> None:
    config = load_config()
    require_stopped_volumes(config["COMPOSE_PROJECT_NAME"])
    destination = ROOT / "backups"
    if destination.is_symlink():
        raise ValueError("Backup directory must not be a symlink.")
    destination.mkdir(mode=0o700, exist_ok=True)
    if destination.stat().st_mode & 0o077:
        raise ValueError("Backup directory must be private (mode 700).")
    output = (
        destination
        / f"observatory-{datetime.now(timezone.utc):%Y%m%d-%H%M%SZ}-{secrets.token_hex(4)}.tar.gz"
    )
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=destination) as temporary:
        tmp = Path(temporary)
        config_copy = tmp / "config.env"
        config_copy.write_bytes(CONFIG.read_bytes())
        config_copy.chmod(0o600)
        files = [config_copy]
        for volume in VOLUMES:
            archive = tmp / f"{volume}.tar"
            fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                docker(
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--mount",
                    f"type=volume,src={volume_name(config['COMPOSE_PROJECT_NAME'], volume)},dst=/data,readonly",
                    HELPER,
                    "tar",
                    "--numeric-owner",
                    "-C",
                    "/data",
                    "-cf",
                    "-",
                    ".",
                    stdout=stream,
                )
            safe_nested_tar(archive)
            files.append(archive)
        manifest = {
            "format": FORMAT,
            "langfuse_version": VERSION,
            "architecture": architecture(),
            "images": image_versions(CONFIG),
            "sha256": {file.name: digest(file) for file in files},
        }
        manifest_path = tmp / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        manifest_path.chmod(0o600)
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with (
                os.fdopen(fd, "wb") as stream,
                tarfile.open(fileobj=stream, mode="w:gz") as tar,
            ):
                for file in [manifest_path, *files]:
                    tar.add(file, arcname=file.name, recursive=False)
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    print(f"Backup created: {output}")
    print(
        "WARNING: archive contains all data and credentials; encrypt it before storing or sharing."
    )


def safe_nested_tar(path: Path) -> None:
    names: set[str] = set()
    links: dict[str, tuple[str, bool]] = {}
    regular_files: set[str] = set()
    with tarfile.open(path, mode="r:") as tar:
        for member in tar:
            name = posixpath.normpath(member.name)
            if (
                member.name.startswith("/")
                or ".." in member.name.split("/")
                or name in names
            ):
                raise ValueError("Backup contains an unsafe volume path.")
            names.add(name)
            if member.isfile():
                regular_files.add(name)
            elif member.issym() or member.islnk():
                if member.linkname.startswith("/"):
                    raise ValueError("Backup contains an unsafe volume link.")
                links[name] = (member.linkname, member.issym())
            elif not member.isdir():
                raise ValueError("Backup contains an unsupported volume entry.")

    # ClickHouse uses relative symlinks into store/. Never extract entries
    # through a symlink or permit a link target to traverse another link.
    for name in names:
        parts = name.split("/")
        if any("/".join(parts[:i]) in links for i in range(1, len(parts))):
            raise ValueError("Backup contains an entry beneath a volume link.")
    for name, (target, symbolic) in links.items():
        parts = posixpath.dirname(name).split("/") if symbolic else []
        parts = [part for part in parts if part not in ("", ".")]
        for part in target.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    raise ValueError("Backup contains an unsafe volume link.")
                parts.pop()
            else:
                parts.append(part)
                if "/".join(parts) in links:
                    raise ValueError("Backup contains a chained volume link.")
        if not symbolic and "/".join(parts) not in regular_files:
            raise ValueError("Backup contains an invalid hard link.")


def unpack_archive(source: Path, directory: Path) -> dict[str, object]:
    expected = {"manifest.json", "config.env", *(f"{volume}.tar" for volume in VOLUMES)}
    with tarfile.open(source, mode="r:gz") as tar:
        members = tar.getmembers()
        if {member.name for member in members} != expected or len(members) != len(
            expected
        ):
            raise ValueError("Backup has missing, duplicate, or unexpected files.")
        if any(not member.isfile() for member in members):
            raise ValueError("Backup contains an unsafe archive entry.")
        for member in members:
            input_file = tar.extractfile(member)
            if input_file is None:
                raise ValueError("Backup contains an unreadable file.")
            target = directory / member.name
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with input_file, os.fdopen(fd, "wb") as output:
                while block := input_file.read(1024 * 1024):
                    output.write(block)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    expected_hashes = expected - {"manifest.json"}
    hashes = manifest.get("sha256")
    if not isinstance(hashes, dict) or set(hashes) != expected_hashes:
        raise ValueError("Invalid backup checksums.")
    if any(digest(directory / name) != hashes[name] for name in expected_hashes):
        raise ValueError("Backup checksum mismatch.")
    if manifest.get("format") != FORMAT:
        raise ValueError(
            "Backup format is unsupported; use its original repository version."
        )
    if manifest.get("langfuse_version") != VERSION:
        raise ValueError("Backup Langfuse version does not match this installation.")
    if manifest.get("architecture") != architecture():
        raise ValueError("Backup architecture does not match this Docker host.")
    if manifest.get("images") != image_versions(directory / "config.env"):
        raise ValueError("Backup service image versions do not match this checkout.")
    for volume in VOLUMES:
        safe_nested_tar(directory / f"{volume}.tar")
    load_config(directory / "config.env")
    return manifest


def publish_config(config: dict[str, str]) -> None:
    """Publish a complete private configuration without replacing an existing file."""
    fd, name = tempfile.mkstemp(prefix=".env.restore-", dir=CONFIG.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.writelines(f"{key}={value}\n" for key, value in config.items())
            stream.flush()
            os.fsync(stream.fileno())
        # Source and destination are in the same directory; link is exclusive.
        os.link(name, CONFIG, follow_symlinks=False)
    finally:
        Path(name).unlink(missing_ok=True)


def restore(source: Path) -> None:
    if CONFIG.exists() or CONFIG.is_symlink():
        raise ValueError(
            "Restore requires a fresh installation with no .env; existing data is untouched."
        )
    if not source.is_file():
        raise ValueError("Backup archive not found.")
    with tempfile.TemporaryDirectory(prefix="ai-observatory-restore-") as temporary:
        tmp = Path(temporary)
        unpack_archive(source, tmp)
        config = load_config(tmp / "config.env")
        project = f"aiobs-{secrets.token_hex(6)}"
        token = secrets.token_hex(16)
        created: list[str] = []
        try:
            for volume in VOLUMES:
                name = volume_name(project, volume)
                docker(
                    "volume",
                    "create",
                    "--label",
                    f"com.docker.compose.project={project}",
                    "--label",
                    f"com.docker.compose.volume={volume}",
                    "--label",
                    f"ai-observatory.restore={token}",
                    name,
                    stdout=subprocess.DEVNULL,
                )
                label = docker(
                    "volume",
                    "inspect",
                    "--format",
                    '{{index .Labels "ai-observatory.restore"}}',
                    name,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                if label != token:
                    raise ValueError(
                        f"Volume name collision: {name}; refusing to overwrite it."
                    )
                created.append(name)
                with (tmp / f"{volume}.tar").open("rb") as stream:
                    docker(
                        "run",
                        "--rm",
                        "--interactive",
                        "--network",
                        "none",
                        "--mount",
                        f"type=volume,src={name},dst=/data",
                        HELPER,
                        "tar",
                        "--numeric-owner",
                        "-C",
                        "/data",
                        "-xf",
                        "-",
                        stdin=stream,
                    )
            config["COMPOSE_PROJECT_NAME"] = project
            # Restores never re-enable remote access from the source instance.
            config["NEXTAUTH_URL"] = f"http://127.0.0.1:{config['WEB_PORT']}"
            config["MEDIA_URL"] = f"http://127.0.0.1:{config['MEDIA_PORT']}"
            publish_config(config)
        except BaseException:
            if created:
                print(
                    "Restore did not complete; new volumes were left untouched:",
                    file=sys.stderr,
                )
                for name in created:
                    print(f"  {name}", file=sys.stderr)
            raise
    print(
        "Restored into a new, separate installation. Run make up after checking ports."
    )
    print("The original instance and backup have not been changed.")


def main(action: str) -> None:
    with maintenance_lock():
        if action == "backup":
            backup()
        elif action == "restore":
            source = os.environ.get("BACKUP")
            if not source:
                raise ValueError("Specify BACKUP=/path/to/archive.")
            restore(Path(source).expanduser())
        else:
            raise ValueError("Expected backup or restore.")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Expected backup or restore.")
        main(sys.argv[1])
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        tarfile.TarError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"Backup command failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
