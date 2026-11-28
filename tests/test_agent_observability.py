"""Synthetic tests for measured dashboards and structural OpenCode reports."""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import common

from integrations import (
    agent_dashboard,
    agent_dashboard_definition,
    agent_query,
    span_dashboard,
)
from integrations import agent_report as report


class DashboardClient:
    base = "http://127.0.0.1:3000"

    def __init__(self) -> None:
        self.resources: dict[str, dict] = {}
        self.writes: list[tuple[str, str, dict]] = []
        self.fail = False

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        if method == "GET":
            return copy.deepcopy(self.resources[path])
        self.writes.append((method, path, copy.deepcopy(body)))
        if self.fail:
            raise ValueError("synthetic write failed")
        if method == "POST":
            identifier = f"synthetic-{len(self.writes)}"
            path += "/" + identifier
            self.resources[path] = {"id": identifier, **copy.deepcopy(body)}
        else:
            self.resources[path].update(copy.deepcopy(body))
        return copy.deepcopy(self.resources[path])


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = DashboardClient()
        for patch in (
            mock.patch.object(common, "ROOT", self.root),
            mock.patch.object(
                span_dashboard, "context", return_value=(self.client, {}, "project-1")
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def apply(self) -> str:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            agent_dashboard.apply()
        return output.getvalue()

    def test_dashboards_are_separately_owned_idempotent_and_do_not_enable_evals(
        self,
    ) -> None:
        output = self.apply()
        count = len(self.client.writes)
        self.apply()
        self.assertEqual(count, len(self.client.writes))
        self.assertEqual(
            sum(
                path == span_dashboard.PATHS["dashboard"]
                for _, path, _ in self.client.writes
            ),
            3,
        )
        self.assertTrue(
            all(
                path
                in (span_dashboard.PATHS["widget"], span_dashboard.PATHS["dashboard"])
                for _, path, _ in self.client.writes
            )
        )
        self.assertIn("No evaluation rules", output)
        self.assertFalse((self.root / span_dashboard.STATE_NAME).exists())
        self.assertEqual(
            (self.root / agent_dashboard.STATE_NAME).stat().st_mode & 0o777, 0o600
        )
        state, _ = span_dashboard.read_state(agent_dashboard.STATE_NAME)
        self.assertIn("dashboard:operations", state["resources"])
        self.assertIn("dashboard:performance", state["resources"])
        self.assertIn("dashboard:runtime", state["resources"])
        definitions = agent_dashboard_definition.definitions()
        self.assertIn("project-wide", definitions["performance"]["description"])
        self.assertIn("not proof", definitions["operations"]["description"])
        for panel_name, panel in definitions.items():
            dashboard_id = state["resources"][f"dashboard:{panel_name}"]["id"]
            dashboard = self.client.resources[
                f"{span_dashboard.PATHS['dashboard']}/{dashboard_id}"
            ]
            placed = dashboard["definition"]["widgets"]
            self.assertEqual(len(placed), len(panel["widgets"]))
            self.assertTrue(all(item.get("widgetId") for item in placed))

    def test_visualizations_use_supported_types_and_non_overlapping_layouts(
        self,
    ) -> None:
        supported = {
            # DashboardWidgetChartType from Langfuse v4.82.0:
            # web/src/features/widgets/lib/dashboardWidgetChartTypes.ts
            "LINE_TIME_SERIES",
            "AREA_TIME_SERIES",
            "BAR_TIME_SERIES",
            "HORIZONTAL_BAR",
            "VERTICAL_BAR",
            "PIE",
            "NUMBER",
            "HISTOGRAM",
            "PIVOT_TABLE",
        }
        definitions = agent_dashboard_definition.definitions()
        expected_trends = {
            "operations": {
                "turn_trend": "LINE_TIME_SERIES",
                "turn_latency_trend": "LINE_TIME_SERIES",
                "turn_error_trend": "BAR_TIME_SERIES",
                "generation_trend": "LINE_TIME_SERIES",
                "generation_latency_trend": "LINE_TIME_SERIES",
                "generation_error_trend": "BAR_TIME_SERIES",
            },
            "performance": {
                "tokens_trend": "LINE_TIME_SERIES",
                "input_tokens_trend": "LINE_TIME_SERIES",
                "output_tokens_trend": "LINE_TIME_SERIES",
                "cost_trend": "LINE_TIME_SERIES",
                "tool_trend": "LINE_TIME_SERIES",
                "tool_error_trend": "BAR_TIME_SERIES",
            },
        }

        for panel in definitions.values():
            widgets = panel["widgets"]
            placements = agent_dashboard.dashboard_placements(widgets)
            self.assertEqual(
                {placement["id"] for placement in placements}, set(widgets)
            )
            occupied: set[tuple[int, int]] = set()
            for key, definition in widgets.items():
                chart = definition["chartType"]
                self.assertIn(chart, supported, key)
                self.assertEqual(definition["chartConfig"]["type"], chart)
                if chart == "PIE":
                    self.assertEqual(definition["dimensions"], [{"field": "level"}])
                    self.assertEqual(
                        definition["metrics"], [{"measure": "count", "agg": "count"}]
                    )
                placement = next(item for item in placements if item["id"] == key)
                self.assertGreater(placement["width"], 0)
                self.assertGreater(placement["height"], 0)
                self.assertLessEqual(placement["x"] + placement["width"], 12)
                if chart.endswith("TIME_SERIES"):
                    self.assertEqual(placement["width"], 6)
                for cell in (
                    (column, row)
                    for column in range(
                        placement["x"], placement["x"] + placement["width"]
                    )
                    for row in range(
                        placement["y"], placement["y"] + placement["height"]
                    )
                ):
                    self.assertNotIn(cell, occupied)
                    occupied.add(cell)

        for panel, trends in expected_trends.items():
            for key, chart in trends.items():
                self.assertEqual(definitions[panel]["widgets"][key]["chartType"], chart)
        level_mix = definitions["operations"]["widgets"]["turn_level_distribution"]
        self.assertIn("not proof of task success", level_mix["description"])
        for key in ("turn_latency_trend", "generation_latency_trend"):
            latency_trend = definitions["operations"]["widgets"][key]
            self.assertEqual(
                latency_trend["metrics"], [{"measure": "latency", "agg": "p95"}]
            )

    def test_tool_charts_disclose_project_scope_and_top_twenty_caps(self) -> None:
        widgets = agent_dashboard_definition.definitions()["performance"]["widgets"]
        for key in ("tool_names", "tool_errors", "tool_p50", "tool_p95"):
            with self.subTest(widget=key):
                widget = widgets[key]
                self.assertEqual(widget["chartType"], "HORIZONTAL_BAR")
                self.assertEqual(
                    widget["chartConfig"],
                    {"type": "HORIZONTAL_BAR", "row_limit": 20},
                )
                self.assertIn("Top 20", widget["description"])
                self.assertIn("other integrations", widget["description"].lower())
                self.assertIn(
                    "authenticated Langfuse Observations table",
                    widget["description"],
                )
                self.assertIn(agent_dashboard_definition.TOOL[0], widget["filters"])

        self.assertIn("observed lower bound", widgets["tool_errors"]["description"])

    def test_token_mix_widgets_use_langfuse_v2_generation_measures(self) -> None:
        # Pinned to langfuse/langfuse v4.82.0, packages/shared/src/features/query/
        # dataModel.ts: viewDeclarations.v2.observations (eventsObservationsView).
        supported_v2_measures = {"inputTokens", "outputTokens"}
        widgets = agent_dashboard_definition.definitions()["performance"]["widgets"]
        expected = {
            "model_input_tokens": (
                "inputTokens",
                "providedModelName",
                "HORIZONTAL_BAR",
            ),
            "model_output_tokens": (
                "outputTokens",
                "providedModelName",
                "HORIZONTAL_BAR",
            ),
            "input_tokens_trend": ("inputTokens", None, "LINE_TIME_SERIES"),
            "output_tokens_trend": ("outputTokens", None, "LINE_TIME_SERIES"),
        }

        generation_filters = agent_dashboard_definition.GENERATION
        for key, (measure, dimension, chart) in expected.items():
            with self.subTest(widget=key):
                widget = widgets[key]
                self.assertIn(widget["metrics"][0]["measure"], supported_v2_measures)
                self.assertEqual(
                    widget["metrics"], [{"measure": measure, "agg": "sum"}]
                )
                self.assertEqual(
                    widget["dimensions"],
                    [{"field": dimension}] if dimension else [],
                )
                self.assertEqual(widget["chartType"], chart)
                for filter_ in generation_filters:
                    self.assertIn(filter_, widget["filters"])
                self.assertIn("provider keys may overlap", widget["description"])

    def test_usage_category_pivot_preserves_raw_keys_by_model(self) -> None:
        # Langfuse v4.82.0 dataModel.ts declares usageType as a paired expansion
        # over usage_details and usageByType as its matching raw value measure.
        widget = agent_dashboard_definition.definitions()["performance"]["widgets"][
            "usage_categories_by_model"
        ]
        self.assertEqual(widget["view"], "observations")
        self.assertEqual(
            widget["dimensions"],
            [{"field": "usageType"}, {"field": "providedModelName"}],
        )
        self.assertEqual(widget["metrics"], [{"measure": "usageByType", "agg": "sum"}])
        self.assertEqual(widget["chartType"], "PIVOT_TABLE")
        self.assertEqual(
            widget["chartConfig"], {"type": "PIVOT_TABLE", "row_limit": 20}
        )
        for filter_ in agent_dashboard_definition.GENERATION:
            self.assertIn(filter_, widget["filters"])
        self.assertIn("Up to 20", widget["description"])
        self.assertIn(
            "additional pairs may be omitted by the cap", widget["description"]
        )
        self.assertIn("not coverage", widget["description"])

    def test_external_change_aborts_before_any_write(self) -> None:
        self.apply()
        state, _ = span_dashboard.read_state(agent_dashboard.STATE_NAME)
        path = (
            span_dashboard.PATHS["dashboard"]
            + "/"
            + state["resources"]["dashboard:performance"]["id"]
        )
        self.client.resources[path]["description"] = "User changed this"
        before = len(self.client.writes)
        with self.assertRaisesRegex(ValueError, "changed outside"):
            self.apply()
        self.assertEqual(before, len(self.client.writes))

    def test_uncertain_write_is_not_retried(self) -> None:
        self.client.fail = True
        with self.assertRaisesRegex(ValueError, "synthetic write failed"):
            self.apply()
        self.client.fail = False
        with self.assertRaisesRegex(ValueError, "earlier dashboard operation"):
            self.apply()
        self.assertEqual(len(self.client.writes), 1)

    def test_unsafe_state_symlink_is_not_followed(self) -> None:
        target = self.root / "other"
        target.write_text("unchanged")
        os.symlink(target, self.root / agent_dashboard.STATE_NAME)
        with self.assertRaises(OSError):
            self.apply()
        self.assertEqual(target.read_text(), "unchanged")


def observation(
    identifier: str, name: str, kind: str, parent: str | None = None, **extra: object
) -> dict:
    return {
        "id": identifier,
        "traceId": "trace-synthetic",
        "parentObservationId": parent,
        "name": name,
        "type": kind,
        "startTime": "2026-11-20T00:00:00Z",
        "endTime": "2026-11-20T00:00:01Z",
        "sessionId": "session-synthetic",
        "level": "DEFAULT",
        "isRootObservation": False,
        "model": None,
        "latency": 1,
        "usageDetails": {},
        **extra,
    }


class PageClient:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.requests: list[str] = []
        self.response_bytes_received = 0
        self.on_request = None

    def request(
        self,
        method: str,
        path: str,
        *,
        timeout: float = 30,
        max_response_bytes: int = span_dashboard.MAX_RESPONSE,
    ) -> dict:
        assert method == "GET"
        assert timeout > 0
        self.requests.append(path)
        if self.on_request is not None:
            self.on_request()
        page = copy.deepcopy(self.pages[len(self.requests) - 1])
        response_size = len(json.dumps(page, separators=(",", ":")).encode("utf-8"))
        if response_size > max_response_bytes:
            raise report.BudgetExceeded(
                "Report total response-byte budget exceeded; narrow the window and retry."
            )
        self.response_bytes_received += response_size
        return page


class GeneratedPageClient:
    def __init__(self, count: int) -> None:
        self.count = count
        self.response_bytes_received = 0
        self.requests = 0

    def request(
        self,
        method: str,
        path: str,
        *,
        timeout: float = 30,
        max_response_bytes: int = span_dashboard.MAX_RESPONSE,
    ) -> dict:
        assert method == "GET"
        assert timeout > 0
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        limit = int(query["limit"][0])
        start = int(query.get("cursor", ["0"])[0])
        end = min(start + limit, self.count)
        page = {
            "data": [
                observation(f"synthetic-{index}", "synthetic.other", "SPAN")
                for index in range(start, end)
            ],
            "meta": {"cursor": str(end)} if end < self.count else {},
        }
        response_size = len(json.dumps(page, separators=(",", ":")).encode("utf-8"))
        if response_size > max_response_bytes:
            raise report.BudgetExceeded(
                "Report total response-byte budget exceeded; narrow the window and retry."
            )
        self.response_bytes_received += response_size
        self.requests += 1
        return page


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = dt.datetime(2026, 11, 19, tzinfo=dt.timezone.utc)
        self.end = dt.datetime(2026, 11, 21, tzinfo=dt.timezone.utc)

    def test_graph_attribution_unknowns_and_sensitive_field_exclusion(self) -> None:
        rows = [
            observation(
                "turn",
                "opencode.turn",
                "AGENT",
                isRootObservation=True,
                input="SYNTHETIC_SECRET",
            ),
            observation(
                "gen",
                "opencode.generation",
                "GENERATION",
                "turn",
                model="synthetic-model",
                usageDetails={"input": 10, "output": 2, "total": 12},
                output="SYNTHETIC_SECRET",
            ),
            observation(
                "tool",
                "read",
                "TOOL",
                "gen",
                level="ERROR",
                statusMessage="SYNTHETIC_SECRET",
            ),
            observation(
                "child", "opencode.turn", "AGENT", "tool", sessionId="child-session"
            ),
            observation(
                "child-gen",
                "opencode.generation",
                "GENERATION",
                "child",
                model="synthetic-model",
                usageDetails={},
            ),
            observation(
                "child-tool", "read", "TOOL", "child-gen", sessionId="child-session"
            ),
            observation("unrelated", "shell", "TOOL"),
            observation("orphan", "shell", "TOOL", "missing"),
            observation("cycle-a", "shell", "TOOL", "cycle-b"),
            observation("cycle-b", "something", "EVENT", "cycle-a"),
            observation(
                "other-trace", "shell", "TOOL", "turn", traceId="different-trace"
            ),
            observation("retry", "opencode.generation.retry", "EVENT", "turn"),
        ]
        selected = [report.select(row) for row in rows]
        value = report.build_report(
            selected, self.start, self.end, include_identifiers=True
        )
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(value))
        self.assertEqual(value["coverage"]["root_turns"], 1)
        self.assertEqual(value["coverage"]["turns"], 2)
        self.assertEqual(value["coverage"]["attached_generations"], 2)
        self.assertEqual(value["coverage"]["attached_tools"], 2)
        self.assertEqual(value["coverage"]["unattached_project_tools"], 4)
        self.assertEqual(value["linked_tools"]["marked_error"], 1)
        self.assertEqual(
            value["linked_generations"]["reported_tokens"]["total"],
            {"sum": 12, "reported": 1, "missing": 1},
        )
        self.assertIsNone(
            value["linked_generations"]["reported_tokens"]["reasoning"]["sum"]
        )
        self.assertEqual(value["linked_retry_events"], 1)
        by_id = {item["observation_id"]: item for item in value["turn_details"]}
        self.assertEqual(
            {x["observation_id"] for x in by_id["turn"]["timeline"]},
            {"gen", "tool", "retry", "child"},
        )
        self.assertEqual(value["coverage"]["linked_child_turns"], 1)
        self.assertEqual(
            {x["observation_id"] for x in by_id["child"]["timeline"]},
            {"child-gen", "child-tool"},
        )
        self.assertIn("not establish task success", value["unknown"]["task_completion"])
        without_ids = report.build_report(selected, self.start, self.end)
        self.assertNotIn("turn_details", without_ids)
        self.assertNotIn("session-synthetic", json.dumps(without_ids))

    def test_linked_tool_long_tail_and_elapsed_latency_coverage(self) -> None:
        base = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)

        def iso(value: dt.datetime) -> str:
            return value.isoformat().replace("+00:00", "Z")

        rows = [
            observation(
                "turn",
                "opencode.turn",
                "AGENT",
                isRootObservation=True,
                startTime=iso(base),
                endTime=iso(base + dt.timedelta(minutes=1)),
            )
        ]
        for index in range(24):
            start = base
            end = base + dt.timedelta(seconds=index + 1)
            overrides: dict[str, object] = {
                "startTime": iso(start),
                "endTime": iso(end),
                # The source latency scalar is deliberately inconsistent: T5 is
                # defined from the selected interval endpoints.
                "latency": 999,
                "level": "ERROR" if index == 0 else "DEFAULT",
            }
            if index == 1:
                overrides.update(endTime=None, level=None)
            elif index == 2:
                overrides["startTime"] = "SYNTHETIC_INVALID_TIMESTAMP"
            elif index == 3:
                overrides.update(
                    startTime=iso(base + dt.timedelta(seconds=2)),
                    endTime=iso(base + dt.timedelta(seconds=1)),
                )
            rows.append(
                observation(
                    f"tool-{index:02d}",
                    f"synthetic-tool-{index:02d}",
                    "TOOL",
                    "turn",
                    **overrides,
                )
            )

        rows.append(
            observation(
                "tool-repeat",
                "synthetic-tool-04",
                "TOOL",
                "turn",
                startTime=iso(base),
                endTime=iso(base + dt.timedelta(seconds=10)),
                latency=999,
            )
        )
        # A project-wide TOOL observation with no observed turn ancestry is not
        # silently attributed to the linked harness population.
        rows.append(
            observation(
                "project-only",
                "synthetic-project-only",
                "TOOL",
                level="ERROR",
            )
        )

        selected = [report.select(row) for row in rows]
        value = report.build_report(
            selected, self.start, self.end, include_identifiers=True
        )
        value_v2 = report.build_report(
            selected,
            self.start,
            self.end,
            include_identifiers=True,
            schema_version="2.0",
        )
        self.assertNotIn("SYNTHETIC_INVALID_TIMESTAMP", json.dumps(value))
        linked = value["linked_tools"]
        linked_v2 = value_v2["linked_tools"]
        by_name = {item["name"]: item for item in linked["by_name"]}
        by_name_v2 = {item["name"]: item for item in linked_v2["by_name"]}
        self.assertEqual(value["schema_version"], "1.0")
        self.assertEqual(value_v2["schema_version"], "2.0")
        self.assertEqual(value["coverage"]["attached_tools"], 25)
        self.assertEqual(value["coverage"]["unattached_project_tools"], 1)
        self.assertEqual(linked["observations"], 25)
        self.assertEqual(len(by_name), 24)
        self.assertEqual(
            list(by_name), [f"synthetic-tool-{index:02d}" for index in range(24)]
        )
        self.assertNotIn("synthetic-project-only", by_name)
        self.assertEqual(linked["marked_error"], 1)
        self.assertEqual(linked["level_missing"], 1)

        # Schema 1.0 retains the source-latency scalar's exact legacy meaning.
        self.assertEqual(
            set(linked["latency"]), {"unit", "reported", "missing", "p50", "p95"}
        )
        self.assertEqual(
            (
                linked["latency"]["reported"],
                linked["latency"]["missing"],
                linked["latency"]["p50"],
                linked["latency"]["p95"],
            ),
            (25, 0, 999, 999),
        )
        self.assertNotIn("elapsed_latency", linked)
        self.assertNotIn("elapsed_latency", linked["by_name"][0])

        latency = linked_v2["elapsed_latency"]
        self.assertEqual(linked_v2["latency"], linked["latency"])
        self.assertEqual(latency["eligible"], 25)
        self.assertEqual(latency["reported"], 22)
        self.assertEqual(latency["missing"], 1)
        self.assertEqual(latency["invalid"], 2)
        self.assertEqual((latency["p50"], latency["p95"]), (13, 23))
        self.assertEqual(latency["duration_basis"], "endTime - startTime")
        self.assertEqual(latency["quantile_method"], "nearest-rank (ceil(p*n)-1)")
        self.assertEqual(by_name_v2["synthetic-tool-00"]["latency"]["p50"], 999)
        self.assertEqual(by_name_v2["synthetic-tool-00"]["elapsed_latency"]["p50"], 1)
        self.assertEqual(by_name["synthetic-tool-01"]["latency"]["missing"], 0)
        self.assertEqual(
            by_name_v2["synthetic-tool-01"]["elapsed_latency"]["missing"], 1
        )
        self.assertEqual(
            by_name_v2["synthetic-tool-02"]["elapsed_latency"]["invalid"], 1
        )
        self.assertEqual(
            by_name_v2["synthetic-tool-03"]["elapsed_latency"]["invalid"], 1
        )
        self.assertEqual(
            (
                by_name_v2["synthetic-tool-04"]["elapsed_latency"]["p50"],
                by_name_v2["synthetic-tool-04"]["elapsed_latency"]["p95"],
            ),
            (5, 10),
        )

        timeline_v1 = {
            item["observation_id"]: item
            for item in value["turn_details"][0]["timeline"]
        }
        self.assertEqual(timeline_v1["tool-00"]["latency_seconds"], 999)
        self.assertNotIn("elapsed_latency_status", timeline_v1["tool-00"])
        timeline = {
            item["observation_id"]: item
            for item in value_v2["turn_details"][0]["timeline"]
        }
        self.assertEqual(timeline["tool-00"]["latency_seconds"], 999)
        self.assertEqual(timeline["tool-00"]["elapsed_latency_status"], "reported")
        self.assertEqual(timeline["tool-00"]["elapsed_latency_seconds"], 1)
        self.assertEqual(timeline["tool-01"]["elapsed_latency_status"], "missing")
        self.assertEqual(timeline["tool-02"]["elapsed_latency_status"], "invalid")

    def test_invalid_tool_timestamp_precedes_a_missing_endpoint(self) -> None:
        rows = [
            report.select(
                observation("turn", "opencode.turn", "AGENT", isRootObservation=True)
            ),
            report.select(
                observation(
                    "invalid-string",
                    "synthetic-tool",
                    "TOOL",
                    "turn",
                    startTime="SYNTHETIC_INVALID_TIMESTAMP",
                    endTime=None,
                )
            ),
            report.select(
                observation(
                    "invalid-type",
                    "synthetic-tool",
                    "TOOL",
                    "turn",
                    startTime={"value": "SYNTHETIC_PRIVATE"},
                    endTime=None,
                )
            ),
            report.select(
                observation(
                    "overflow-timezone",
                    "synthetic-tool",
                    "TOOL",
                    "turn",
                    startTime="0001-01-01T00:00:00+23:59",
                    endTime="2026-11-20T00:00:01Z",
                )
            ),
            report.select(
                observation(
                    "missing-only",
                    "synthetic-tool",
                    "TOOL",
                    "turn",
                    endTime=None,
                )
            ),
        ]

        value = report.build_report(
            rows,
            self.start,
            self.end,
            include_identifiers=True,
            schema_version="2.0",
        )
        latency = value["linked_tools"]["elapsed_latency"]
        self.assertEqual(latency["eligible"], 4)
        self.assertEqual(latency["reported"], 0)
        self.assertEqual(latency["missing"], 1)
        self.assertEqual(latency["invalid"], 3)
        self.assertIsNone(latency["p50"])
        serialized = json.dumps(value)
        self.assertNotIn("SYNTHETIC_INVALID_TIMESTAMP", serialized)
        self.assertNotIn("0001-01-01", serialized)
        timeline = {
            item["observation_id"]: item
            for item in value["turn_details"][0]["timeline"]
        }
        self.assertEqual(
            timeline["overflow-timezone"]["elapsed_latency_status"], "invalid"
        )
        self.assertIsNone(timeline["overflow-timezone"]["start_time"])
        with self.assertRaisesRegex(ValueError, "timezone"):
            report.timestamp("0001-01-01T00:00:00+23:59")

    def test_linked_tool_name_budget_fails_instead_of_truncating(self) -> None:
        rows = [
            report.select(
                observation("turn", "opencode.turn", "AGENT", isRootObservation=True)
            )
        ]
        rows.extend(
            report.select(
                observation(f"tool-{index}", f"synthetic-tool-{index}", "TOOL", "turn")
            )
            for index in range(report.MAX_GROUPS_PER_BREAKDOWN + 1)
        )
        with self.assertRaisesRegex(
            report.BudgetExceeded, "cardinality budget exceeded"
        ):
            report.build_report(rows, self.start, self.end)

    def test_token_usage_separates_reported_missing_and_excluded_values(self) -> None:
        selected = report.select(
            observation(
                "generation",
                "opencode.generation",
                "GENERATION",
                usageDetails={
                    "input": 0,
                    "output": None,
                    "total": "SYNTHETIC_INVALID_TOKEN",
                    "reasoning": -1,
                    "cache_read": True,
                    "cache_write": float("inf"),
                    "uncontracted": "SYNTHETIC_SECRET",
                },
            )
        )

        selected_json = json.dumps(selected, allow_nan=False)
        self.assertNotIn("SYNTHETIC_INVALID_TOKEN", selected_json)
        self.assertNotIn("SYNTHETIC_SECRET", selected_json)
        self.assertEqual(
            selected["usageDetailsExcluded"],
            ("total", "reasoning", "cache_read", "cache_write"),
        )

        usage = report.usage_summary([selected], schema_version="2.0")
        self.assertEqual(
            usage["input"],
            {"sum": 0, "reported": 1, "missing": 0, "excluded": 0},
        )
        self.assertEqual(
            usage["output"],
            {"sum": None, "reported": 0, "missing": 1, "excluded": 0},
        )
        for key in ("total", "reasoning", "cache_read", "cache_write"):
            self.assertEqual(
                usage[key],
                {"sum": None, "reported": 0, "missing": 0, "excluded": 1},
            )

        malformed_map = report.select(
            observation(
                "malformed-map",
                "opencode.generation",
                "GENERATION",
                usageDetails="SYNTHETIC_INVALID_CONTAINER",
            )
        )
        self.assertNotIn("SYNTHETIC_INVALID_CONTAINER", json.dumps(malformed_map))
        self.assertEqual(
            report.usage_summary([malformed_map], schema_version="2.0")["input"],
            {"sum": None, "reported": 0, "missing": 0, "excluded": 1},
        )

    def test_schema_two_is_opt_in_for_exclusive_token_missingness(self) -> None:
        rows = [
            report.select(
                observation("turn", "opencode.turn", "AGENT", isRootObservation=True)
            ),
            report.select(
                observation(
                    "generation",
                    "opencode.generation",
                    "GENERATION",
                    "turn",
                    usageDetails={
                        "input": 0,
                        "output": None,
                        "total": "SYNTHETIC_INVALID_TOKEN",
                    },
                )
            ),
        ]

        legacy = report.build_report(rows, self.start, self.end)
        version_two = report.build_report(
            rows, self.start, self.end, schema_version="2.0"
        )
        legacy_tokens = legacy["linked_generations"]["reported_tokens"]
        version_two_tokens = version_two["linked_generations"]["reported_tokens"]

        self.assertEqual(legacy["schema_version"], "1.0")
        self.assertEqual(version_two["schema_version"], "2.0")
        self.assertEqual(
            legacy_tokens["total"],
            {"sum": None, "reported": 0, "missing": 1},
        )
        self.assertEqual(
            version_two_tokens["total"],
            {"sum": None, "reported": 0, "missing": 0, "excluded": 1},
        )
        self.assertEqual(
            version_two_tokens["input"],
            {"sum": 0, "reported": 1, "missing": 0, "excluded": 0},
        )
        self.assertEqual(
            version_two_tokens["output"],
            {"sum": None, "reported": 0, "missing": 1, "excluded": 0},
        )
        self.assertNotIn("excluded", legacy_tokens["total"])
        self.assertNotIn("SYNTHETIC_INVALID_TOKEN", json.dumps(version_two))

    def test_cli_schema_version_selects_the_requested_report_contract(self) -> None:
        rows = [
            report.select(
                observation("turn", "opencode.turn", "AGENT", isRootObservation=True)
            ),
            report.select(
                observation(
                    "generation",
                    "opencode.generation",
                    "GENERATION",
                    "turn",
                    usageDetails={"input": "SYNTHETIC_INVALID_TOKEN"},
                )
            ),
        ]
        output = io.StringIO()

        with (
            mock.patch.object(
                report.common,
                "report_query_lock",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(
                report,
                "enforce_wall_time_budget",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(
                report.span_dashboard,
                "context",
                return_value=(object(), None, None),
            ),
            mock.patch.object(
                report, "fetch_observations", return_value=(rows, 0)
            ) as fetch,
            contextlib.redirect_stdout(output),
        ):
            report.main(
                [
                    "--from-time",
                    self.start.isoformat(),
                    "--to-time",
                    self.end.isoformat(),
                    "--schema-version",
                    "2.0",
                ]
            )

        result = json.loads(output.getvalue())
        self.assertEqual(result["schema_version"], "2.0")
        self.assertEqual(
            result["linked_generations"]["reported_tokens"]["input"],
            {"sum": None, "reported": 0, "missing": 0, "excluded": 1},
        )
        self.assertNotIn("SYNTHETIC_INVALID_TOKEN", output.getvalue())
        fetch.assert_called_once()

    def test_utc_buckets_report_each_token_field_for_linked_generations(self) -> None:
        start = dt.datetime(2026, 11, 8, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc)
        rows = [
            observation(
                "turn-sunday",
                "opencode.turn",
                "AGENT",
                isRootObservation=True,
                startTime="2026-11-08T23:59:00Z",
            ),
            observation(
                "gen-sunday",
                "opencode.generation",
                "GENERATION",
                "turn-sunday",
                startTime="2026-11-08T23:59:10Z",
                model="model-a",
                usageDetails={"input": 0, "total": 0},
            ),
            observation(
                "gen-sunday-a2",
                "opencode.generation",
                "GENERATION",
                "turn-sunday",
                startTime="2026-11-08T23:59:15Z",
                model="model-a",
                usageDetails={"input": 2, "output": 2},
            ),
            observation(
                "gen-sunday-2",
                "opencode.generation",
                "GENERATION",
                "turn-sunday",
                startTime="2026-11-08T23:59:20Z",
                model="model-b",
                usageDetails={"input": 3, "output": 4},
            ),
            observation(
                "turn-monday",
                "opencode.turn",
                "AGENT",
                isRootObservation=True,
                startTime="2026-11-09T00:01:00Z",
            ),
            observation(
                "gen-monday",
                "opencode.generation",
                "GENERATION",
                "turn-monday",
                startTime="2026-11-09T00:01:10Z",
                model="model-a",
                usageDetails={"output": 5, "reasoning": "SYNTHETIC_INVALID_TOKEN"},
            ),
            observation(
                "unlinked-gen",
                "opencode.generation",
                "GENERATION",
                "missing-parent",
                startTime="2026-11-09T00:02:00Z",
                model="unlinked-model",
                usageDetails={"input": 999, "total": 999},
            ),
        ]
        result = report.build_report([report.select(row) for row in rows], start, end)
        by_day = {bucket["day"]: bucket for bucket in result["by_day_utc"]}
        sunday_bucket = by_day["2026-11-08"]
        monday_bucket = by_day["2026-11-09"]
        sunday = sunday_bucket["generation_token_usage"]
        monday = monday_bucket["generation_token_usage"]
        self.assertEqual(set(sunday), set(report.TOKEN_KEYS))
        self.assertEqual(sunday_bucket["generation"], 3)
        self.assertEqual(
            sunday["input"],
            {"sum": 5, "reported": 3, "missing": 0},
        )
        self.assertEqual(
            sunday["total"],
            {"sum": 0, "reported": 1, "missing": 2},
        )
        self.assertEqual(
            sunday["output"],
            {"sum": 6, "reported": 2, "missing": 1},
        )
        sunday_by_model = {
            item["model"]: item
            for item in sunday_bucket["generation_token_usage_by_model"]
        }
        self.assertEqual(set(sunday_by_model), {"model-a", "model-b"})
        self.assertEqual(sunday_by_model["model-a"]["observations"], 2)
        self.assertEqual(
            set(sunday_by_model["model-a"]["generation_token_usage"]),
            set(report.TOKEN_KEYS),
        )
        self.assertEqual(
            sunday_by_model["model-a"]["generation_token_usage"]["input"],
            {"sum": 2, "reported": 2, "missing": 0},
        )
        self.assertEqual(
            sunday_by_model["model-a"]["generation_token_usage"]["output"],
            {"sum": 2, "reported": 1, "missing": 1},
        )
        self.assertEqual(
            sunday_by_model["model-b"]["generation_token_usage"]["input"],
            {"sum": 3, "reported": 1, "missing": 0},
        )
        self.assertEqual(
            monday["input"],
            {"sum": None, "reported": 0, "missing": 1},
        )
        self.assertEqual(
            monday["output"],
            {"sum": 5, "reported": 1, "missing": 0},
        )
        self.assertEqual(
            monday["reasoning"],
            {"sum": None, "reported": 0, "missing": 1},
        )
        self.assertNotIn("SYNTHETIC_INVALID_TOKEN", json.dumps(result))
        monday_by_model = {
            item["model"]: item
            for item in monday_bucket["generation_token_usage_by_model"]
        }
        self.assertEqual(set(monday_by_model), {"model-a"})
        self.assertEqual(
            monday_by_model["model-a"]["generation_token_usage"]["output"],
            {"sum": 5, "reported": 1, "missing": 0},
        )
        self.assertEqual(sunday_bucket["reported_total_tokens"], 0)
        self.assertEqual(sunday_bucket["generations_with_total_tokens"], 1)
        self.assertNotIn("reported_total_tokens", monday_bucket)
        self.assertNotIn("2026-11-10", by_day)

        by_week = {bucket["week"]: bucket for bucket in result["by_iso_week_utc"]}
        self.assertEqual(
            by_week["2026-W45"]["generation_token_usage"]["input"],
            {"sum": 5, "reported": 3, "missing": 0},
        )
        self.assertEqual(
            by_week["2026-W46"]["generation_token_usage"]["output"],
            {"sum": 5, "reported": 1, "missing": 0},
        )
        self.assertEqual(
            [
                item["model"]
                for item in by_week["2026-W45"]["generation_token_usage_by_model"]
            ],
            ["model-a", "model-b"],
        )
        self.assertEqual(result["coverage"]["unattached_generation_candidates"], 1)

    def test_generations_without_valid_start_time_are_counted_but_not_bucketed(
        self,
    ) -> None:
        start = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 11, 21, tzinfo=dt.timezone.utc)
        rows = [
            observation(
                "turn",
                "opencode.turn",
                "AGENT",
                isRootObservation=True,
                startTime="2026-11-20T00:00:00Z",
            ),
            observation(
                "gen-missing-start",
                "opencode.generation",
                "GENERATION",
                "turn",
                startTime=None,
                endTime="2026-11-20T00:00:01Z",
                usageDetails={"input": 2},
            ),
            observation(
                "gen-invalid-start",
                "opencode.generation",
                "GENERATION",
                "turn",
                startTime="not-a-timestamp",
                endTime="2026-11-20T00:00:02Z",
                usageDetails={"input": 3},
            ),
        ]
        result = report.build_report([report.select(row) for row in rows], start, end)

        self.assertEqual(result["coverage"]["attached_generations"], 2)
        self.assertEqual(result["coverage"]["missing_or_invalid_start_time"], 2)
        self.assertEqual(
            result["coverage"]["linked_generations_without_valid_start_time"], 2
        )
        self.assertEqual(
            result["linked_generations"]["reported_tokens"]["input"]["sum"], 5
        )
        day_bucket = result["by_day_utc"][0]
        week_bucket = result["by_iso_week_utc"][0]
        self.assertNotIn("generation", day_bucket)
        self.assertNotIn("generation", week_bucket)
        self.assertIsNone(day_bucket["generation_token_usage"]["input"]["sum"])

    def test_empty_page_with_cursor_and_exact_duplicates(self) -> None:
        row = observation("turn", "opencode.turn", "AGENT")
        changed_discarded_field = {**row, "statusMessage": "SYNTHETIC_DISCARDED"}
        client = PageClient(
            [
                {"data": [row, row], "meta": {"cursor": "first"}},
                {"data": [], "meta": {"cursor": "second"}},
                {"data": [changed_discarded_field], "meta": {}},
            ]
        )
        result, duplicates = report.fetch_observations(client, self.start, self.end, 10)
        self.assertEqual((len(result), duplicates, len(client.requests)), (1, 2, 3))
        self.assertNotIn("SYNTHETIC_DISCARDED", json.dumps(result))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(client.requests[0]).query)
        self.assertEqual(query["fields"], ["basic,model,usage,metrics"])
        self.assertNotIn("io", query["fields"][0].split(","))

    def test_changed_invalid_usage_value_is_not_miscounted_as_exact_duplicate(
        self,
    ) -> None:
        row = observation(
            "generation",
            "opencode.generation",
            "GENERATION",
            usageDetails={"input": "SYNTHETIC_INVALID_FIRST"},
        )
        changed = {**row, "usageDetails": {"input": "SYNTHETIC_INVALID_SECOND"}}
        client = PageClient(
            [
                {"data": [row], "meta": {"cursor": "next"}},
                {"data": [changed], "meta": {}},
            ]
        )

        with self.assertRaisesRegex(ValueError, "changed during the scan") as raised:
            report.fetch_observations(client, self.start, self.end, 10)
        self.assertNotIn("SYNTHETIC_INVALID", str(raised.exception))

    def test_changed_invalid_tool_interval_is_not_miscounted_as_duplicate(self) -> None:
        row = observation(
            "tool",
            "synthetic-tool",
            "TOOL",
            startTime={"value": "SYNTHETIC_PRIVATE_FIRST"},
        )
        changed = {
            **row,
            "startTime": {"value": "SYNTHETIC_PRIVATE_SECOND"},
        }
        client = PageClient(
            [
                {"data": [row], "meta": {"cursor": "next"}},
                {"data": [changed], "meta": {}},
            ]
        )

        with self.assertRaisesRegex(ValueError, "changed during the scan") as raised:
            report.fetch_observations(client, self.start, self.end, 10)
        self.assertNotIn("SYNTHETIC_PRIVATE", str(raised.exception))

    def test_conflicting_duplicate_cycle_and_truncation_fail_closed(self) -> None:
        row = observation("turn", "opencode.turn", "AGENT")
        for pages, expected in [
            (
                [
                    {"data": [row], "meta": {"cursor": "next"}},
                    {"data": [{**row, "level": "ERROR"}], "meta": {}},
                ],
                "changed during",
            ),
            (
                [
                    {"data": [], "meta": {"cursor": "same"}},
                    {"data": [], "meta": {"cursor": "same"}},
                ],
                "repeated",
            ),
        ]:
            with (
                self.subTest(expected=expected),
                self.assertRaisesRegex(ValueError, expected),
            ):
                report.fetch_observations(PageClient(pages), self.start, self.end, 10)
        with self.assertRaisesRegex(ValueError, "limit reached"):
            report.fetch_observations(
                PageClient([{"data": [row], "meta": {"cursor": "next"}}]),
                self.start,
                self.end,
                1,
            )

    def test_page_row_and_total_response_byte_budgets_fail_closed(self) -> None:
        oversized_page = PageClient(
            [
                {
                    "data": [observation(str(i), "other", "SPAN") for i in range(251)],
                    "meta": {},
                }
            ]
        )
        with self.assertRaisesRegex(report.BudgetExceeded, "page row budget"):
            report.fetch_observations(oversized_page, self.start, self.end, 1000)

        client = PageClient([{"data": [], "meta": {}}])
        with self.assertRaisesRegex(
            report.BudgetExceeded, "total response-byte budget"
        ):
            report.fetch_observations(
                client,
                self.start,
                self.end,
                10,
                budget=report.ReportBudget(max_response_bytes=1),
            )

    def test_slow_page_exhausts_end_to_end_deadline(self) -> None:
        class Clock:
            now = 0.0

            def __call__(self) -> float:
                return self.now

        clock = Clock()
        client = PageClient([{"data": [], "meta": {}}])
        client.on_request = lambda: setattr(clock, "now", 121.0)
        with self.assertRaisesRegex(report.BudgetExceeded, "wall-time budget"):
            report.fetch_observations(
                client,
                self.start,
                self.end,
                10,
                budget=report.ReportBudget(max_seconds=120, clock=clock),
            )

    def test_request_admission_allows_only_one_in_flight_request(self) -> None:
        budget = report.ReportBudget()
        self.assertGreater(budget.admit_request(), 0)
        try:
            with self.assertRaisesRegex(report.BudgetExceeded, "concurrency budget"):
                budget.admit_request()
        finally:
            budget.release_request()

    def test_generated_scale_fixtures_stay_within_declared_budgets(self) -> None:
        for count in (100, 10_000, 100_000):
            with self.subTest(observations=count):
                client = GeneratedPageClient(count)
                budget = report.ReportBudget()
                rows, duplicates = report.fetch_observations(
                    client, self.start, self.end, count, budget=budget
                )
                result = report.build_report(
                    rows, self.start, self.end, duplicates, budget=budget
                )
                self.assertEqual(result["coverage"]["observations_scanned"], count)
                self.assertLessEqual(client.requests, report.MAX_PAGES)
                self.assertLessEqual(
                    budget.response_bytes, report.MAX_TOTAL_RESPONSE_BYTES
                )
                self.assertLessEqual(
                    len(report.serialize_report(result, budget=budget).encode()),
                    report.MAX_OUTPUT_BYTES,
                )

    def test_high_model_cardinality_and_output_bytes_fail_without_truncation(
        self,
    ) -> None:
        rows = [report.select(observation("turn", "opencode.turn", "AGENT"))]
        rows.extend(
            report.select(
                observation(
                    f"generation-{index}",
                    "opencode.generation",
                    "GENERATION",
                    "turn",
                    model=f"synthetic-model-{index}",
                )
            )
            for index in range(report.MAX_GROUPS_PER_BREAKDOWN + 1)
        )
        with self.assertRaisesRegex(report.BudgetExceeded, "Model cardinality budget"):
            report.build_report(rows, self.start, self.end)

        with self.assertRaisesRegex(report.BudgetExceeded, "output-byte budget"):
            report.serialize_report({"synthetic": "x" * 100}, maximum=32)

    def test_session_filter_is_bounded_and_does_not_appear_in_default_report(
        self,
    ) -> None:
        client = PageClient([{"data": [], "meta": {}}])
        rows, duplicates = report.fetch_observations(
            client, self.start, self.end, 10, "session-test"
        )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(client.requests[0]).query)
        self.assertEqual(query["sessionId"], ["session-test"])
        result = report.build_report(
            rows, self.start, self.end, duplicates, session_scoped=True
        )
        self.assertEqual(result["window"]["scope"], "one session")
        self.assertNotIn("session-test", json.dumps(result))
        with self.assertRaisesRegex(ValueError, "Invalid OpenCode session ID"):
            report.fetch_observations(
                PageClient([]), self.start, self.end, 10, "invalid/path"
            )

    def test_numeric_and_time_validation(self) -> None:
        self.assertIsNone(report.scalar_number(True))
        self.assertIsNone(report.scalar_number(float("nan")))
        self.assertIsNone(report.scalar_number(-1))
        self.assertEqual(report.scalar_number(10**1000), 10**1000)
        self.assertEqual(report.percentile([5, 1, 3, 2, 4], 0.95), 5)
        with self.assertRaisesRegex(ValueError, "timezone"):
            report.timestamp("2026-11-20T00:00:00")

    def test_unexpected_field_types_are_not_echoed_or_sorted(self) -> None:
        row = observation(
            "turn",
            "opencode.turn",
            "AGENT",
            isRootObservation=True,
            level={"secret": "SYNTHETIC_SECRET"},
            startTime={"secret": "SYNTHETIC_SECRET"},
        )
        result = report.build_report(
            [report.select(row)], self.start, self.end, include_identifiers=True
        )
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))
        self.assertIsNone(result["turn_details"][0]["level"])

    def test_name_only_cycle_candidates_do_not_claim_an_error(self) -> None:
        def tools(names: list[str]) -> list[dict]:
            return [
                report.select(observation(str(i), name, "TOOL"))
                for i, name in enumerate(names)
            ]

        repeated = report.tool_sequence_candidates(tools(["read", "read", "read"]))
        self.assertTrue(repeated["same_name_three_times"])
        self.assertFalse(repeated["alternating_pair_twice"])
        alternating = report.tool_sequence_candidates(
            tools(["read", "grep", "read", "grep"])
        )
        self.assertTrue(alternating["alternating_pair_twice"])
        missing = tools(["read"])
        missing[0]["startTime"] = None
        self.assertIsNone(
            report.tool_sequence_candidates(missing)["same_name_three_times"]
        )

    def test_long_shared_ancestry_is_attributed(self) -> None:
        rows = [
            report.select(
                observation("root", "opencode.turn", "AGENT", isRootObservation=True)
            )
        ]
        parent = "root"
        for i in range(1500):
            current = f"chain-{i}"
            rows.append(report.select(observation(current, "middle", "SPAN", parent)))
            parent = current
        for i in range(100):
            rows.append(report.select(observation(f"tool-{i}", "read", "TOOL", parent)))
        value = report.build_report(rows, self.start, self.end)
        self.assertEqual(value["coverage"]["attached_tools"], 100)

    def test_overlaps_are_unioned_and_missing_intervals_are_not_waits(self) -> None:
        turn = report.select(
            observation(
                "turn", "opencode.turn", "AGENT", endTime="2026-11-20T00:00:10Z"
            )
        )
        first = report.select(
            observation(
                "gen",
                "opencode.generation",
                "GENERATION",
                "turn",
                endTime="2026-11-20T00:00:05Z",
            )
        )
        second = report.select(
            observation(
                "tool",
                "read",
                "TOOL",
                "gen",
                startTime="2026-11-20T00:00:03Z",
                endTime="2026-11-20T00:00:08Z",
            )
        )
        child = report.select(
            observation(
                "child",
                "opencode.turn",
                "AGENT",
                "tool",
                startTime="2026-11-20T00:00:08Z",
                endTime="2026-11-20T00:00:09Z",
            )
        )
        result = report.observed_time(turn, [first, second, child])
        self.assertEqual(result["observed_child_union_seconds"], 9)
        self.assertEqual(result["uncovered_by_observed_children_seconds"], 1)
        second["endTime"] = None
        self.assertIsNone(
            report.observed_time(turn, [first, second])[
                "uncovered_by_observed_children_seconds"
            ]
        )

    def test_cyclic_child_turn_relationships_are_not_emitted(self) -> None:
        rows = [
            report.select(observation("a", "opencode.turn", "AGENT", "b")),
            report.select(observation("b", "opencode.turn", "AGENT", "a")),
        ]
        value = report.build_report(
            rows, self.start, self.end, include_identifiers=True
        )
        self.assertEqual(value["coverage"]["linked_child_turns"], 0)
        self.assertEqual(value["coverage"]["ambiguous_child_turn_links"], 2)
        self.assertTrue(all(not turn["timeline"] for turn in value["turn_details"]))

    def test_cycle_through_turn_does_not_attach_tool(self) -> None:
        rows = [
            report.select(observation("turn", "opencode.turn", "AGENT", "tool")),
            report.select(observation("tool", "read", "TOOL", "turn")),
        ]
        value = report.build_report(rows, self.start, self.end)
        self.assertEqual(value["coverage"]["cycle_affected_observations"], 2)
        self.assertEqual(value["coverage"]["attached_tools"], 0)
        self.assertEqual(value["coverage"]["unattached_project_tools"], 1)
        self.assertEqual(value["tool_sequence_candidates"]["turns_unavailable"], 1)
        self.assertEqual(
            value["turn_timing_coverage"]["turns_without_usable_timing"], 1
        )

    def test_structural_runtime_events_are_not_attributed_to_turns(self) -> None:
        environment = agent_dashboard_definition.RUNTIME_ENVIRONMENT
        rows = [
            report.select(
                observation("turn", "opencode.turn", "AGENT", isRootObservation=True)
            ),
            report.select(
                observation(
                    "event",
                    "aiobs.execution.succeeded",
                    "EVENT",
                    "turn",
                    environment=environment,
                )
            ),
            report.select(
                observation(
                    "foreign",
                    "aiobs.execution.failed",
                    "EVENT",
                    "turn",
                    environment="other",
                )
            ),
            report.select(
                observation(
                    "other-name",
                    "unrecognized",
                    "EVENT",
                    "turn",
                    environment=environment,
                )
            ),
        ]
        value = report.build_report(
            rows, self.start, self.end, include_identifiers=True
        )
        self.assertEqual(
            value["runtime_events"]["by_name"], {"aiobs.execution.succeeded": 1}
        )
        self.assertEqual(value["turn_details"][0]["timeline"], [])
        self.assertIn("not prompt or task outcomes", value["runtime_events"]["scope"])
        self.assertEqual(
            value["sessions"][0]["runtime_events"], {"aiobs.execution.succeeded": 1}
        )

    def test_compaction_usage_is_separate_and_reports_missing_fields(self) -> None:
        environment = agent_dashboard_definition.RUNTIME_ENVIRONMENT
        rows = [
            report.select(
                observation("turn", "opencode.turn", "AGENT", isRootObservation=True)
            ),
            report.select(
                observation(
                    "gen",
                    "opencode.generation",
                    "GENERATION",
                    "turn",
                    usageDetails={"input": 100},
                )
            ),
            report.select(
                observation(
                    "compact",
                    "aiobs.compaction.ended.auto",
                    "EVENT",
                    environment=environment,
                    usageDetails={"input": 10, "output": 3, "cache_read": 0},
                )
            ),
            report.select(
                observation(
                    "failed",
                    "aiobs.compaction.failed.manual",
                    "EVENT",
                    environment=environment,
                )
            ),
            report.select(
                observation(
                    "started",
                    "aiobs.compaction.started.auto",
                    "EVENT",
                    environment=environment,
                    usageDetails={"input": 999},
                )
            ),
        ]
        value = report.build_report(rows, self.start, self.end)
        runtime = value["runtime_events"]
        self.assertEqual(runtime["compaction_terminal_observations"], 2)
        self.assertEqual(
            runtime["compaction_reported_tokens"]["input"],
            {"sum": 10, "reported": 1, "missing": 1},
        )
        self.assertEqual(
            runtime["compaction_reported_tokens"]["cache_read"],
            {"sum": 0, "reported": 1, "missing": 1},
        )
        self.assertIsNone(runtime["compaction_reported_tokens"]["reasoning"]["sum"])
        self.assertNotIn("total", runtime["compaction_reported_tokens"])
        self.assertEqual(
            value["linked_generations"]["reported_tokens"]["input"]["sum"], 100
        )

    def test_permission_waits_require_unique_same_session_ordered_events(self) -> None:
        environment = agent_dashboard_definition.RUNTIME_ENVIRONMENT

        def event(identifier: str, trace: str, name: str, **fields: object) -> dict:
            return report.select(
                observation(
                    identifier,
                    name,
                    "EVENT",
                    traceId=trace,
                    environment=environment,
                    **fields,
                )
            )

        ask = "aiobs.permission.asked"
        reply = "aiobs.permission.replied.once"
        rows = [
            event("a", "good", ask, startTime="2026-11-20T00:00:00Z"),
            event("b", "good", reply, startTime="2026-11-20T00:01:00Z"),
            event("c", "open", ask),
            event("d", "late", reply),
            event("e", "duplicate", ask),
            event("f", "duplicate", ask),
            event("g", "duplicate", reply),
            event("h", "wrong-session", ask),
            event("i", "wrong-session", reply, sessionId="other-session"),
            event("j", "bad-time", ask, startTime=None),
            event("k", "bad-time", reply),
            event("l", "negative", ask, startTime="2026-11-20T00:02:00Z"),
            event("m", "negative", reply, startTime="2026-11-20T00:01:00Z"),
            report.select(
                observation(
                    "foreign", ask, "EVENT", traceId="foreign", environment="other"
                )
            ),
        ]
        result = report.build_report(rows, self.start, self.end)["runtime_events"][
            "permission_waits"
        ]
        self.assertEqual(result["matched_pairs"], 1)
        self.assertEqual(
            result["matched_wait_seconds"], {"count": 1, "p50": 60, "p95": 60}
        )
        self.assertEqual(result["ask_without_reply"], 1)
        self.assertEqual(result["reply_without_ask"], 1)
        self.assertEqual(result["ambiguous_groups"], 1)
        self.assertEqual(result["missing_or_mismatched_session"], 1)
        self.assertEqual(result["invalid_timestamps"], 1)
        self.assertEqual(result["reply_before_ask"], 1)
        self.assertEqual(result["request_groups"], 7)


