"""Explicit task attestations stay private, bounded and distinct from observations."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from integrations import agent_report, task_evidence


def task(identifier: str, **updates: object) -> dict:
    return {
        "id": identifier,
        "kind": "task",
        "parent_id": None,
        "status": "unknown",
        "validation": "unknown",
        "attested_by": "unknown",
        "bindings": [],
        **updates,
    }


def snapshot(tasks: list[dict] | None = None) -> dict:
    return {
        "schema_version": "1.0",
        "as_of": "2026-11-20T00:00:00Z",
        "tasks": tasks or [],
    }


class TaskEvidenceTests(unittest.TestCase):
    def test_private_file_and_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "evidence.json"
            path.write_text(json.dumps(snapshot([task("task_1")])))
            path.chmod(0o600)
            self.assertEqual(task_evidence.read_file(path)["tasks"][0]["id"], "task_1")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private regular file"):
                task_evidence.read_file(path)
            path.chmod(0o600)
            link = root / "link"
            os.symlink(path, link)
            with self.assertRaisesRegex(ValueError, "safely read"):
                task_evidence.read_file(link)
            path.write_text(
                '{"schema_version":"1.0","as_of":"2026-11-20T00:00:00Z","tasks":[],"tasks":[]}'
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON keys"):
                task_evidence.read_file(path)
            path.write_text(
                '{"schema_version":"1.0","as_of":"2026-11-20T00:00:00Z","tasks":[{"id":"a","id":"b"}]}'
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON keys"):
                task_evidence.read_file(path)
            self.assertTrue(path.exists())

    def test_oversized_and_nonregular_files_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "large"
            path.write_bytes(b"x" * (task_evidence.MAX_BYTES + 1))
            path.chmod(0o600)
            with self.assertRaisesRegex(ValueError, "maximum 1 MiB"):
                task_evidence.read_file(path)
            fifo = root / "fifo"
            os.mkfifo(fifo, 0o600)
            with self.assertRaisesRegex(ValueError, "private regular file"):
                task_evidence.read_file(fifo)

    def test_invalid_schema_categories_ids_and_cycles_fail_closed(self) -> None:
        bad = [
            {"schema_version": "2", "as_of": "2026-11-20T00:00:00Z", "tasks": []},
            {**snapshot(), "extra": "SYNTHETIC_SECRET"},
            {**snapshot(), "as_of": "2026-11-20T00:00:00"},
            {**snapshot(), "as_of": "2026-11-20X00:00:00Z"},
            {**snapshot(), "as_of": "2026-11-20T00:00:00.123456789Z"},
            snapshot([task("a"), task("a")]),
            snapshot([task("a", parent_id="b"), task("b", parent_id="a")]),
            snapshot([task("a", status="success")]),
            snapshot([task("a", validation=True)]),
            snapshot([task("a", kind="task", notes="SYNTHETIC_SECRET")]),
            snapshot(
                [
                    task(
                        "a",
                        bindings=[
                            {"trace_id": "trace", "turn_id": "turn"},
                            {"trace_id": "trace", "turn_id": "turn"},
                        ],
                    )
                ]
            ),
            snapshot(
                [task("a", bindings=[{"trace_id": "../secret", "turn_id": "turn"}])]
            ),
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                task_evidence.validate(value)

    def test_attestations_are_separate_and_ambiguous_links_are_not_attributed(
        self,
    ) -> None:
        linked = {"trace_id": "trace1", "turn_id": "turn1"}
        ambiguous = {"trace_id": "trace2", "turn_id": "turn2"}
        absent = {"trace_id": "trace3", "turn_id": "turn3"}
        value = task_evidence.validate(
            snapshot(
                [
                    task(
                        "epic_1",
                        kind="epic",
                        status="completed",
                        validation="passed",
                        attested_by="human",
                    ),
                    task(
                        "task_1",
                        parent_id="epic_1",
                        status="completed",
                        validation="unknown",
                        attested_by="agent",
                        bindings=[linked, ambiguous],
                    ),
                    task(
                        "task_2",
                        parent_id="outside_snapshot",
                        status="blocked",
                        attested_by="automation",
                        bindings=[ambiguous, absent],
                    ),
                ]
            )
        )
        result = task_evidence.summarize(
            value, {("trace1", "turn1"), ("trace2", "turn2")}
        )
        self.assertEqual(result["evidence_class"], "operator_supplied_attestation")
        self.assertEqual(result["completed_records_with_pass_attested"], 1)
        self.assertEqual(result["completed_records_without_pass_attested"], 1)
        self.assertEqual(result["by_kind_and_status"]["epic"]["completed"], 1)
        self.assertEqual(result["by_kind_and_status"]["task"]["completed"], 1)
        self.assertEqual(result["parent_links_outside_snapshot"], 1)
        self.assertEqual(result["tasks_without_bindings"], 1)
        self.assertEqual(result["unique_bindings_matched"], 1)
        self.assertEqual(result["unique_bindings_not_observed"], 1)
        self.assertEqual(result["ambiguous_bindings"], 1)
        self.assertNotIn("task_1", json.dumps(result))
        detailed = task_evidence.summarize(
            value, {("trace1", "turn1"), ("trace2", "turn2")}, True
        )
        matches = detailed["tasks"][1]["bindings"]
        self.assertEqual(
            [item["match"] for item in matches], ["observed_turn", "ambiguous"]
        )
        self.assertEqual(value["tasks"][1]["bindings"], [linked, ambiguous])

    def test_snapshot_freshness_is_informational_and_handles_future_timestamps(
        self,
    ) -> None:
        computed_at = dt.datetime(2026, 11, 21, 12, tzinfo=dt.timezone.utc)
        current = task_evidence.validate(
            {**snapshot(), "as_of": "2026-11-21T11:00:00Z"}
        )
        summary = task_evidence.summarize(current, set(), computed_at=computed_at)
        self.assertEqual(summary["freshness"]["age_seconds"], 60 * 60)
        self.assertFalse(summary["freshness"]["future_dated"])
        self.assertIn("no stale threshold", summary["freshness"]["interpretation"])

        future = task_evidence.validate({**snapshot(), "as_of": "2026-11-21T12:15:00Z"})
        summary = task_evidence.summarize(future, set(), computed_at=computed_at)
        self.assertEqual(summary["freshness"]["age_seconds"], -15 * 60)
        self.assertTrue(summary["freshness"]["future_dated"])
        with self.assertRaisesRegex(ValueError, "must include a timezone"):
            task_evidence.summarize(
                future,
                set(),
                computed_at=dt.datetime(2026, 11, 21, 12),  # noqa: DTZ001
            )

    def test_report_freshness_uses_the_report_computation_timestamp(self) -> None:
        start = dt.datetime(2026, 11, 19, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 11, 21, tzinfo=dt.timezone.utc)
        value = task_evidence.validate(snapshot())
        report = agent_report.build_report([], start, end, task_snapshot=value)
        computed_at = dt.datetime.fromisoformat(report["computed_at"])
        as_of = dt.datetime.fromisoformat(report["task_attestations"]["as_of"])
        self.assertEqual(
            report["task_attestations"]["freshness"]["age_seconds"],
            (computed_at - as_of).total_seconds(),
        )

    def test_report_has_no_attestations_without_explicit_input(self) -> None:
        start = dt.datetime(2026, 11, 19, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 11, 21, tzinfo=dt.timezone.utc)
        self.assertNotIn("task_attestations", agent_report.build_report([], start, end))
        value = task_evidence.validate(snapshot([task("task_1", status="completed")]))
        result = agent_report.build_report([], start, end, task_snapshot=value)
        self.assertEqual(result["task_attestations"]["by_status"]["completed"], 1)
        self.assertIn(
            "do not establish task success", result["unknown"]["task_completion"]
        )

    def test_report_matches_only_observed_opencode_turns(self) -> None:
        start = dt.datetime(2026, 11, 19, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 11, 21, tzinfo=dt.timezone.utc)
        value = task_evidence.validate(
            snapshot(
                [
                    task(
                        "task_1",
                        bindings=[
                            {"trace_id": "trace1", "turn_id": "real_turn"},
                            {"trace_id": "trace2", "turn_id": "wrong_type"},
                        ],
                    )
                ]
            )
        )
        rows = [
            agent_report.select(
                {
                    "id": "real_turn",
                    "traceId": "trace1",
                    "name": "opencode.turn",
                    "type": "AGENT",
                }
            ),
            agent_report.select(
                {
                    "id": "wrong_type",
                    "traceId": "trace2",
                    "name": "opencode.turn",
                    "type": "EVENT",
                }
            ),
        ]
        summary = agent_report.build_report(rows, start, end, task_snapshot=value)[
            "task_attestations"
        ]
        self.assertEqual(summary["unique_bindings_matched"], 1)
        self.assertEqual(summary["unique_bindings_not_observed"], 1)

    def test_conditional_tokens_exclude_ambiguous_missing_and_cyclic_turns(
        self,
    ) -> None:
        start = dt.datetime(2026, 11, 19, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 11, 21, tzinfo=dt.timezone.utc)

        def binding(trace: str, turn: str) -> dict:
            return {"trace_id": trace, "turn_id": turn}

        value = task_evidence.validate(
            snapshot(
                [
                    task(
                        "parent_task",
                        bindings=[
                            binding("trace", "root"),
                            binding("trace", "amb"),
                            binding("cycle", "cy"),
                            binding("absent", "missing"),
                        ],
                    ),
                    task(
                        "child_task",
                        parent_id="parent_task",
                        bindings=[binding("trace", "child"), binding("trace", "amb")],
                    ),
                    task("unbound_task"),
                ]
            )
        )

        def row(
            identifier: str,
            name: str,
            typ: str,
            parent: str | None = None,
            trace: str = "trace",
            **extra: object,
        ) -> dict:
            return agent_report.select(
                {
                    "id": identifier,
                    "traceId": trace,
                    "name": name,
                    "type": typ,
                    "parentObservationId": parent,
                    **extra,
                }
            )

        rows = [
            row("root", "opencode.turn", "AGENT"),
            row(
                "gen_root",
                "opencode.generation",
                "GENERATION",
                "root",
                usageDetails={"input": 10, "total": 12},
            ),
            row("child", "opencode.turn", "AGENT", "root"),
            row(
                "gen_child",
                "opencode.generation",
                "GENERATION",
                "child",
                usageDetails={"input": 20, "total": 25},
            ),
            row("amb", "opencode.turn", "AGENT"),
            row(
                "gen_amb",
                "opencode.generation",
                "GENERATION",
                "amb",
                usageDetails={"input": 1000},
            ),
            row("cy", "opencode.turn", "AGENT", "cy", trace="cycle"),
        ]
        result = agent_report.build_report(
            rows, start, end, task_snapshot=value, include_identifiers=True
        )
        section = result["task_attestations"]
        usage = section["conditional_generation_usage"]
        self.assertEqual(usage["eligible_turns"], 2)
        self.assertEqual(usage["recorded_generation_observations"], 2)
        self.assertEqual(
            usage["reported_tokens"]["input"],
            {"sum": 30, "reported": 2, "missing": 0},
        )
        self.assertEqual(usage["reported_tokens"]["total"]["sum"], 37)
        self.assertEqual(usage["ambiguous_bindings_excluded"], 1)
        self.assertEqual(usage["unmatched_unique_bindings"], 1)
        self.assertEqual(usage["cyclic_unique_bindings"], 1)
        details = {
            item["id"]: item["conditional_generation_usage"]
            for item in section["tasks"]
        }
        self.assertEqual(details["parent_task"]["reported_tokens"]["input"]["sum"], 10)
        self.assertEqual(details["child_task"]["reported_tokens"]["input"]["sum"], 20)
        self.assertFalse(details["parent_task"]["all_declared_bindings_eligible"])
        self.assertIsNone(details["unbound_task"]["reported_tokens"]["input"]["sum"])
        anonymous = agent_report.build_report(rows, start, end, task_snapshot=value)
        self.assertNotIn("parent_task", json.dumps(anonymous))
        self.assertNotIn("tasks", anonymous["task_attestations"])

    def test_eligible_turn_with_no_generation_is_not_zero_usage(self) -> None:
        value = task_evidence.validate(
            snapshot(
                [task("task_1", bindings=[{"trace_id": "trace", "turn_id": "turn"}])]
            )
        )
        key = ("trace", "turn")
        overall, per_task = agent_report.task_generation_usage(
            value, {key}, {}, {key: False}
        )
        self.assertEqual(overall["turns_without_observed_generations"], 1)
        self.assertIsNone(per_task["task_1"]["reported_tokens"]["total"]["sum"])

    def test_cli_rejects_invalid_selected_file_before_contacting_langfuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps({**snapshot(), "secret": "SYNTHETIC_SECRET"}))
            path.chmod(0o600)
            with (
                mock.patch.object(agent_report.span_dashboard, "context") as context,
                self.assertRaisesRegex(ValueError, "invalid schema") as error,
            ):
                agent_report.main(["--task-evidence", str(path)])
            context.assert_not_called()
            self.assertNotIn("SYNTHETIC_SECRET", str(error.exception))

    def test_cli_emits_only_selected_structural_attestations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            evidence = snapshot([task("task_1", status="blocked", attested_by="human")])
            path.write_text(json.dumps(evidence))
            path.chmod(0o600)
            with (
                mock.patch.object(
                    agent_report.span_dashboard,
                    "context",
                    return_value=(object(), {}, "project"),
                ),
                mock.patch.object(
                    agent_report, "fetch_observations", return_value=([], 0)
                ),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                agent_report.main(["--task-evidence", str(path)])
            data = json.loads(output.getvalue())
            self.assertEqual(data["task_attestations"]["by_status"]["blocked"], 1)
            self.assertNotIn("task_1", output.getvalue())
            self.assertEqual(json.loads(path.read_text()), evidence)


if __name__ == "__main__":
    unittest.main()
