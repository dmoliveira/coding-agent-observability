"""Offline scoring keeps synthetic judgment evidence and abstention distinct."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from integrations import agent_evaluate, judgment_calibration

FIXTURE_DIR = ROOT / "tests/fixtures/harness-benchmark/v1"
REFERENCE_LABELS = {
    case_id: dict(labels) for case_id, labels in agent_evaluate.SYNTHETIC_LABELS.items()
}


def summarize(predictions: list[dict]) -> dict:
    return judgment_calibration.summarize_calibration(
        predictions,
        REFERENCE_LABELS,
        agent_evaluate.ENUMS,
        model_id="synthetic-model-v1",
        rubric_id=agent_evaluate.RUBRIC,
        rubric_version=agent_evaluate.RUBRIC_VERSION,
        fixture_pack_id=agent_evaluate.SYNTHETIC_FIXTURE_PACK_ID,
        fixture_pack_version=agent_evaluate.SYNTHETIC_FIXTURE_PACK_VERSION,
    )


def exact_predictions() -> list[dict]:
    return [
        {"case_id": case_id, "assessment": copy.deepcopy(labels)}
        for case_id, labels in REFERENCE_LABELS.items()
    ]


class JudgmentCalibrationTests(unittest.TestCase):
    def test_exact_agreement_preserves_attested_reference_and_unknown_labels(
        self,
    ) -> None:
        summary = summarize(exact_predictions())

        self.assertEqual(summary["schema_version"], "aiobs.judgment-calibration.v1")
        self.assertEqual(summary["metric_id"], "X6")
        self.assertFalse(summary["metric_delivery_advanced"])
        self.assertEqual(summary["evidence_class"], "derived")
        self.assertEqual(summary["judgment_evidence_class"], "local_model_judgment")
        self.assertEqual(summary["reference_evidence_class"], "attested")
        self.assertEqual(summary["actor"], "local_model")
        self.assertEqual(summary["model_id"], "synthetic-model-v1")
        self.assertEqual(summary["matched_fields"], 15)
        self.assertEqual(summary["total_fields"], 15)
        self.assertEqual(summary["scored_fields"], 15)
        self.assertEqual(summary["unresolved_fields"], 0)
        self.assertEqual(summary["agreement_rate"], 1)
        self.assertEqual(summary["explicit_unknown_judgment_count"], 2)
        self.assertEqual(summary["unknown_reference_label_count"], 2)
        self.assertEqual(summary["case_counts"]["unresolved_cases"], 0)
        self.assertEqual(summary["uncertainty"]["state"], "unavailable")
        self.assertIn("not independently validated", summary["reference_provenance"])

    def test_disagreements_and_unresolved_outputs_have_separate_denominators(
        self,
    ) -> None:
        predictions = exact_predictions()
        first = predictions[0]["assessment"]
        first["completion_evidence"] = "not_supported"
        first["validation_evidence"] = "unknown"
        predictions[1]["assessment"] = {
            **predictions[1]["assessment"],
            "extra": "ignored-content-must-not-leak",
        }
        predictions.pop()

        summary = summarize(predictions)

        self.assertEqual(summary["matched_fields"], 3)
        self.assertEqual(summary["total_fields"], 15)
        self.assertEqual(summary["scored_fields"], 5)
        self.assertEqual(summary["disagreement_count"], 2)
        self.assertEqual(summary["unresolved_fields"], 10)
        self.assertEqual(summary["agreement_rate"], 0.6)
        self.assertEqual(summary["explicit_unknown_judgment_count"], 1)
        self.assertEqual(
            summary["case_counts"]["unresolved_reason_counts"],
            {"missing_prediction": 1, "invalid_assessment_schema": 1},
        )
        self.assertEqual(
            summary["field_calibration"]["completion_evidence"][
                "disagreement_categories"
            ],
            [{"reference": "supported", "judgment": "not_supported", "count": 1}],
        )
        self.assertNotIn("ignored-content-must-not-leak", json.dumps(summary))

    def test_empty_predictions_are_unresolved_not_zero_agreement(self) -> None:
        summary = summarize([])

        self.assertEqual(summary["matched_fields"], 0)
        self.assertEqual(summary["scored_fields"], 0)
        self.assertEqual(summary["unresolved_fields"], 15)
        self.assertIsNone(summary["agreement_rate"])
        self.assertEqual(
            summary["case_counts"]["unresolved_reason_counts"]["missing_prediction"],
            3,
        )

    def test_duplicate_or_unregistered_fixture_ids_are_rejected(self) -> None:
        predictions = exact_predictions()
        with self.assertRaisesRegex(ValueError, "unique"):
            summarize([predictions[0], predictions[0]])

        unregistered = {
            "case_id": "not-in-pack",
            "assessment": copy.deepcopy(REFERENCE_LABELS["synthetic-completed"]),
        }
        with self.assertRaisesRegex(ValueError, "not in the reference fixture pack"):
            summarize([unregistered])

    def test_invalid_reference_and_metadata_schemas_fail_closed(self) -> None:
        invalid_reference = copy.deepcopy(REFERENCE_LABELS)
        invalid_reference["synthetic-completed"]["completion_evidence"] = "success"
        with self.assertRaisesRegex(ValueError, "declared rubric categories"):
            judgment_calibration.summarize_calibration(
                exact_predictions(),
                invalid_reference,
                agent_evaluate.ENUMS,
                model_id="synthetic-model-v1",
                rubric_id=agent_evaluate.RUBRIC,
                rubric_version=agent_evaluate.RUBRIC_VERSION,
                fixture_pack_id=agent_evaluate.SYNTHETIC_FIXTURE_PACK_ID,
                fixture_pack_version=agent_evaluate.SYNTHETIC_FIXTURE_PACK_VERSION,
            )

        with self.assertRaisesRegex(ValueError, "model_id"):
            judgment_calibration.summarize_calibration(
                exact_predictions(),
                REFERENCE_LABELS,
                agent_evaluate.ENUMS,
                model_id="  ",
                rubric_id=agent_evaluate.RUBRIC,
                rubric_version=agent_evaluate.RUBRIC_VERSION,
                fixture_pack_id=agent_evaluate.SYNTHETIC_FIXTURE_PACK_ID,
                fixture_pack_version=agent_evaluate.SYNTHETIC_FIXTURE_PACK_VERSION,
            )

    def test_calibration_is_pure_and_does_not_invoke_the_model_path(self) -> None:
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
            summary = summarize(exact_predictions())

        self.assertEqual(summary["matched_fields"], 15)
        run.assert_not_called()
        evaluate.assert_not_called()

    def test_artifact_versions_match_the_calibration_metadata(self) -> None:
        cases = json.loads((FIXTURE_DIR / "cases.json").read_text(encoding="utf-8"))
        rubric = json.loads((FIXTURE_DIR / "rubric.json").read_text(encoding="utf-8"))
        labels = json.loads(
            (FIXTURE_DIR / "held-out-labels.json").read_text(encoding="utf-8")
        )

        self.assertEqual(
            cases["fixture_pack_id"], agent_evaluate.SYNTHETIC_FIXTURE_PACK_ID
        )
        self.assertEqual(
            cases["fixture_pack_version"], agent_evaluate.SYNTHETIC_FIXTURE_PACK_VERSION
        )
        self.assertEqual(rubric["rubric_id"], agent_evaluate.RUBRIC)
        self.assertEqual(rubric["rubric_version"], agent_evaluate.RUBRIC_VERSION)
        self.assertEqual(labels["evidence_class"], "attested")
        self.assertEqual(labels["cases"], REFERENCE_LABELS)


if __name__ == "__main__":
    unittest.main()
