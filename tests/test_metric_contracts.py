"""Contract-registry invariants for harness metrics and identities."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs/specs/harness-metric-contracts.json"


class MetricContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        cls.families = cls.registry["families"]

    def test_registry_has_all_stable_families_and_owned_components(self) -> None:
        families = {family["id"]: family for family in self.families}
        self.assertEqual(len(families), 82)
        self.assertEqual(self.registry["metric_family_count"], 82)
        self.assertEqual(len(self.registry["families"]), 82)
        self.assertEqual(self.registry["metric_component_count"], 268)
        self.assertTrue(
            all(re.fullmatch(r"[CTFLQAMRX][0-9]+", key) for key in families)
        )

        components = [
            component for family in self.families for component in family["components"]
        ]
        component_ids = [component["id"] for component in components]
        self.assertEqual(len(component_ids), len(set(component_ids)))
        self.assertEqual(len(components), self.registry["metric_component_count"])
        self.assertGreater(len(components), len(self.families))
        for family in self.families:
            self.assertTrue(family["owner_task_id"].startswith("task_"))
            self.assertTrue(family["definition"])
            self.assertTrue(family["chart"])
            self.assertTrue(family["readiness"])
            self.assertNotIn("delivery_state", family)
            self.assertEqual(
                sum(family["delivery_state_summary"].values()),
                len(family["components"]),
            )
            self.assertGreaterEqual(len(family["components"]), 1)
            for component in family["components"]:
                self.assertTrue(component["name"])
                self.assertTrue(component["calculation_rule"])
                self.assertTrue(component["formula"])
                self.assertTrue(component["eligibility"])
                self.assertTrue(component["exclusions"])
                self.assertTrue(component["timestamp_basis"])
                self.assertEqual(
                    set(component["cohort_contract"]),
                    set(self.registry["cohort_contract_schema"]["required"]),
                )
                if component["definition_version"] == "1.2":
                    self.assertEqual(
                        component["owner_review_status"],
                        "required_before_metric_delivery",
                    )
                self.assertIn(
                    component["timestamp_basis_status"],
                    {"observed", "requires_source_probe"},
                )
                self.assertEqual(component["id"].split(".", 1)[0], family["id"])
                self.assertNotIn("/", component["unit"])
                self.assertIn(
                    component["delivery_state"], self.registry["delivery_states"]
                )
                if component["delivery_state"] == "implemented":
                    self.assertTrue(component["delivery_evidence"])
                    self.assertEqual(component["contract_conformance"], "legacy")
                    for reference in component["delivery_evidence"]:
                        path, separator, symbol = reference.partition("::")
                        evidence_path = ROOT / path
                        self.assertTrue(evidence_path.is_file(), reference)
                        if separator and symbol:
                            evidence = evidence_path.read_text(encoding="utf-8")
                            self.assertTrue(
                                symbol in evidence
                                or symbol.rsplit(".", 1)[-1] in evidence,
                                reference,
                            )
                if component["delivery_state"] == "explicitly-unavailable-with-reason":
                    self.assertTrue(component["delivery_reason"])
                if component["unit"] == "fraction":
                    self.assertTrue(component["numerator"])
                    self.assertTrue(component["denominator"])
                    self.assertIn("/", component["formula"])
                cohort = component["cohort_contract"]
                contract_schema = self.registry["cohort_contract_schema"]["properties"]
                self.assertIn(
                    cohort["population_basis"],
                    contract_schema["population_basis"]["enum"],
                )
                self.assertIn(
                    cohort["cohort_state"], contract_schema["cohort_state"]["enum"]
                )
                self.assertIn(
                    cohort["sampling_design"],
                    contract_schema["sampling_design"]["enum"],
                )
                self.assertIn(
                    cohort["pooling_policy"], contract_schema["pooling_policy"]["enum"]
                )
                self.assertIn(
                    cohort["cluster_unit"], contract_schema["cluster_unit"]["enum"]
                )
                if cohort["selection_probability_status"] == "known":
                    self.assertIsInstance(cohort["selection_probability"], (int, float))
                else:
                    self.assertIsNone(cohort["selection_probability"])
                if component["aggregation"] == "count":
                    self.assertIn(component["id"], component["formula"])

    def test_metric_instances_require_formula_population_provenance_and_time_basis(
        self,
    ) -> None:
        required = set(self.registry["metric_fields_required"])
        self.assertTrue(
            {
                "schema_version",
                "metric_id",
                "definition_version",
                "formula",
                "population",
                "eligibility",
                "exclusions",
                "value",
                "unit",
                "aggregation",
                "numerator",
                "denominator",
                "evidence_class",
                "source_version",
                "producer_version",
                "transform_version",
                "transform_version_reason",
                "readiness",
                "delivery_state",
                "coverage",
                "sample_size",
                "counter_epoch",
                "counter_epoch_reason",
                "source_clock_uncertainty_ms",
                "source_clock_uncertainty_reason",
                "watermark",
                "watermark_reason",
                "late_data_policy",
                "uncertainty",
                "quantile_method",
                "quantile_method_reason",
                "sample_size_reason",
                "timestamp_basis",
                "window_from",
                "window_to",
                "timezone",
                "computed_at",
                "dimensions",
                "interpretation",
                "drilldown",
                "availability_reason",
                "provenance_notes",
            }
            <= required
        )
        self.assertIn("unavailable", self.registry["missing_states"])
        self.assertIn("censored", self.registry["missing_states"])
        self.assertIn(
            "explicitly-unavailable-with-reason", self.registry["delivery_states"]
        )

    def test_json_schema_is_closed_versioned_and_requires_reasons_for_unknowns(
        self,
    ) -> None:
        schema = self.registry["metric_instance_schema"]
        self.assertEqual(
            schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
        )
        self.assertEqual(self.registry["schema_version"], "aiobs.metric-contract.v1.2")
        self.assertEqual(schema["title"], "AI Observatory metric instance v1.2")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]), set(self.registry["metric_fields_required"])
        )
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            self.registry["schema_version"],
        )
        self.assertEqual(
            schema["properties"]["delivery_state"]["enum"],
            self.registry["delivery_states"],
        )
        self.assertIn("null", schema["properties"]["value"]["type"])
        self.assertTrue(
            any(
                "availability_reason" in item.get("then", {}).get("properties", {})
                for item in schema["allOf"]
            )
        )
        self.assertTrue(
            any(
                "transform_version_reason" in item.get("then", {}).get("properties", {})
                for item in schema["allOf"]
            )
        )
        self.assertTrue(
            any(
                item.get("if", {})
                .get("properties", {})
                .get("aggregation", {})
                .get("enum")
                == ["fraction", "ratio"]
                and item.get("then", {})
                .get("properties", {})
                .get("numerator", {})
                .get("type")
                == "number"
                for item in schema["allOf"]
            )
        )
        self.assertTrue(
            any(
                item.get("if", {})
                .get("properties", {})
                .get("denominator", {})
                .get("const")
                == 0
                and item.get("then", {})
                .get("properties", {})
                .get("value", {})
                .get("type")
                == "null"
                for item in schema["allOf"]
            )
        )
        self.assertTrue(
            any(
                item.get("if", {})
                .get("properties", {})
                .get("metric_id", {})
                .get("pattern")
                == "^C3\\."
                and item.get("then", {})
                .get("properties", {})
                .get("counter_epoch", {})
                .get("type")
                == "string"
                for item in schema["allOf"]
            )
        )
        self.assertFalse(schema["properties"]["dimensions"]["additionalProperties"])
        self.assertFalse(schema["properties"]["coverage"]["additionalProperties"])
        self.assertIn("reasons", schema["properties"]["coverage"]["required"])
        self.assertIn("sampling_notes", schema["properties"]["population"]["required"])
        self.assertIn(
            "late_data_policy", schema["properties"]["population"]["required"]
        )
        self.assertIn("basis_kind", schema["properties"]["population"]["required"])
        self.assertIn(
            "source_clock_uncertainty_reason",
            schema["properties"]["population"]["required"],
        )
        self.assertIn("complete", schema["properties"]["coverage"]["required"])
        self.assertIn(
            "independent_cluster_count",
            schema["properties"]["uncertainty"]["required"],
        )
        self.assertIn(
            "null", schema["properties"]["coverage"]["properties"]["eligible"]["type"]
        )
        metric_id_pattern = schema["properties"]["metric_id"]["pattern"]
        self.assertRegex("L4.input_tokens", metric_id_pattern)
        self.assertNotRegex("L4/input_tokens", metric_id_pattern)
        self.assertNotIn("input", schema["properties"])
        self.assertNotIn("output", schema["properties"])

    def test_identity_and_outcome_contracts_prohibit_inferred_successful_joins(
        self,
    ) -> None:
        identities = self.registry["identity_types"]
        self.assertTrue(
            {
                "request_attempt",
                "logical_operation",
                "nested_envelope",
                "session_turn_busy_period",
                "task_artifact_revision_check",
                "agent_delegation",
                "producer_epoch",
            }
            <= identities.keys()
        )
        self.assertIn("timestamp", identities["logical_operation"])
        self.assertIn("unknown", self.registry["execution_states"])
        self.assertIn("expected-negative", self.registry["terminal_results"])
        self.assertIn("unknown", self.registry["terminal_results"])
        self.assertTrue(
            any("No title" in rule for rule in self.registry["privacy_rules"])
        )
        self.assertTrue(
            any("prompts" in rule for rule in self.registry["privacy_rules"])
        )

    def test_documentation_explains_atomic_components_and_delivery_ledger(self) -> None:
        document = (ROOT / "docs/specs/harness-metric-contracts.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("single-unit components", document)
        self.assertIn("missing is never zero", document)
        self.assertIn("Delivery status", document)
        self.assertIn("busy-period", document)
        self.assertIn("watermark", document)
        self.assertIn("Do not create a universal AI/harness health score", document)
        self.assertIn("Current source limitations", document)
        unsupported = {item["id"] for item in self.registry["unsupported_correlations"]}
        self.assertTrue(
            {
                "busy-period-to-turn-or-task",
                "event-replay-or-completeness",
                "langfuse-native-parent-child-and-high-cardinality-joins",
                "nested-envelope-to-child-operation",
                "reported-versus-inferred-cost",
                "compaction-start-to-terminal",
                "legacy-structural-id-scope",
            }
            <= unsupported
        )

    def test_registry_matches_pinned_source_versions_and_does_not_infer_late_identity(
        self,
    ) -> None:
        compatibility = self.registry["source_compatibility"]
        package_lock = json.loads(
            (ROOT / "integrations/opencode-global/package-lock.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            compatibility["langfuse_plugin"],
            "@langfuse/opencode-observability-plugin "
            + package_lock["packages"][
                "node_modules/@langfuse/opencode-observability-plugin"
            ]["version"],
        )
        self.assertEqual(compatibility["opencode"], "2.2.5")
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn("langfuse/langfuse:4.82.0", compose)
        self.assertEqual(compatibility["langfuse"].split(";")[0], "4.82.0")
        self.assertEqual(len(compatibility["evidence"]), 3)
        self.assertIn("opencode --version", compatibility["evidence"][0]["evidence"])
        self.assertIn("timestamp", self.registry["identity_types"]["logical_operation"])
        self.assertIn(
            "turn", self.registry["identity_types"]["session_turn_busy_period"]
        )
        self.assertTrue(
            any("latest-prompt" in rule for rule in self.registry["privacy_rules"])
        )

    def test_current_delivery_ledger_does_not_overstate_legacy_or_unavailable_paths(
        self,
    ) -> None:
        components = {
            component["id"]: component
            for family in self.families
            for component in family["components"]
        }
        self.assertEqual(components["L4.input_tokens"]["delivery_state"], "implemented")
        self.assertEqual(
            components["L4.input_tokens"]["contract_conformance"], "legacy"
        )
        self.assertEqual(
            components["L5.inferred_cost"]["delivery_state"],
            "explicitly-unavailable-with-reason",
        )
        self.assertEqual(
            components["L8.compaction_duration"]["delivery_state"],
            "explicitly-unavailable-with-reason",
        )
        self.assertEqual(components["Q3.acceptance_rate"]["delivery_state"], "planned")

    def test_family_specific_cohort_and_coverage_contracts(self) -> None:
        components = {
            component["id"]: component
            for family in self.families
            for component in family["components"]
        }
        for component_id in (
            "C3.receipt_admission_fraction",
            "C3.receipt_drop_fraction",
            "C3.admission_emission_fraction",
            "C3.emission_acknowledgement_fraction",
            "C3.acknowledgement_ingestion_fraction",
            "C3.reconciled_completeness",
        ):
            component = components[component_id]
            cohort = component["cohort_contract"]
            self.assertEqual(
                cohort["population_basis"], "counter_reconciliation_window"
            )
            self.assertEqual(cohort["cluster_unit"], "producer_epoch")
            self.assertIn("unchanged", cohort["closure_rule"])
            self.assertIn("watermark", cohort["late_data_policy"])
        self.assertEqual(
            components["T3.classification_coverage"]["denominator"],
            "eligible attempts in a closed execution cohort",
        )
        self.assertIn("unresolved", components["T3.classification_coverage"]["formula"])
        self.assertIn(
            "T3 coverage", components["T4.unexpected_technical_rate"]["formula"]
        )
        self.assertIn(
            "completed intervals only",
            components["F3.admission_to_terminal"]["formula"],
        )
        self.assertIn(
            "declared horizon", components["Q6.mature_reopen_rate"]["formula"]
        )
        self.assertIn(
            "mature",
            components["Q6.mature_reopen_rate"]["cohort_contract"][
                "right_censoring_policy"
            ],
        )
        self.assertIn("zero relevant", components["M3.precision_at_k"]["formula"])
        self.assertIn(
            "explicit_correct_abstentions",
            components["M3.no_relevant_abstention"]["formula"],
        )
        self.assertIn(
            "complete eligible cohort", components["X4.resource_yield"]["formula"]
        )
        self.assertIn("full-cohort", components["X4.resource_yield"]["formula"])
        self.assertIn(
            "T3", self.registry["cohort_and_statistics_policy"]["family_specific"]
        )
        self.assertIn("multiplicity", self.registry["cohort_and_statistics_policy"])

    def test_hand_calculated_cohort_clock_counter_weighting_and_zero_cases(
        self,
    ) -> None:
        examples = {item["id"]: item for item in self.registry["contract_examples"]}

        followup = examples["closed_reopen_follow_up"]
        inputs = followup["inputs"]
        expected = followup["expected"]
        matured_indices = [
            index
            for index, accepted_at in enumerate(inputs["accepted_at_minutes"])
            if accepted_at is not None
            and accepted_at
            + inputs["horizon_minutes"]
            + inputs["lateness_allowance_minutes"]
            <= inputs["as_of_minutes"]
            and inputs["watermark_minutes"]
            >= accepted_at
            + inputs["horizon_minutes"]
            + inputs["lateness_allowance_minutes"]
        ]
        self.assertEqual(len(matured_indices), expected["mature_denominator"])
        self.assertEqual(
            sum(inputs["reopened"][index] is True for index in matured_indices),
            expected["reopened_numerator"],
        )
        self.assertEqual(
            expected["reopen_fraction"],
            expected["reopened_numerator"] / expected["mature_denominator"],
        )
        immature_watermark = 16
        self.assertEqual(
            sum(
                accepted_at is not None
                and accepted_at
                + inputs["horizon_minutes"]
                + inputs["lateness_allowance_minutes"]
                <= inputs["as_of_minutes"]
                and immature_watermark
                >= accepted_at
                + inputs["horizon_minutes"]
                + inputs["lateness_allowance_minutes"]
                for accepted_at in inputs["accepted_at_minutes"]
            ),
            1,
        )
        self.assertEqual(
            sum(value is not None for value in inputs["accepted_at_minutes"])
            - len(matured_indices),
            expected["immature_censored"],
        )
        self.assertEqual(
            sum(value is None for value in inputs["accepted_at_minutes"]),
            expected["missing_entry_time"],
        )

        duration = examples["completed_duration_with_censoring"]
        values = sorted(duration["inputs"]["completed_elapsed_seconds"])
        p50 = (values[0] + values[1]) / 2
        self.assertEqual(p50, duration["expected"]["completed_only_p50_seconds"])
        self.assertEqual(duration["expected"]["completed_n"], len(values))
        self.assertEqual(
            duration["expected"]["censored"],
            duration["inputs"]["right_censored_running"],
        )

        counters = examples["same_epoch_collector_stage_reconciliation"]
        inputs = counters["inputs"]
        expected = counters["expected"]
        self.assertEqual(len(set(inputs["epoch_by_stage"].values())), 1)
        self.assertTrue(inputs["watermark_passed_window_end"])
        counts = inputs["counts"]
        self.assertAlmostEqual(
            counts["queue_admitted"] / counts["eligible_received"],
            expected["receipt_admission_fraction"],
        )
        self.assertAlmostEqual(
            counts["queue_dropped"] / counts["eligible_received"],
            expected["receipt_drop_fraction"],
        )
        self.assertAlmostEqual(
            counts["unique_emitted"] / counts["queue_admitted"],
            expected["admission_emission_fraction"],
        )
        self.assertAlmostEqual(
            counts["acknowledged"] / counts["unique_emitted"],
            expected["emission_acknowledgement_fraction"],
        )
        self.assertAlmostEqual(
            counts["confirmed_ingested"] / counts["acknowledged"],
            expected["acknowledgement_ingestion_fraction"],
        )
        self.assertAlmostEqual(
            counts["confirmed_ingested"] / counts["unique_emitted"],
            expected["reconciled_completeness"],
        )

        def stage_ratio(
            numerator: str,
            denominator: str,
            epochs: dict[str, str],
            watermark_passed: bool | None = None,
        ) -> float | None:
            if watermark_passed is None:
                watermark_passed = inputs["watermark_passed_window_end"]
            if not watermark_passed:
                return None
            if epochs[numerator] != epochs[denominator] or counts[denominator] == 0:
                return None
            return counts[numerator] / counts[denominator]

        reset_epochs = dict(inputs["epoch_by_stage"], acknowledged="worker-8")
        self.assertIsNone(
            stage_ratio("confirmed_ingested", "acknowledged", reset_epochs)
        )
        self.assertIsNone(
            stage_ratio(
                "confirmed_ingested",
                "acknowledged",
                inputs["epoch_by_stage"],
                watermark_passed=False,
            )
        )

        clock = examples["clock_uncertainty_bounds_ordering"]
        clock_in = clock["inputs"]
        duration = clock_in["end_time_seconds"] - clock_in["start_time_seconds"]
        uncertainty = (
            clock_in["start_uncertainty_seconds"] + clock_in["end_uncertainty_seconds"]
        )
        self.assertAlmostEqual(duration, clock["expected"]["duration_seconds"])
        self.assertAlmostEqual(
            uncertainty, clock["expected"]["duration_uncertainty_seconds"]
        )
        self.assertAlmostEqual(
            duration - uncertainty,
            clock["expected"]["duration_bounds_seconds"][0],
        )
        self.assertAlmostEqual(
            duration + uncertainty,
            clock["expected"]["duration_bounds_seconds"][1],
        )
        self.assertEqual(
            "ambiguous"
            if clock_in["cross_source_event_gap_seconds"]
            <= clock_in["cross_source_uncertainty_seconds"]
            else "ordered",
            clock["expected"]["cross_source_relation"],
        )

        weighted = examples["weighted_sample_vs_hand_selected"]
        sample = weighted["inputs"]["sample"]
        raw = sum(row["success"] for row in sample) / len(sample)
        weighted_fraction = sum(
            row["success"] / row["selection_probability"] for row in sample
        ) / sum(1 / row["selection_probability"] for row in sample)
        self.assertAlmostEqual(raw, weighted["expected"]["unweighted_sample_fraction"])
        self.assertAlmostEqual(
            weighted_fraction,
            weighted["expected"]["inverse_probability_weighted_fraction"],
        )
        self.assertFalse(
            weighted["expected"]["hand_selected_population_inference_allowed"]
        )
        self.assertIn("hand-selected", weighted["rule"])
        zero = examples["zero_denominator_is_unavailable"]
        value = (
            zero["inputs"]["numerator"] / zero["inputs"]["denominator"]
            if zero["inputs"]["denominator"] > 0
            else None
        )
        self.assertIsNone(value)
        self.assertIsNone(zero["expected"]["value"])


if __name__ == "__main__":
    unittest.main()
