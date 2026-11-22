"""Offline paired scoring preserves unresolved, missing, and synthetic-only limits."""

from __future__ import annotations

import copy
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from test_harness_benchmark import (
    artifact_digest,
    fixture_pack_digest,
    make_pair,
    make_run,
    read_artifact,
)

from integrations import agent_evaluate, benchmark_protocol, paired_comparison

REFERENCE_LABELS = read_artifact("held-out-labels.json")["cases"]
MAX_REPLICATES = read_artifact("protocol.json")["budgets"][
    "max_replicates_per_fixture_arm"
]
ARTIFACT_DIGESTS = {
    "fixture_pack_sha256": fixture_pack_digest(),
    "rubric_sha256": artifact_digest("rubric.json"),
    "protocol_sha256": artifact_digest("protocol.json"),
}
FIXTURE_IDS = set(REFERENCE_LABELS)


def classify_pair(
    *,
    pair_id: str,
    replicate_id: str,
    fixture_id: str = "synthetic-completed",
    order_assignment: str = "baseline_first",
    cache_condition: str = "cold",
) -> dict:
    baseline, candidate = make_pair(
        pair_id=pair_id,
        replicate_id=replicate_id,
        fixture_id=fixture_id,
        order_assignment=order_assignment,
        cache_condition=cache_condition,
    )
    return benchmark_protocol.assess_pair(
        baseline,
        candidate,
        artifact_digests=ARTIFACT_DIGESTS,
        fixture_ids=FIXTURE_IDS,
        max_attempts=read_artifact("protocol.json")["budgets"][
            "max_attempts_per_arm_replicate"
        ],
    )


def observation(
    pair_id: str,
    *,
    baseline_assessment: object = None,
    candidate_assessment: object = None,
    baseline_resources: dict | None = None,
    candidate_resources: dict | None = None,
) -> dict:
    return {
        "pair_id": pair_id,
        "arms": {
            "baseline": {
                "assessment": copy.deepcopy(baseline_assessment),
                "resources": copy.deepcopy(baseline_resources),
            },
            "candidate": {
                "assessment": copy.deepcopy(candidate_assessment),
                "resources": copy.deepcopy(candidate_resources),
            },
        },
    }


def exact_labels(fixture_id: str) -> dict:
    return copy.deepcopy(REFERENCE_LABELS[fixture_id])


def resources(
    *,
    elapsed_seconds: float = 1,
    tokens: int = 100,
    token_basis: str = "reported_total",
    cost: float = 0.25,
    currency: str = "USD",
    cost_basis: str = "synthetic_cost_v1",
) -> dict:
    return {
        "elapsed_seconds": elapsed_seconds,
        "tokens": {"value": tokens, "basis": token_basis},
        "cost": {"amount": cost, "currency": currency, "basis": cost_basis},
    }


def summarize(assessments: list[dict], observations: list[dict]) -> dict:
    return paired_comparison.summarize_synthetic_pairs(
        assessments,
        observations,
        REFERENCE_LABELS,
        agent_evaluate.ENUMS,
        max_replicates_per_fixture=MAX_REPLICATES,
    )