class GeneratedQueryClient:
    def __init__(self, count: int, kind: str) -> None:
        self.count = count
        self.kind = kind
        self.response_bytes_received = 0
        self.requests = 0

    def request(
        self,
        method: str,
        path: str,
        *,
        timeout: float = 30,
        max_response_bytes: int = span_dashboard.MAX_RESPONSE,
    ) -> dict:
        assert method == "GET"
        assert timeout > 0
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
        limit = int(query["limit"][0])
        start = int(query.get("cursor", ["0"])[0])
        end = min(start + limit, self.count)
        observation_type = "TOOL" if self.kind == "tool" else "GENERATION"
        field = (
            {}
            if self.kind == "tool"
            else {
                "model": "synthetic-model",
                "usageDetails": {"input": 0},
            }
        )
        page = {
            "data": [
                observation(
                    f"synthetic-{index}",
                    "synthetic-tool" if self.kind == "tool" else "opencode.generation",
                    observation_type,
                    **field,
                )
                for index in range(start, end)
            ],
            "meta": {"cursor": str(end)} if end < self.count else {},
        }
        response_size = len(json.dumps(page, separators=(",", ":")).encode("utf-8"))
        if response_size > max_response_bytes:
            raise report.BudgetExceeded("synthetic response-byte budget exceeded")
        self.response_bytes_received += response_size
        self.requests += 1
        return page


class QueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = dt.datetime(2026, 11, 19, tzinfo=dt.timezone.utc)
        self.end = dt.datetime(2026, 11, 21, tzinfo=dt.timezone.utc)

    def query(
        self, pages: list[dict], **kwargs: object
    ) -> tuple[agent_query.ObservationQuery, PageClient]:
        client = PageClient(pages)
        query = agent_query.ObservationQuery(
            client, self.start, self.end, kind="tool", **kwargs
        )
        return query, client

    def test_tool_and_model_search_use_pinned_server_filters(self) -> None:
        cases = (
            ("tool", "name", "TOOL", "shell"),
            ("model", "model", "GENERATION", "local-model"),
        )
        for kind, field, observation_type, term in cases:
            with self.subTest(kind=kind):
                row = observation(
                    "synthetic-row",
                    "shell" if kind == "tool" else "opencode.generation",
                    observation_type,
                    model="local-model" if kind == "model" else None,
                    input="SYNTHETIC_SECRET",
                    output="SYNTHETIC_SECRET",
                    statusMessage="SYNTHETIC_SECRET",
                    metadata={"private": "SYNTHETIC_SECRET"},
                    usageDetails={"input": 0} if kind == "model" else {},
                )
                client = PageClient([{"data": [row], "meta": {}}])
                query = agent_query.ObservationQuery(
                    client,
                    self.start,
                    self.end,
                    kind=kind,
                    search=term,
                    page_size=17,
                )
                output = query.next_page_json()
                value = json.loads(output)
                request = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(client.requests[0]).query
                )
                filter_ = json.loads(request["filter"][0])
                self.assertEqual(request["type"], [observation_type])
                self.assertEqual(request["limit"], ["17"])
                self.assertEqual(filter_[0]["column"], field)
                self.assertEqual(filter_[0]["operator"], "contains")
                self.assertEqual(filter_[0]["value"], term)
                self.assertEqual(request["fields"], [agent_query.QUERY_FIELDS])
                self.assertNotIn("io", request["fields"][0].split(","))
                self.assertEqual(value["evidence_class"], "observed")
                self.assertTrue(value["page"]["query_complete"])
                self.assertNotIn("identifiers", value["observations"][0])
                self.assertNotIn("SYNTHETIC_SECRET", output)
                self.assertNotIn("observation_id", output)
                if kind == "model":
                    self.assertEqual(
                        value["observations"][0]["usage_details"]["input"], 0
                    )
                    self.assertIsNone(
                        value["observations"][0]["usage_details"]["output"]
                    )

    def test_empty_page_with_cursor_is_incomplete_and_cursor_stays_internal(
        self,
    ) -> None:
        pages = [
            {"data": [], "meta": {"cursor": "private-cursor-one"}},
            {
                "data": [observation("tool-one", "read", "TOOL")],
                "meta": {"cursor": "private-cursor-two"},
            },
            {"data": [observation("tool-two", "grep", "TOOL")], "meta": {}},
        ]
        query, client = self.query(pages)
        first = query.next_page_json()
        first_page = json.loads(first)
        self.assertEqual(first_page["observations"], [])
        self.assertFalse(first_page["page"]["query_complete"])
        self.assertTrue(first_page["page"]["continuation_available"])
        self.assertNotIn("private-cursor-one", first)

        second = json.loads(query.next_page_json())
        self.assertFalse(second["page"]["query_complete"])
        self.assertEqual(second["observations"][0]["name"], "read")
        third = json.loads(query.next_page_json())
        self.assertTrue(third["page"]["query_complete"])
        self.assertFalse(third["page"]["continuation_available"])
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(
            urllib.parse.parse_qs(urllib.parse.urlsplit(client.requests[1]).query)[
                "cursor"
            ],
            ["private-cursor-one"],
        )
        self.assertNotIn("private-cursor-two", json.dumps(third))
        with self.assertRaisesRegex(ValueError, "already complete"):
            query.next_page_json()

    def test_empty_terminal_page_is_a_genuine_empty_result(self) -> None:
        query, _ = self.query([{"data": [], "meta": {}}])
        page = json.loads(query.next_page_json())
        self.assertEqual(page["observations"], [])
        self.assertEqual(page["page"]["returned"], 0)
        self.assertTrue(page["page"]["query_complete"])
        self.assertFalse(page["page"]["continuation_available"])

    def test_identifier_detail_is_explicit(self) -> None:
        row = observation("private-observation", "read", "TOOL")
        query, _ = self.query([{"data": [row], "meta": {}}], include_identifiers=True)
        value = json.loads(query.next_page_json())
        self.assertEqual(
            value["observations"][0]["identifiers"],
            {
                "observation_id": "private-observation",
                "trace_id": "trace-synthetic",
                "parent_observation_id": None,
                "session_id": "session-synthetic",
            },
        )

    def test_repeated_cursors_rows_and_page_budget_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "repeated query cursor"):
            query, _ = self.query(
                [
                    {"data": [], "meta": {"cursor": "same"}},
                    {"data": [], "meta": {"cursor": "same"}},
                ]
            )
            query.next_page_json()
            query.next_page_json()

        row = observation("repeated-row", "read", "TOOL")
        query, _ = self.query(
            [
                {"data": [row], "meta": {"cursor": "next"}},
                {"data": [row], "meta": {}},
            ]
        )
        query.next_page_json()
        with self.assertRaisesRegex(ValueError, "repeated an observation"):
            query.next_page_json()

        query, _ = self.query([{"data": [], "meta": {"cursor": "next"}}], max_pages=1)
        with self.assertRaisesRegex(report.BudgetExceeded, "page budget"):
            query.next_page_json()
        with self.assertRaisesRegex(ValueError, "cannot be resumed"):
            query.next_page_json()

    def test_row_response_and_output_budgets_are_explicit(self) -> None:
        rows = [observation(f"row-{i}", "read", "TOOL") for i in range(2)]
        query, _ = self.query([{"data": rows, "meta": {}}], page_size=1)
        with self.assertRaisesRegex(report.BudgetExceeded, "page-row budget"):
            query.next_page_json()

        query, _ = self.query([{"data": rows, "meta": {"cursor": "next"}}], max_rows=1)
        with self.assertRaisesRegex(report.BudgetExceeded, "row budget"):
            query.next_page_json()

        client = PageClient([{"data": [], "meta": {}}])
        query = agent_query.ObservationQuery(
            client,
            self.start,
            self.end,
            kind="tool",
            budget=report.ReportBudget(max_response_bytes=1),
        )
        with self.assertRaisesRegex(report.BudgetExceeded, "response-byte budget"):
            query.next_page_json()

        class OversizedPageClient:
            def request(self, *args: object, **kwargs: object) -> dict:
                raise span_dashboard.ResponseBudgetExceeded(
                    "synthetic response-byte limit exceeded"
                )

        query = agent_query.ObservationQuery(
            OversizedPageClient(),
            self.start,
            self.end,
            kind="tool",
        )
        with self.assertRaisesRegex(report.BudgetExceeded, "response-byte budget"):
            query.next_page_json()

        query, _ = self.query(
            [{"data": [observation("row", "read", "TOOL")], "meta": {}}],
            max_page_output_bytes=100,
        )
        with self.assertRaisesRegex(report.BudgetExceeded, "output-byte budget"):
            query.next_page_json()

    def test_concurrent_query_budget_refuses_without_a_request(self) -> None:
        query, client = self.query([{"data": [], "meta": {}}])
        with (
            common.report_query_lock(),
            self.assertRaisesRegex(report.BudgetExceeded, "concurrency budget"),
        ):
            query.next_page_json()
        self.assertEqual(client.requests, [])

    def test_slow_page_and_invalid_search_fail_closed(self) -> None:
        class Clock:
            now = 0.0

            def __call__(self) -> float:
                return self.now

        clock = Clock()
        client = PageClient([{"data": [], "meta": {}}])
        client.on_request = lambda: setattr(clock, "now", 121.0)
        query = agent_query.ObservationQuery(
            client,
            self.start,
            self.end,
            kind="tool",
            budget=report.ReportBudget(max_seconds=120, clock=clock),
        )
        with self.assertRaisesRegex(report.BudgetExceeded, "wall-time budget"):
            query.next_page_json()

        for kwargs, expected in (
            ({"kind": "unknown"}, "kind"),
            ({"kind": "tool", "search": "x" * 257}, "256 bytes"),
            ({"kind": "tool", "page_size": 0}, "Page size"),
        ):
            with (
                self.subTest(kwargs=kwargs),
                self.assertRaisesRegex(ValueError, expected),
            ):
                agent_query.ObservationQuery(
                    PageClient([]), self.start, self.end, **kwargs
                )

    def test_generated_100_10k_and_100k_query_pages_stay_bounded(self) -> None:
        for kind in ("tool", "model"):
            for count in (100, 10_000, 100_000):
                with self.subTest(kind=kind, rows=count):
                    client = GeneratedQueryClient(count, kind)
                    query = agent_query.ObservationQuery(
                        client, self.start, self.end, kind=kind
                    )
                    returned = 0
                    pages = 0
                    while True:
                        page_json = query.next_page_json()
                        page = json.loads(page_json)
                        returned += len(page["observations"])
                        pages += 1
                        self.assertEqual(
                            page["budget"]["observed"]["page_output_bytes"],
                            len(page_json.encode("utf-8")),
                        )
                        self.assertLessEqual(
                            page["budget"]["observed"]["page_output_bytes"],
                            agent_query.MAX_PAGE_OUTPUT_BYTES,
                        )
                        self.assertLessEqual(
                            page["budget"]["observed"]["response_bytes"],
                            agent_query.MAX_QUERY_RESPONSE_BYTES,
                        )
                        if page["page"]["query_complete"]:
                            break
                    self.assertEqual(returned, count)
                    self.assertEqual(client.requests, pages)
                    self.assertLessEqual(pages, agent_query.MAX_QUERY_PAGES)
                    self.assertLessEqual(returned, agent_query.MAX_QUERY_ROWS)
                    self.assertLessEqual(
                        query.budget.response_bytes,
                        agent_query.MAX_QUERY_RESPONSE_BYTES,
                    )


if __name__ == "__main__":
    unittest.main()
