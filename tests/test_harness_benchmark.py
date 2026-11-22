"""Offline, versioned synthetic benchmark fixtures and pair eligibility."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from integrations import agent_evaluate, benchmark_protocol

FIXTURE_DIR = ROOT / "tests/fixtures/harness-benchmark/v1"
ARTIFACT_NAMES = (
    "cases.json",
    "rubric.json",
    "held-out-labels.json",
    "protocol.json",
)


def read_artifact(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def artifact_digest(name: str) -> str:
    return hashlib.sha256((FIXTURE_DIR / name).read_bytes()).hexdigest()


def fixture_pack_digest() -> str:
    digest = hashlib.sha256()
    for name in ("cases.json", "held-out-labels.json"):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((FIXTURE_DIR / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def make_run(
    *,
    arm: str,
    pair_id: str = "pair-1",
    replicate_id: str = "replicate-1",
    fixture_id: str = "synthetic-completed",
    comparison_axis: str = "harness",
    order_assignment: str = "baseline_first",
    cache_condition: str = "cold",
    run_state: str = "completed",
    attempt_outcomes: list[str] | None = None,
) -> dict:
    if attempt_outcomes is None:
        attempt_outcomes = [run_state if run_state != "unresolved" else "unresolved"]
    if attempt_outcomes[-1] == "failed" and run_state == "unresolved":
        terminal_reason = "invalid_output"
    else:
        terminal_reason = {
            "completed": None,
            "unresolved": "unknown_output",
            "censored": "deadline",
            "aborted": "operator_cancel",
        }[run_state]

    attempts = []
    previous_id = None
    for number, outcome in enumerate(attempt_outcomes, start=1):
        attempt_id = f"{arm}-{pair_id}-attempt-{number}"
        attempts.append(
            {
                "attempt_id": attempt_id,
                "attempt_number": number,
                "outcome": outcome,
                "retry_of": previous_id,
            }
        )
        previous_id = attempt_id

    if comparison_axis == "harness":
        harness_version = (
            f"synthetic-harness-{arm}-v1"
            if arm == "baseline"
            else "synthetic-harness-candidate-v2"
        )
        harness_commit = "a" * 40 if arm == "baseline" else "b" * 40
        model_id = "synthetic/provider-model"
        model_version = "synthetic-model-build-v1"
    else:
        harness_version = "synthetic-harness-shared-v1"
        harness_commit = "a" * 40
        model_id = (
            "synthetic/provider-model-v1"
            if arm == "baseline"
            else "synthetic/provider-model-v2"
        )
        model_version = (
            "synthetic-model-build-v1"
            if arm == "baseline"
            else "synthetic-model-build-v2"
        )

    order_position = {
        "baseline_first": {"baseline": 1, "candidate": 2},
        "candidate_first": {"candidate": 1, "baseline": 2},
    }[order_assignment][arm]
    return {
        "schema_version": "aiobs.benchmark-run.v1",
        "cohort_id": "synthetic-cohort-v1",
        "fixture_pack_sha256": fixture_pack_digest(),
        "rubric_sha256": artifact_digest("rubric.json"),
        "protocol_sha256": artifact_digest("protocol.json"),
        "fixture_id": fixture_id,
        "pair_id": pair_id,
        "replicate_id": replicate_id,
        "comparison_axis": comparison_axis,
        "arm": arm,
        "order_assignment": order_assignment,
        "order_position": order_position,
        "cache_condition": cache_condition,
        "random_seed": "seed-17",
        "memory_snapshot": {
            "state": "not_applicable",
            "reason": "fixture_has_no_memory_input",
        },
        "reset_protocol_id": "synthetic-reset-v1",
        "washout_seconds": 30,
        "workspace_template_sha256": hashlib.sha256(
            b"synthetic-workspace-template-v1"
        ).hexdigest(),
        "workspace_id": f"workspace-{arm}-{pair_id}",
        "harness_version": harness_version,
        "harness_commit": harness_commit,
        "model_id": model_id,
        "model_version": model_version,
        "run_state": run_state,
        "terminal_reason": terminal_reason,
        "attempts": attempts,
    }


def make_pair(**kwargs) -> tuple[dict, dict]:
    return make_run(arm="baseline", **kwargs), make_run(arm="candidate", **kwargs)


class FixturePackTests(unittest.TestCase):
    def test_versioned_artifacts_match_the_sha256_lock(self) -> None:
        lines = (FIXTURE_DIR / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        locked = {}
        for line in lines:
            digest, separator, name = line.partition("  ")
            self.assertTrue(separator)
            self.assertRegex(digest, r"^[a-f0-9]{64}$")
            self.assertNotIn(name, locked)
            locked[name] = digest
        self.assertEqual(set(locked), set(ARTIFACT_NAMES))
        self.assertEqual(
            locked, {name: artifact_digest(name) for name in ARTIFACT_NAMES}
        )
        self.assertNotEqual(fixture_pack_digest(), artifact_digest("cases.json"))

    def test_cases_rubric_and_held_out_targets_are_version_aligned(self) -> None:
        pack = read_artifact("cases.json")
        rubric = read_artifact("rubric.json")
        targets = read_artifact("held-out-labels.json")

        self.assertEqual(pack["schema_version"], "aiobs.synthetic-fixture-pack.v1")
        self.assertEqual(pack["fixture_pack_version"], "1.0.0")
        self.assertEqual(rubric["rubric_id"], agent_evaluate.RUBRIC)
        self.assertEqual(pack["rubric_id"], rubric["rubric_id"])
        self.assertEqual(pack["rubric_version"], rubric["rubric_version"])
        self.assertEqual(targets["fixture_pack_version"], pack["fixture_pack_version"])
        self.assertEqual(targets["rubric_version"], rubric["rubric_version"])
        self.assertEqual(rubric["instructions"], agent_evaluate.SYSTEM)
        self.assertEqual(
            rubric["labels"],
            {key: list(values) for key, values in agent_evaluate.ENUMS.items()},
        )

        cases = pack["cases"]
        budgets = read_artifact("protocol.json")["budgets"]
        self.assertEqual(len(cases), budgets["max_fixture_cases_per_cohort_arm"])
        for case in cases:
            fields = case["input"]
            self.assertTrue(
                all(
                    len(value) <= budgets["max_characters_per_input_field"]
                    for value in fields.values()
                )
            )
            self.assertLessEqual(
                sum(len(value) for value in fields.values()),
                budgets["max_input_characters_per_case"],
            )
        fixture_ids = [case["fixture_id"] for case in cases]
        self.assertEqual(len(fixture_ids), len(set(fixture_ids)))
        self.assertEqual(
            [case["input"] for case in cases], list(agent_evaluate.SYNTHETIC)
        )
        self.assertEqual(set(targets["cases"]), set(fixture_ids))
        self.assertEqual(list(targets["cases"].values()), list(agent_evaluate.EXPECTED))
        for case in cases:
            self.assertEqual(set(case), {"fixture_id", "input"})
            self.assertEqual(case["fixture_id"], case["input"]["case_id"])

        # The model-facing artifact contains inputs only; target labels are separate.
        self.assertNotIn("expected_labels", pack)
        self.assertNotIn("labels", pack)
        self.assertNotIn("input", targets)
        self.assertNotIn("task", targets)
        self.assertEqual(targets["evidence_class"], "attested")

    def test_protocol_declares_budgets_and_controlled_design_without_a_runner(
        self,
    ) -> None:
        protocol = read_artifact("protocol.json")
        self.assertFalse(protocol["execution_authority"]["runner_included"])
        self.assertFalse(protocol["execution_authority"]["model_calls_enabled"])
        self.assertFalse(protocol["execution_authority"]["automatic_execution"])
        self.assertFalse(
            protocol["execution_authority"]["automatic_real_content_selection"]
        )
        self.assertFalse(protocol["execution_authority"]["automatic_retries"])
        budgets = protocol["budgets"]
        self.assertEqual(budgets["max_fixture_cases_per_cohort_arm"], 3)
        self.assertEqual(budgets["max_replicates_per_fixture_arm"], 4)
        self.assertEqual(budgets["max_characters_per_input_field"], 4000)
        self.assertEqual(budgets["max_input_characters_per_case"], 12000)
        self.assertEqual(budgets["max_response_bytes_per_attempt"], 65536)
        self.assertEqual(budgets["max_output_tokens_per_attempt"], 256)
        self.assertEqual(budgets["request_timeout_seconds"], 120)
        self.assertIn("held_out_labels", protocol["controls"])
        self.assertIn("reset_and_washout", protocol["controls"])
        self.assertIn("counterbalance", protocol["controls"]["order"])
        self.assertIn("stratify", protocol["controls"]["cache"])
        self.assertIn("harness_commit", protocol["run_record_required_fields"])
        self.assertIn("model_version", protocol["run_record_required_fields"])
        self.assertIn(
            "missing_run_record",
            protocol["pair_eligibility"]["missing_arm_disposition"],
        )
        self.assertIn(
            "version_unavailable",
            protocol["pair_eligibility"]["unresolved_version_disposition"],
        )
        self.assertEqual(
            set(protocol["cohort_and_sampling"]["condition_cells"]),
            {
                "cold/baseline_first",
                "cold/candidate_first",
                "warm/baseline_first",
                "warm/candidate_first",
            },
        )
        sample = make_run(arm="baseline")
        self.assertEqual(set(protocol["run_record_required_fields"]), set(sample))
        self.assertIn("exactly one fixture", protocol["run_record_unit"])
        self.assertEqual(
            protocol["comparison"][
                "minimum_completed_replicates_for_within_fixture_variability"
            ],
            2,
        )
        self.assertIn("probability sample", protocol["cohort_and_sampling"]["sampling"])
        self.assertIn("does not mean", protocol["run_state_semantics"])


class PairEligibilityTests(unittest.TestCase):
    max_attempts = read_artifact("protocol.json")["budgets"][
        "max_attempts_per_arm_replicate"
    ]
    max_replicates = read_artifact("protocol.json")["budgets"][
        "max_replicates_per_fixture_arm"
    ]
    artifact_digests: ClassVar[dict[str, str]] = {
        "fixture_pack_sha256": fixture_pack_digest(),
        "rubric_sha256": artifact_digest("rubric.json"),
        "protocol_sha256": artifact_digest("protocol.json"),
    }
    fixture_ids: ClassVar[set[str]] = {
        case["fixture_id"] for case in read_artifact("cases.json")["cases"]
    }

    def assess(self, baseline: dict, candidate: dict) -> dict:
        return benchmark_protocol.assess_pair(
            baseline,
            candidate,
            artifact_digests=self.artifact_digests,
            fixture_ids=self.fixture_ids,
            max_attempts=self.max_attempts,
        )

    def test_empty_summary_keeps_version_comparison_unavailable(self) -> None:
        summary = benchmark_protocol.summarize_pair_eligibility(
            [], max_replicates_per_fixture=self.max_replicates
        )
        self.assertEqual(summary["supplied_pair_count"], 0)
        self.assertIsNone(summary["compared_versions_are_uniform"])
        self.assertEqual(summary["effect_size"]["state"], "unavailable")
        self.assertIn("completeness is not asserted", summary["scope"])

    def test_exact_completed_harness_pair_is_eligible(self) -> None:
        baseline, candidate = make_pair()
        result = self.assess(baseline, candidate)
        self.assertEqual(result["status"], "eligible")
        self.assertEqual(
            result["arm_states"], {"baseline": "completed", "candidate": "completed"}
        )
        self.assertEqual(result["cluster_unit"], "fixture_id")
        self.assertEqual(
            result["versions"]["baseline"]["harness_version"],
            baseline["harness_version"],
        )
        self.assertEqual(
            result["arm_terminal_reasons"], {"baseline": None, "candidate": None}
        )
        self.assertEqual(result["order_assignment"], "baseline_first")
        self.assertFalse(result["attempts_are_independent_samples"])

    def test_exact_model_comparison_keeps_harness_fixed(self) -> None:
        baseline, candidate = make_pair(comparison_axis="model")
        result = self.assess(baseline, candidate)
        self.assertEqual(result["status"], "eligible")

    def test_artifact_control_and_workspace_mismatches_are_ineligible(self) -> None:
        cases = (
            ("rubric_sha256", "f" * 64, "mismatch:rubric_sha256"),
            ("cache_condition", "warm", "mismatch:cache_condition"),
            (
                "memory_snapshot",
                {"state": "frozen", "sha256": "f" * 64},
                "mismatch:memory_snapshot",
            ),
            ("reset_protocol_id", "synthetic-reset-v2", "mismatch:reset_protocol_id"),
            ("washout_seconds", 60, "mismatch:washout_seconds"),
            ("random_seed", "seed-18", "mismatch:random_seed"),
            (
                "workspace_template_sha256",
                "f" * 64,
                "mismatch:workspace_template_sha256",
            ),
            ("workspace_id", "workspace-baseline-pair-1", "workspace_reused"),
            ("fixture_id", "unregistered-fixture", "fixture_not_in_pack"),
        )
        for field, value, reason in cases:
            with self.subTest(field=field):
                baseline, candidate = make_pair()
                candidate[field] = value
                result = self.assess(baseline, candidate)
                self.assertEqual(result["status"], "ineligible")
                self.assertIn(reason, result["reason_codes"])

        baseline, candidate = make_pair()
        candidate["rubric_sha256"] = "f" * 64
        ineligible = self.assess(baseline, candidate)
        summary = benchmark_protocol.summarize_pair_eligibility(
            [ineligible], max_replicates_per_fixture=self.max_replicates
        )
        self.assertEqual(summary["status_counts"]["ineligible"], 1)
        self.assertEqual(
            summary["eligibility_reason_code_counts"][
                "artifact_not_verified:rubric_sha256"
            ],
            1,
        )

    def test_comparison_axis_requires_only_its_declared_version_to_change(self) -> None:
        baseline, candidate = make_pair()
        candidate["model_version"] = "synthetic-model-build-v2"
        result = self.assess(baseline, candidate)
        self.assertEqual(result["status"], "ineligible")
        self.assertIn("non_comparison_model_changed", result["reason_codes"])

        baseline, candidate = make_pair()
        candidate["harness_version"] = baseline["harness_version"]
        candidate["harness_commit"] = baseline["harness_commit"]
        result = self.assess(baseline, candidate)
        self.assertIn("comparison_harness_unchanged", result["reason_codes"])

        baseline, candidate = make_pair(comparison_axis="model")
        candidate["harness_version"] = "synthetic-harness-changed-v2"
        result = self.assess(baseline, candidate)
        self.assertIn("non_comparison_harness_changed", result["reason_codes"])

    def test_unresolved_censored_and_aborted_states_are_not_collapsed(self) -> None:
        for state in ("unresolved", "censored", "aborted"):
            with self.subTest(state=state):
                baseline = make_run(arm="baseline", run_state=state)
                candidate = make_run(arm="candidate")
                result = self.assess(baseline, candidate)
                self.assertEqual(result["status"], "incomplete")
                self.assertEqual(result["arm_states"]["baseline"], state)
                self.assertEqual(result["arm_states"]["candidate"], "completed")
                self.assertIsNotNone(result["arm_terminal_reasons"]["baseline"])
                self.assertIsNone(result["arm_terminal_reasons"]["candidate"])
                self.assertEqual(result["reason_codes"], [])

    def test_retries_are_nested_and_repeated_fixtures_are_clustered(self) -> None:
        baseline1 = make_run(arm="baseline", pair_id="pair-1")
        candidate1 = make_run(
            arm="candidate",
            pair_id="pair-1",
            attempt_outcomes=["failed", "completed"],
        )
        baseline2, candidate2 = make_pair(
            pair_id="pair-2",
            replicate_id="replicate-2",
            order_assignment="candidate_first",
        )
        baseline3, candidate3 = make_pair(
            pair_id="pair-3",
            replicate_id="replicate-3",
            order_assignment="baseline_first",
            cache_condition="warm",
        )
        baseline4, candidate4 = make_pair(
            pair_id="pair-4",
            replicate_id="replicate-4",
            order_assignment="candidate_first",
            cache_condition="warm",
        )
        assessments = [
            self.assess(baseline1, candidate1),
            self.assess(baseline2, candidate2),
            self.assess(baseline3, candidate3),
            self.assess(baseline4, candidate4),
        ]
        summary = benchmark_protocol.summarize_pair_eligibility(
            assessments, max_replicates_per_fixture=self.max_replicates
        )
        self.assertEqual(summary["status_counts"]["eligible"], 4)
        self.assertEqual(summary["eligible_fixture_cluster_count"], 1)
        self.assertEqual(summary["comparison_axis"], "harness")
        self.assertEqual(
            summary["compared_versions_by_pair"][0]["baseline"]["model_id"],
            "synthetic/provider-model",
        )
        self.assertTrue(summary["compared_versions_are_uniform"])
        self.assertEqual(summary["order_assignment_counts"]["baseline_first"], 2)
        self.assertEqual(summary["order_assignment_counts"]["candidate_first"], 2)
        self.assertEqual(summary["cache_condition_counts"]["cold"], 2)
        self.assertEqual(summary["cache_condition_counts"]["warm"], 2)
        self.assertEqual(
            summary["status_counts_by_cache_condition"]["cold"]["eligible"], 2
        )
        self.assertEqual(
            summary["condition_cell_counts"],
            {
                "cold/baseline_first": 1,
                "cold/candidate_first": 1,
                "warm/baseline_first": 1,
                "warm/candidate_first": 1,
            },
        )
        self.assertTrue(
            all(
                counts["eligible"] == 1
                for counts in summary["condition_cell_status_counts"].values()
            )
        )
        self.assertEqual(summary["attempt_count_descriptive_only"], 9)
        self.assertFalse(summary["attempts_are_independent_samples"])
        self.assertEqual(summary["effect_size"]["state"], "unavailable")
        self.assertEqual(summary["uncertainty"]["state"], "unavailable")
        self.assertIn("completeness is not asserted", summary["scope"])

    def test_incomplete_summary_retains_state_counts_and_never_scores_missing_as_zero(
        self,
    ) -> None:
        baseline = make_run(arm="baseline", run_state="unresolved")
        candidate = make_run(arm="candidate", run_state="censored")
        assessment = self.assess(baseline, candidate)
        summary = benchmark_protocol.summarize_pair_eligibility(
            [assessment], max_replicates_per_fixture=self.max_replicates
        )
        self.assertEqual(summary["status_counts"]["incomplete"], 1)
        self.assertEqual(summary["arm_state_counts"]["baseline"]["unresolved"], 1)
        self.assertEqual(summary["arm_state_counts"]["candidate"]["censored"], 1)
        self.assertEqual(
            summary["arm_terminal_reason_counts"]["baseline"]["unknown_output"], 1
        )
        self.assertEqual(
            summary["arm_terminal_reason_counts"]["candidate"]["deadline"], 1
        )
        self.assertEqual(summary["effect_size"]["state"], "unavailable")

    def test_run_schema_rejects_missing_versions_bad_retries_and_unpaired_order(
        self,
    ) -> None:
        baseline, candidate = make_pair()
        del baseline["model_version"]
        with self.assertRaisesRegex(ValueError, "exact benchmark schema"):
            self.assess(baseline, candidate)

        baseline, candidate = make_pair()
        baseline["model_version"] = "unknown"
        with self.assertRaisesRegex(ValueError, "actual non-empty version identity"):
            self.assess(baseline, candidate)

        baseline, candidate = make_pair()
        candidate["attempts"] = [
            {
                "attempt_id": "attempt-1",
                "attempt_number": 1,
                "outcome": "failed",
                "retry_of": None,
            },
            {
                "attempt_id": "attempt-2",
                "attempt_number": 2,
                "outcome": "completed",
                "retry_of": "wrong-id",
            },
        ]
        with self.assertRaisesRegex(ValueError, "preceding failed attempt"):
            self.assess(baseline, candidate)

        candidate = make_run(
            arm="candidate",
            attempt_outcomes=["failed", "failed", "failed", "completed"],
        )
        with self.assertRaisesRegex(ValueError, "within the declared budget"):
            self.assess(make_run(arm="baseline"), candidate)

        baseline, candidate = make_pair()
        candidate["order_position"] = 1
        with self.assertRaisesRegex(ValueError, "order_position"):
            self.assess(baseline, candidate)

        baseline, candidate = make_pair()
        candidate["workspace_id"] = "/private/path"
        with self.assertRaisesRegex(ValueError, "opaque identifier"):
            self.assess(baseline, candidate)

    def test_duplicate_pair_ids_cannot_inflate_a_summary(self) -> None:
        baseline, candidate = make_pair()
        result = self.assess(baseline, candidate)
        with self.assertRaisesRegex(ValueError, "unique"):
            benchmark_protocol.summarize_pair_eligibility(
                [result, copy.deepcopy(result)],
                max_replicates_per_fixture=self.max_replicates,
            )

    def test_fixture_replicate_budget_is_enforced(self) -> None:
        assessments = [
            self.assess(
                *make_pair(
                    pair_id="pair-1",
                    replicate_id="replicate-1",
                    order_assignment="baseline_first",
                )
            ),
            self.assess(
                *make_pair(
                    pair_id="pair-2",
                    replicate_id="replicate-2",
                    order_assignment="candidate_first",
                )
            ),
        ]
        with self.assertRaisesRegex(ValueError, "exceed the declared budget"):
            benchmark_protocol.summarize_pair_eligibility(
                assessments, max_replicates_per_fixture=1
            )

    def test_replicate_ids_must_be_unique_within_each_fixture(self) -> None:
        first = self.assess(*make_pair(pair_id="pair-1", replicate_id="replicate-1"))
        second = self.assess(*make_pair(pair_id="pair-2", replicate_id="replicate-1"))
        with self.assertRaisesRegex(ValueError, "unique within each fixture"):
            benchmark_protocol.summarize_pair_eligibility(
                [first, second], max_replicates_per_fixture=self.max_replicates
            )

    def test_summary_rejects_duplicate_fixture_condition_cells(self) -> None:
        first = self.assess(*make_pair(pair_id="pair-1", replicate_id="replicate-1"))
        second = self.assess(*make_pair(pair_id="pair-2", replicate_id="replicate-2"))
        with self.assertRaisesRegex(ValueError, "only one pair per preregistered"):
            benchmark_protocol.summarize_pair_eligibility(
                [first, second], max_replicates_per_fixture=self.max_replicates
            )

    def test_summary_preserves_mixed_version_pairs_without_estimating_effect(
        self,
    ) -> None:
        first = self.assess(*make_pair(pair_id="pair-1"))
        baseline, candidate = make_pair(
            pair_id="pair-2",
            replicate_id="replicate-2",
            order_assignment="candidate_first",
        )
        candidate["harness_version"] = "synthetic-harness-candidate-v3"
        candidate["harness_commit"] = "c" * 40
        second = self.assess(baseline, candidate)
        summary = benchmark_protocol.summarize_pair_eligibility(
            [first, second], max_replicates_per_fixture=self.max_replicates
        )
        self.assertEqual(summary["status_counts"]["eligible"], 2)
        self.assertFalse(summary["compared_versions_are_uniform"])
        self.assertEqual(len(summary["compared_versions_by_pair"]), 2)
        self.assertEqual(summary["effect_size"]["state"], "unavailable")

    def test_offline_helpers_do_not_call_the_existing_model_evaluator(self) -> None:
        baseline, candidate = make_pair()
        with (
            mock.patch.object(
                agent_evaluate, "run", side_effect=AssertionError("model path called")
            ) as run,
            mock.patch.object(
                agent_evaluate,
                "evaluate",
                side_effect=AssertionError("model path called"),
            ) as evaluate,
        ):
            self.assertEqual(self.assess(baseline, candidate)["status"], "eligible")
            run.assert_not_called()
            evaluate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