class PairedComparisonTests(unittest.TestCase):
    def test_paired_agreement_is_candidate_minus_baseline_not_acceptance(self) -> None:
        assessment = classify_pair(pair_id="pair-1", replicate_id="replicate-1")
        baseline = exact_labels("synthetic-completed")
        baseline["completion_evidence"] = "not_supported"
        candidate = exact_labels("synthetic-completed")
        values = resources(elapsed_seconds=2, tokens=120, cost=0.5)

        summary = summarize(
            [assessment],
            [
                observation(
                    "pair-1",
                    baseline_assessment=baseline,
                    candidate_assessment=candidate,
                    baseline_resources=values,
                    candidate_resources=resources(
                        elapsed_seconds=3, tokens=140, cost=0.75
                    ),
                )
            ],
        )

        result = summary["paired_judgment_agreement"]
        self.assertEqual(result["eligible_pair_count"], 1)
        self.assertEqual(result["scored_pair_count"], 1)
        self.assertEqual(result["unresolved_pair_count"], 0)
        self.assertEqual(result["pair_results"][0]["baseline_agreement"], 0.8)
        self.assertEqual(result["pair_results"][0]["candidate_agreement"], 1.0)
        self.assertAlmostEqual(
            result["pair_results"][0]["candidate_minus_baseline"], 0.2
        )
        self.assertEqual(summary["evidence_scope"], "synthetic_fixture_only")
        self.assertEqual(summary["reference_evidence_class"], "attested")
        self.assertFalse(summary["metric_delivery_advanced"])
        self.assertEqual(summary["metric_availability"]["X1"]["state"], "unavailable")
        self.assertEqual(summary["metric_availability"]["X2"]["state"], "unavailable")
        self.assertEqual(summary["metric_availability"]["X4"]["state"], "unavailable")
        self.assertEqual(summary["metric_availability"]["X5"]["state"], "unavailable")
        self.assertEqual(summary["metric_availability"]["X7"]["state"], "unavailable")
        self.assertFalse(summary["version_ranking"]["ranked"])
        self.assertNotIn("accepted_tasks", summary)

    def test_incomplete_and_ineligible_pairs_remain_unscored_and_visible(self) -> None:
        eligible = classify_pair(pair_id="pair-1", replicate_id="replicate-1")
        baseline = make_run(
            arm="baseline",
            pair_id="pair-2",
            replicate_id="replicate-2",
            fixture_id="synthetic-blocked",
            run_state="unresolved",
        )
        candidate = make_run(
            arm="candidate",
            pair_id="pair-2",
            replicate_id="replicate-2",
            fixture_id="synthetic-blocked",
        )
        incomplete = benchmark_protocol.assess_pair(
            baseline,
            candidate,
            artifact_digests=ARTIFACT_DIGESTS,
            fixture_ids=FIXTURE_IDS,
            max_attempts=3,
        )
        baseline, candidate = make_pair(
            pair_id="pair-3",
            replicate_id="replicate-3",
            fixture_id="synthetic-unclear",
            order_assignment="candidate_first",
        )
        candidate["protocol_sha256"] = "f" * 64
        ineligible = benchmark_protocol.assess_pair(
            baseline,
            candidate,
            artifact_digests=ARTIFACT_DIGESTS,
            fixture_ids=FIXTURE_IDS,
            max_attempts=3,
        )
        summary = summarize(
            [eligible, incomplete, ineligible],
            [
                observation(
                    "pair-1",
                    baseline_assessment=exact_labels("synthetic-completed"),
                    candidate_assessment=exact_labels("synthetic-completed"),
                    baseline_resources=resources(),
                    candidate_resources=resources(),
                )
            ],
        )

        self.assertEqual(
            summary["pair_eligibility"]["status_counts"],
            {"eligible": 1, "ineligible": 1, "incomplete": 1},
        )
        rows = {
            row["pair_id"]: row
            for row in summary["paired_judgment_agreement"]["pair_results"]
        }
        self.assertEqual(rows["pair-2"]["status"], "incomplete")
        self.assertEqual(rows["pair-2"]["reason_codes"], ["pair_incomplete"])
        self.assertEqual(
            rows["pair-2"]["arm_states"],
            {"baseline": "unresolved", "candidate": "completed"},
        )
        self.assertEqual(
            rows["pair-2"]["arm_terminal_reasons"]["baseline"], "unknown_output"
        )
        self.assertEqual(rows["pair-2"]["attempt_counts"]["baseline"], 1)
        self.assertFalse(rows["pair-2"]["attempts_are_independent_samples"])
        self.assertEqual(rows["pair-3"]["status"], "ineligible")
        self.assertEqual(rows["pair-3"]["scoring_state"], "unavailable")
        self.assertIn(
            "artifact_not_verified:protocol_sha256",
            rows["pair-3"]["eligibility_reason_codes"],
        )
        self.assertEqual(
            summary["pair_eligibility"]["arm_state_counts"]["baseline"]["unresolved"],
            1,
        )
        self.assertEqual(
            summary["synthetic_resource_deltas"]["elapsed_seconds"][
                "eligible_pair_count"
            ],
            1,
        )

    def test_missing_resource_is_not_zero_and_blocks_full_coverage_total(self) -> None:
        assessment = classify_pair(pair_id="pair-1", replicate_id="replicate-1")
        baseline_resources = resources(elapsed_seconds=0, tokens=0, cost=0)
        candidate_resources = resources(elapsed_seconds=None, tokens=0, cost=0)

        summary = summarize(
            [assessment],
            [
                observation(
                    "pair-1",
                    baseline_assessment=exact_labels("synthetic-completed"),
                    candidate_assessment=exact_labels("synthetic-completed"),
                    baseline_resources=baseline_resources,
                    candidate_resources=candidate_resources,
                )
            ],
        )

        elapsed = summary["synthetic_resource_deltas"]["elapsed_seconds"]
        self.assertEqual(elapsed["paired_observation_count"], 0)
        self.assertEqual(elapsed["missing_pair_count"], 1)
        self.assertEqual(elapsed["missing_arm_counts"]["candidate"], 1)
        self.assertIsNone(elapsed["total_candidate_minus_baseline"])
        self.assertEqual(
            elapsed["total_unavailable_reason"], "incomplete_resource_coverage"
        )
        token_summary = summary["synthetic_resource_deltas"]["tokens"]
        self.assertEqual(token_summary["total_candidate_minus_baseline"], 0)
        self.assertEqual(token_summary["paired_observation_count"], 1)
        self.assertFalse(summary["version_ranking"]["ranked"])

    def test_unknown_is_a_valid_category_but_malformed_arm_is_unresolved(self) -> None:
        first = classify_pair(
            pair_id="pair-1",
            replicate_id="replicate-1",
            fixture_id="synthetic-unclear",
            order_assignment="baseline_first",
        )
        second = classify_pair(
            pair_id="pair-2",
            replicate_id="replicate-2",
            fixture_id="synthetic-unclear",
            order_assignment="candidate_first",
        )
        valid = exact_labels("synthetic-unclear")
        malformed = exact_labels("synthetic-unclear")
        malformed["extra"] = "not-a-rubric-field"

        summary = summarize(
            [first, second],
            [
                observation(
                    "pair-1",
                    baseline_assessment=valid,
                    candidate_assessment=valid,
                ),
                observation(
                    "pair-2",
                    baseline_assessment=valid,
                    candidate_assessment=malformed,
                ),
            ],
        )

        result = summary["paired_judgment_agreement"]
        self.assertEqual(result["scored_pair_count"], 1)
        self.assertEqual(result["unresolved_pair_count"], 1)
        self.assertEqual(
            result["pair_results"][1]["reason_codes"],
            ["invalid_assessment_schema_candidate"],
        )
        variability = result["repeat_variability_by_fixture_cache"][0]
        self.assertEqual(variability["scored_replicate_count"], 1)
        self.assertEqual(variability["state"], "unavailable")
        self.assertEqual(variability["reason"], "fewer_than_two_scored_replicates")

    def test_repeat_variability_is_fixture_and_cache_stratified(self) -> None:
        first = classify_pair(
            pair_id="pair-1",
            replicate_id="replicate-1",
            fixture_id="synthetic-completed",
            order_assignment="baseline_first",
            cache_condition="cold",
        )
        second = classify_pair(
            pair_id="pair-2",
            replicate_id="replicate-2",
            fixture_id="synthetic-completed",
            order_assignment="candidate_first",
            cache_condition="cold",
        )
        target = exact_labels("synthetic-completed")
        baseline1 = copy.deepcopy(target)
        baseline1["completion_evidence"] = "not_supported"
        baseline2 = copy.deepcopy(target)
        baseline2["completion_evidence"] = "not_supported"
        baseline2["validation_evidence"] = "none_reported"
        candidate2 = copy.deepcopy(target)

        summary = summarize(
            [first, second],
            [
                observation(
                    "pair-1",
                    baseline_assessment=baseline1,
                    candidate_assessment=target,
                ),
                observation(
                    "pair-2",
                    baseline_assessment=baseline2,
                    candidate_assessment=candidate2,
                ),
            ],
        )

        variability = summary["paired_judgment_agreement"][
            "repeat_variability_by_fixture_cache"
        ]
        self.assertEqual(len(variability), 1)
        self.assertEqual(variability[0]["scored_replicate_count"], 2)
        self.assertEqual(variability[0]["state"], "descriptive_only")
        self.assertAlmostEqual(variability[0]["range"], 0.2)
        self.assertAlmostEqual(
            variability[0]["sample_standard_deviation"], math.sqrt(0.02)
        )
        self.assertEqual(variability[0]["unit"], "agreement_fraction")
        self.assertEqual(variability[0]["cluster_unit"], "fixture_id")
        self.assertEqual(variability[0]["uncertainty"]["state"], "unavailable")

    def test_repeat_variability_requires_frozen_versions(self) -> None:
        first = classify_pair(
            pair_id="pair-1",
            replicate_id="replicate-1",
            order_assignment="baseline_first",
        )
        baseline, candidate = make_pair(
            pair_id="pair-2",
            replicate_id="replicate-2",
            order_assignment="candidate_first",
        )
        candidate["harness_version"] = "synthetic-harness-candidate-v3"
        candidate["harness_commit"] = "c" * 40
        second = benchmark_protocol.assess_pair(
            baseline,
            candidate,
            artifact_digests=ARTIFACT_DIGESTS,
            fixture_ids=FIXTURE_IDS,
            max_attempts=3,
        )
        target = exact_labels("synthetic-completed")

        summary = summarize(
            [first, second],
            [
                observation(
                    "pair-1",
                    baseline_assessment=target,
                    candidate_assessment=target,
                ),
                observation(
                    "pair-2",
                    baseline_assessment=target,
                    candidate_assessment=target,
                ),
            ],
        )

        variability = summary["paired_judgment_agreement"][
            "repeat_variability_by_fixture_cache"
        ][0]
        self.assertEqual(variability["scored_replicate_count"], 2)
        self.assertFalse(variability["versions_are_frozen"])
        self.assertEqual(variability["state"], "unavailable")
        self.assertEqual(variability["reason"], "comparison_versions_not_frozen")

    def test_mixed_cost_basis_keeps_pair_deltas_but_withholds_total(self) -> None:
        first = classify_pair(
            pair_id="pair-1",
            replicate_id="replicate-1",
            order_assignment="baseline_first",
        )
        second = classify_pair(
            pair_id="pair-2",
            replicate_id="replicate-2",
            order_assignment="candidate_first",
        )
        target = exact_labels("synthetic-completed")
        observations = []
        for pair_id, basis, order_cost in (
            ("pair-1", "cost_basis_v1", 0.5),
            ("pair-2", "cost_basis_v2", 0.75),
        ):
            observations.append(
                observation(
                    pair_id,
                    baseline_assessment=target,
                    candidate_assessment=target,
                    baseline_resources=resources(cost_basis=basis, cost=order_cost),
                    candidate_resources=resources(
                        cost_basis=basis, cost=order_cost + 0.25
                    ),
                )
            )

        summary = summarize([first, second], observations)
        costs = summary["synthetic_resource_deltas"]["cost"]

        self.assertTrue(costs["complete_pair_coverage"])
        self.assertEqual(costs["paired_observation_count"], 2)
        self.assertEqual(len(costs["paired_deltas"]), 2)
        self.assertIsNone(costs["total_candidate_minus_baseline"])
        self.assertEqual(costs["total_unavailable_reason"], "mixed_resource_basis")
        self.assertIsNone(costs["basis"])

    def test_resource_basis_mismatch_is_retained_as_unpaired(self) -> None:
        assessment = classify_pair(pair_id="pair-1", replicate_id="replicate-1")
        baseline_resources = resources(token_basis="prompt_tokens", currency="USD")
        candidate_resources = resources(token_basis="completion_tokens", currency="EUR")
        summary = summarize(
            [assessment],
            [
                observation(
                    "pair-1",
                    baseline_assessment=exact_labels("synthetic-completed"),
                    candidate_assessment=exact_labels("synthetic-completed"),
                    baseline_resources=baseline_resources,
                    candidate_resources=candidate_resources,
                )
            ],
        )

        tokens = summary["synthetic_resource_deltas"]["tokens"]
        self.assertEqual(tokens["paired_observation_count"], 0)
        self.assertEqual(tokens["unpaired_reason_counts"], {"token_basis_mismatch": 1})
        self.assertEqual(tokens["incompatible_basis_pair_count"], 1)
        self.assertIsNone(tokens["total_candidate_minus_baseline"])
        self.assertEqual(
            tokens["total_unavailable_reason"], "incomplete_resource_coverage"
        )
        cost = summary["synthetic_resource_deltas"]["cost"]
        self.assertEqual(cost["unpaired_reason_counts"], {"currency_mismatch": 1})
        self.assertEqual(cost["paired_observation_count"], 0)
        self.assertEqual(cost["incompatible_basis_pair_count"], 1)

    def test_invalid_resources_count_as_invalid_not_zero(self) -> None:
        assessment = classify_pair(pair_id="pair-1", replicate_id="replicate-1")
        baseline_resources = resources()
        candidate_resources = resources(elapsed_seconds=float("nan"))
        summary = summarize(
            [assessment],
            [
                observation(
                    "pair-1",
                    baseline_assessment=exact_labels("synthetic-completed"),
                    candidate_assessment=exact_labels("synthetic-completed"),
                    baseline_resources=baseline_resources,
                    candidate_resources=candidate_resources,
                )
            ],
        )

        elapsed = summary["synthetic_resource_deltas"]["elapsed_seconds"]
        self.assertEqual(elapsed["invalid_arm_counts"]["candidate"], 1)
        self.assertEqual(elapsed["invalid_pair_count"], 1)
        self.assertEqual(elapsed["missing_pair_count"], 0)
        self.assertEqual(elapsed["unpaired_reason_counts"], {"invalid_candidate": 1})
        self.assertIsNone(elapsed["total_candidate_minus_baseline"])

        malformed_token_resources = resources()
        malformed_token_resources["tokens"] = {"value": 100}
        summary = summarize(
            [assessment],
            [
                observation(
                    "pair-1",
                    baseline_assessment=exact_labels("synthetic-completed"),
                    candidate_assessment=exact_labels("synthetic-completed"),
                    baseline_resources=resources(),
                    candidate_resources=malformed_token_resources,
                )
            ],
        )
        tokens = summary["synthetic_resource_deltas"]["tokens"]
        self.assertEqual(tokens["invalid_arm_counts"]["candidate"], 1)
        self.assertEqual(tokens["unpaired_reason_counts"], {"invalid_candidate": 1})

    def test_duplicate_or_unclassified_pair_observations_are_rejected(self) -> None:
        assessment = classify_pair(pair_id="pair-1", replicate_id="replicate-1")
        item = observation(
            "pair-1",
            baseline_assessment=exact_labels("synthetic-completed"),
            candidate_assessment=exact_labels("synthetic-completed"),
        )
        with self.assertRaisesRegex(ValueError, "unique pair_id"):
            summarize([assessment], [item, copy.deepcopy(item)])

        extra = copy.deepcopy(item)
        extra["pair_id"] = "pair-unknown"
        with self.assertRaisesRegex(ValueError, "does not match a classified pair"):
            summarize([assessment], [extra])


if __name__ == "__main__":
    unittest.main()
