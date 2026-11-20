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

from integrations import agent_dashboard, agent_dashboard_definition, span_dashboard
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

    def request(self, method: str, path: str) -> dict:
        assert method == "GET"
        self.requests.append(path)
        return copy.deepcopy(self.pages[len(self.requests) - 1])


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

    def test_empty_page_with_cursor_and_exact_duplicates(self) -> None:
        row = observation("turn", "opencode.turn", "AGENT")
        client = PageClient(
            [
                {"data": [row], "meta": {"cursor": "first"}},
                {"data": [], "meta": {"cursor": "second"}},
                {"data": [row], "meta": {}},
            ]
        )
        result, duplicates = report.fetch_observations(client, self.start, self.end, 10)
        self.assertEqual((len(result), duplicates, len(client.requests)), (1, 1, 3))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(client.requests[0]).query)
        self.assertEqual(query["fields"], ["basic,model,usage,metrics"])
        self.assertNotIn("io", query["fields"][0].split(","))

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


if __name__ == "__main__":
    unittest.main()
