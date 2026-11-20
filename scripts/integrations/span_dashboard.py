"""Provision a small, owned Langfuse dashboard and send synthetic demo spans."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common

from integrations import lmstudio
from integrations import span_dashboard_definition as spec
from integrations.opencode import NoRedirect, write_new

STATE_NAME = ".ai-observatory.span-dashboard-state.json"
MAX_RESPONSE = 4 * 1024 * 1024
RESOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
FIELDS = {
    "evaluator": (
        "name",
        "description",
        "type",
        "prompt",
        "modelConfig",
        "variableMapping",
        "outputDefinition",
    ),
    "rule": ("name", "enabled", "sampling", "filter", "evaluatorAssignments"),
    "widget": (
        "name",
        "description",
        "view",
        "dimensions",
        "metrics",
        "filters",
        "chartType",
        "chartConfig",
    ),
    "dashboard": ("name", "description", "definition", "filters"),
}
PATHS = {
    "evaluator": "/api/public/v2/evaluators",
    "rule": "/api/public/v2/evaluation-rules",
    "widget": "/api/public/unstable/dashboard-widgets",
    "dashboard": "/api/public/unstable/dashboards",
}


class ApiError(ValueError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Langfuse returned HTTP {status}.")


class Client:
    def __init__(self, base: str, auth: str) -> None:
        self.base, self.auth = base, auth
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        headers = {"Authorization": self.auth, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path,
            method=method,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("Langfuse response was too large.")
            result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code) from None
        except (
            OSError,
            urllib.error.URLError,
            http.client.HTTPException,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            raise ValueError("Could not safely read the Langfuse response.") from None
        if not isinstance(result, dict):
            raise ValueError("Langfuse returned an unexpected response.")  # noqa: TRY004
        return result


def kind_for(key: str) -> str:
    if key.startswith("widget:"):
        return "widget"
    if key.startswith("dashboard:"):
        return "dashboard"
    return key


def projection(kind: str, value: dict[str, Any]) -> dict[str, Any]:
    fields = FIELDS[kind]
    if any(field not in value for field in fields):
        raise ValueError(f"Langfuse returned an incomplete {kind}.")
    return {field: value[field] for field in fields}


def fingerprint(kind: str, value: dict[str, Any]) -> str:
    if not isinstance(value.get("id"), str) or not RESOURCE_ID.fullmatch(value["id"]):
        raise ValueError(f"Langfuse returned an invalid {kind} ID.")
    data = {"id": value["id"], **projection(kind, value)}
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def read_state(
    state_name: str = STATE_NAME,
) -> tuple[dict[str, Any] | None, bytes | None]:
    try:
        fd = os.open(
            common.ROOT / state_name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
    except FileNotFoundError:
        return None, None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 65536
        ):
            raise ValueError(
                "Dashboard state must be a private file owned by the current user."
            )
        raw = stream.read()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("Dashboard state is invalid; it was not changed.") from None
    if (
        not isinstance(data, dict)
        or set(data) != {"version", "projectId", "nonce", "resources", "pending"}
        or data["version"] != 1
        or not isinstance(data["projectId"], str)
        or not re.fullmatch(r"[0-9a-f]{8}", str(data["nonce"]))
        or not isinstance(data["resources"], dict)
        or (data["pending"] is not None and not isinstance(data["pending"], str))
    ):
        raise ValueError("Dashboard state is invalid; it was not changed.")
    for key, item in data["resources"].items():
        if (
            not isinstance(key, str)
            or kind_for(key) not in FIELDS
            or not isinstance(item, dict)
            or set(item) != {"id", "fingerprint"}
            or not isinstance(item["id"], str)
            or not RESOURCE_ID.fullmatch(item["id"])
            or not re.fullmatch(r"[0-9a-f]{64}", str(item["fingerprint"]))
        ):
            raise ValueError(
                "Dashboard ownership state is invalid; it was not changed."
            )
    return data, raw


def save_state(
    state: dict[str, Any], previous: bytes | None, state_name: str = STATE_NAME
) -> bytes:
    path = common.ROOT / state_name
    content = (json.dumps(state, sort_keys=True) + "\n").encode()
    if previous is None:
        write_new(path, content.decode())
        return content
    _, actual = read_state(state_name)
    if actual != previous:
        raise ValueError(
            "Dashboard state changed concurrently; it was not overwritten."
        )
    fd, name = tempfile.mkstemp(prefix=".ai-observatory-dashboard-", dir=common.ROOT)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        _, actual = read_state(state_name)
        if actual != previous:
            raise ValueError(
                "Dashboard state changed concurrently; it was not overwritten."
            )
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
    return content


def existing_resources(
    client: Client, state: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    if state["pending"] is not None:
        raise ValueError(
            "An earlier dashboard operation may have completed without recording its result; see dashboard recovery instructions."
        )
    result = {}
    for key, record in state["resources"].items():
        kind = kind_for(key)
        value = client.request("GET", f"{PATHS[kind]}/{record['id']}")
        if fingerprint(kind, value) != record["fingerprint"]:
            raise ValueError(
                f"The managed {kind} was changed outside this script; it was not overwritten."
            )
        result[key] = value
    return result


def ensure_resource(
    client: Client,
    state: dict[str, Any],
    raw: bytes,
    existing: dict[str, dict[str, Any]],
    key: str,
    desired: dict[str, Any],
    state_name: str = STATE_NAME,
) -> tuple[dict[str, Any], bytes]:
    kind = kind_for(key)
    current = existing.get(key)
    if current is not None and projection(kind, current) == desired:
        return current, raw
    state["pending"] = key
    raw = save_state(state, raw, state_name)
    path = PATHS[kind] + (f"/{current['id']}" if current is not None else "")
    result = client.request("PATCH" if current is not None else "POST", path, desired)
    if current is not None and result.get("id") != current["id"]:
        raise ValueError(
            f"Langfuse returned an unexpected {kind}; inspect it before retrying."
        )
    state["resources"][key] = {
        "id": result.get("id"),
        "fingerprint": fingerprint(kind, result),
    }
    state["pending"] = None
    raw = save_state(state, raw, state_name)
    if projection(kind, result) != desired:
        raise ValueError(
            f"Langfuse normalized the {kind} unexpectedly; inspect it before retrying."
        )
    existing[key] = result
    return result, raw


def connection(client: Client, config: dict[str, str], project: str) -> str:
    lmstudio.verify_server()
    lmstudio.verify_docker_engine(config)
    if not common.lmstudio_enabled():
        raise ValueError("Run make connect-lmstudio before provisioning the dashboard.")
    state, _ = lmstudio.read_state()
    if not state or state["projectId"] != project or not state["connectionId"]:
        raise ValueError(
            "The managed LM Studio connection is missing or belongs to another project."
        )
    matches = [
        row
        for row in lmstudio.connections(client.base, client.auth)
        if row.get("id") == state["connectionId"]
    ]
    if (
        len(matches) != 1
        or lmstudio.connection_fingerprint(matches[0]) != state["fingerprint"]
    ):
        raise ValueError(
            "The managed LM Studio connection changed; inspect it before proceeding."
        )
    if spec.MODEL not in lmstudio.validate_connection(matches[0], state["provider"]):
        raise ValueError(
            "The configured demo model is not registered in the LM Studio connection."
        )
    return state["provider"]


def context() -> tuple[Client, dict[str, str], str]:
    config = common.load_config()
    base, auth = lmstudio.project_credentials(config)
    client = Client(base, auth)
    return client, config, lmstudio.project_id(base, auth)


def rule_definition(
    nonce: str, definitions: dict[str, Any], evaluator_id: str
) -> dict[str, Any]:
    return {
        "name": f"AI Observatory demo span rule [{nonce}]",
        "enabled": True,
        "sampling": 1,
        "filter": definitions["rule_filter"],
        "evaluatorAssignments": [
            {"evaluatorId": evaluator_id, "variableMapping": None}
        ],
    }


def apply() -> None:
    with common.maintenance_lock():
        client, config, project = context()
        provider = connection(client, config, project)
        state, raw = read_state()
        if state is not None and state["projectId"] != project:
            raise ValueError(
                "Dashboard state belongs to a different project; nothing was changed."
            )
        if state is None:
            state = {
                "version": 1,
                "projectId": project,
                "nonce": secrets.token_hex(4),
                "resources": {},
                "pending": None,
            }
            raw = save_state(state, None)
        assert raw is not None
        definitions = spec.definitions(provider, state["nonce"])
        allowed = {
            "evaluator",
            "rule",
            "dashboard",
            *(f"widget:{key}" for key in definitions["widgets"]),
        }
        if set(state["resources"]) - allowed:
            raise ValueError(
                "Definitions removed a managed resource; it was not deleted or detached."
            )
        existing = existing_resources(client, state)
        evaluator, raw = ensure_resource(
            client, state, raw, existing, "evaluator", definitions["evaluator"]
        )
        if evaluator.get("status") != "active":
            raise ValueError(
                "The demo evaluator is paused; check its model connection in Langfuse."
            )
        placements = []
        for index, (key, widget) in enumerate(definitions["widgets"].items()):
            value, raw = ensure_resource(
                client, state, raw, existing, f"widget:{key}", widget
            )
            placements.append(
                {
                    "type": "widget",
                    "id": key,
                    "widgetId": value["id"],
                    "x": (index % 2) * 6,
                    "y": (index // 2) * 5,
                    "width": 6,
                    "height": 5,
                }
            )
        dashboard = {
            "name": "AI Observatory — Span activity",
            "description": "Local LM Studio classifications for explicitly marked demo spans.",
            "definition": {"widgets": placements},
            "filters": [],
        }
        panel, raw = ensure_resource(
            client, state, raw, existing, "dashboard", dashboard
        )
        rule = rule_definition(state["nonce"], definitions, evaluator["id"])
        _, raw = ensure_resource(client, state, raw, existing, "rule", rule)
        print(
            "Dashboard ready:",
            f"{client.base}/project/{project}/dashboards/{panel['id']}",
        )
        print(
            "Only specifically named demo spans in the ai-observatory-demo environment are classified; no historical spans were scheduled."
        )
        print("Run make span-dashboard-demo to add three synthetic examples.")


def no_other_enabled_rules(client: Client, own_id: str) -> None:
    cursor = None
    for _ in range(100):
        query = "?limit=100" + (
            "&cursor=" + urllib.parse.quote(cursor, safe="") if cursor else ""
        )
        page = client.request("GET", PATHS["rule"] + query)
        rows, meta = page.get("data"), page.get("meta")
        if not isinstance(rows, list) or not isinstance(meta, dict):
            raise ValueError("Could not verify existing evaluation rules.")  # noqa: TRY004
        if any(
            not isinstance(row, dict)
            or not isinstance(row.get("enabled"), bool)
            or not isinstance(row.get("id"), str)
            or (row["enabled"] and row["id"] != own_id)
            for row in rows
        ):
            raise ValueError(
                "Another enabled evaluation rule could process the synthetic examples; no spans were sent."
            )
        cursor = meta.get("cursor")
        if cursor is None:
            return
        if not isinstance(cursor, str):
            raise ValueError("Langfuse returned invalid rule pagination.")  # noqa: TRY004
    raise ValueError("Too many evaluation-rule pages to verify safely.")


def demo() -> None:
    with common.maintenance_lock():
        client, config, project = context()
        provider = connection(client, config, project)
        state, _ = read_state()
        if not state or state["projectId"] != project:
            raise ValueError("Run make span-dashboard before sending demo spans.")
        existing = existing_resources(client, state)
        if (
            "rule" not in existing
            or "evaluator" not in existing
            or not existing["rule"].get("enabled")
        ):
            raise ValueError("The demo classification rule is not active.")
        definitions = spec.definitions(provider, state["nonce"])
        if projection("evaluator", existing["evaluator"]) != definitions[
            "evaluator"
        ] or projection("rule", existing["rule"]) != rule_definition(
            state["nonce"], definitions, existing["evaluator"]["id"]
        ):
            raise ValueError(
                "Dashboard definitions changed; run make span-dashboard before sending demo spans."
            )
        no_other_enabled_rules(client, existing["rule"]["id"])
        trace = secrets.token_hex(16)
        now = int(time.time() * 1000)

        def attribute(key: str, value: str) -> dict[str, Any]:
            return {"key": key, "value": {"stringValue": value}}

        spans = []
        for index, example in enumerate(spec.EXAMPLES):
            spans.append(
                {
                    "traceId": trace,
                    "spanId": secrets.token_hex(8),
                    "name": definitions["span_name"],
                    "kind": 1,
                    "startTimeUnixNano": str((now + index * 2) * 1_000_000),
                    "endTimeUnixNano": str((now + index * 2 + 1) * 1_000_000),
                    "attributes": [
                        attribute("langfuse.observation.input", example),
                        attribute(
                            "langfuse.trace.name", "AI Observatory synthetic span demo"
                        ),
                    ],
                    "status": {},
                }
            )
        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            attribute("service.name", "ai-observatory-demo"),
                            attribute("langfuse.environment", spec.ENVIRONMENT),
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "ai-observatory-demo", "version": "1"},
                            "spans": spans,
                        }
                    ],
                }
            ]
        }
        result = client.request("POST", "/api/public/otel/v1/traces", payload)
        if result.get("partialSuccess", {}).get("rejectedSpans", 0) not in (0, "0"):
            raise ValueError("Langfuse rejected one or more synthetic spans.")
        print(
            f"Sent {len(spans)} synthetic spans. Classifications are processed asynchronously."
        )
        print("Synthetic trace:", f"{client.base}/project/{project}/traces/{trace}")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ("apply", "demo"):
            raise ValueError("Expected apply or demo.")
        apply() if sys.argv[1] == "apply" else demo()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"Span dashboard failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
