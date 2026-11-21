"""Provision separately owned native dashboards without evaluation rules."""

from __future__ import annotations

import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import common

from integrations import agent_dashboard_definition as spec
from integrations import span_dashboard as managed

STATE_NAME = ".ai-observatory.agent-dashboard-state.json"
GRID_COLUMNS = 12
TIME_SERIES_CHARTS = {
    "LINE_TIME_SERIES",
    "AREA_TIME_SERIES",
    "BAR_TIME_SERIES",
}


def dashboard_placements(widgets: dict[str, dict]) -> list[dict[str, str | int]]:
    """Pack compact count cards and paired charts into a 12-column grid."""
    x = y = row_height = 0
    placements: list[dict[str, str | int]] = []
    for key, widget in widgets.items():
        chart = widget["chartType"]
        if chart in TIME_SERIES_CHARTS:
            width, height = 6, 5
        elif chart == "NUMBER":
            width, height = 3, 3
        elif chart in {"HORIZONTAL_BAR", "VERTICAL_BAR", "PIE", "HISTOGRAM"}:
            width, height = 6, 5
        elif chart == "PIVOT_TABLE":
            width, height = GRID_COLUMNS, 8
        else:
            raise ValueError(f"Unsupported dashboard chart type: {chart}")

        if x + width > GRID_COLUMNS:
            y += row_height
            x = row_height = 0
        placements.append(
            {
                "type": "widget",
                "id": key,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
            }
        )
        x += width
        row_height = max(row_height, height)
        if x == GRID_COLUMNS:
            y += row_height
            x = row_height = 0
    return placements


def apply() -> None:
    with common.maintenance_lock():
        client, _, project = managed.context()
        state, raw = managed.read_state(STATE_NAME)
        if state is not None and state["projectId"] != project:
            raise ValueError(
                "Agent dashboard state belongs to a different project; nothing was changed."
            )
        if state is None:
            state = {
                "version": 1,
                "projectId": project,
                "nonce": secrets.token_hex(4),
                "resources": {},
                "pending": None,
            }
            raw = managed.save_state(state, None, STATE_NAME)
        assert raw is not None
        definitions = spec.definitions()
        allowed = {
            *(f"dashboard:{panel}" for panel in definitions),
            *(
                f"widget:{panel}.{key}"
                for panel, value in definitions.items()
                for key in value["widgets"]
            ),
        }
        if set(state["resources"]) - allowed:
            raise ValueError(
                "Definitions removed a managed resource; it was not deleted or detached."
            )
        existing = managed.existing_resources(client, state)
        urls = []
        for panel_key, panel in definitions.items():
            placements = []
            for widget_key, widget in panel["widgets"].items():
                resource, raw = managed.ensure_resource(
                    client,
                    state,
                    raw,
                    existing,
                    f"widget:{panel_key}.{widget_key}",
                    widget,
                    STATE_NAME,
                )
                placements.append((widget_key, resource["id"]))
            layout = {
                placement["id"]: placement
                for placement in dashboard_placements(panel["widgets"])
            }
            placements = [
                {**layout[key], "widgetId": widget_id} for key, widget_id in placements
            ]
            desired = {
                "name": panel["name"],
                "description": panel["description"],
                "definition": {"widgets": placements},
                "filters": [],
            }
            dashboard, raw = managed.ensure_resource(
                client,
                state,
                raw,
                existing,
                f"dashboard:{panel_key}",
                desired,
                STATE_NAME,
            )
            urls.append(
                (
                    panel["name"],
                    f"{client.base}/project/{project}/dashboards/{dashboard['id']}",
                )
            )
        for name, url in urls:
            print(f"{name}: {url}")
        print("No evaluation rules or historical processing were created.")


if __name__ == "__main__":
    try:
        apply()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"Agent dashboards failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
