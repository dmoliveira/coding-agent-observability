# Synthetic harness benchmark contract

**State:** versioned offline fixture and pair-eligibility contract; no benchmark runner, model execution, real-evidence adapter, or metric delivery is implemented here.
**Owner:** `task_6288`; plan `memory_411`; shared metric contract `memory_398`.
**Fixture artifacts:** [`tests/fixtures/harness-benchmark/v1/`](../../tests/fixtures/harness-benchmark/v1/).

This contract supplies a minimal deterministic starting point for independent synthetic assessment and controlled comparisons. It does not establish deployed task outcomes, model quality, production task-lifecycle capture, or population-level effectiveness. All fixture prose and run records used by tests are synthetic. The optional `agent_evaluate.py --demo` remains an explicit LM Studio invocation. The separate offline calibration helper (`task_6298`) summarizes returned labels against held-out synthetic targets only; it makes no model call, reports attested-reference provenance and unresolved outputs, and does not advance X6 delivery state.

## Versioned artifacts and evidence classes

The v1 bundle has four separately hashed UTF-8 JSON artifacts, pinned by `SHA256SUMS` using SHA-256 over exact file bytes (including the trailing newline). The run field `fixture_pack_sha256` is a composite SHA-256 over `cases.json` and `held-out-labels.json`, in that order, updating with each UTF-8 filename, a NUL byte, the exact file bytes, and a trailing NUL byte. This binds the hidden targets into pack identity without placing them in the model-facing case artifact.

| Artifact | Purpose | Evidence treatment |
| --- | --- | --- |
| `cases.json` | Three fixed synthetic task-evidence inputs and their stable fixture IDs. Contains no target labels. | Synthetic inputs; not observed production evidence. |
| `rubric.json` | Versioned rubric instructions, allowed output categories, and exact-field scoring definition. | Defines a judged output schema; not a result. |
| `held-out-labels.json` | Separate targets keyed by fixture ID. Must not be sent to the model or included in model-facing input. | `attested` curated synthetic targets, not independently validated truth or real acceptance. |
| `protocol.json` | Versioned budgets, run identity fields, pair eligibility, comparison controls, and inference limits. | Protocol only; no run or metric is implied. |

Every run records the composite fixture-pack, rubric and protocol content digests. Any changed artifact requires an intentional version and lock update; consumers must not infer identity from filenames. Fixture IDs are declared synthetic identities and are never joins to Codememory tasks or production sessions. Model output remains `judged`; pair eligibility and any later paired difference are `derived`. Keep source claims, judgments, attestations, and unavailable states separate. `unknown` is a valid rubric category; malformed or absent output is unresolved, not an `unknown` label, zero, or pass.

## Run record and eligibility

`benchmark_protocol.assess_pair` accepts strict metadata-only `aiobs.benchmark-run.v1` records plus the expected artifact digests and fixture-ID set from a separately digest-verified bundle. It performs no I/O and consumes no model output; it does not itself read or authenticate the lock file. Required identity includes cohort, fixture/rubric/protocol digests, fixture/pair/replicate IDs, comparison axis, seed, memory snapshot, reset protocol, washout, cache stratum, workspace-template digest, order assignment, and arm position. Provenance records actual harness version and commit plus actual model ID and resolved version; missing version data is not replaced with a default.

A pair is eligible only when:

1. Each arm record covers exactly one fixture. Baseline and candidate have the exact same cohort, frozen fixture/rubric/protocol identities, fixture, pair and replicate IDs, seed, snapshot, reset and washout, cache condition, workspace template, comparison axis and assigned order.
2. The arm positions match the declared `baseline_first` or `candidate_first` assignment, and the arms used distinct disposable workspace IDs built from the same template.
3. Only the declared version axis changes: model ID/resolved version is held fixed for a harness comparison; harness version/commit is held fixed for a model comparison; the selected identity tuple must actually differ.
4. Both run states are `completed` and each run has a well-formed bounded attempt chain.

`completed` means the benchmark evaluation invocation returned a schema-valid label result; it does not mean the represented task was accepted, succeeded, or satisfied. `unresolved`, `censored`, and `aborted` are retained separately per arm. Any such arm makes the pair incomplete and excluded from paired effects; no imputation is permitted. A condition, artifact digest, or fixture-membership mismatch makes the pair ineligible with reason codes. Keep a preregistered pair plan for every scheduled arm; if no run record exists, retain that arm as `unavailable` with `missing_run_record`. If the actual model/harness identity cannot be resolved, retain `unavailable` with `version_unavailable` rather than inventing a version. The helper summarizes supplied run pairs only and cannot reconstruct omitted plans; it does not claim a complete cohort. It retains arm-state/reason, order and cache-stratum counts, and marks effect size and uncertainty unavailable when no scored outcomes/estimator are supplied.

Retries are attempt records nested under one arm run and replicate. A retry must reference the immediately preceding failed or unresolved attempt, use a unique attempt ID, and remain within the declared attempt budget. It never increases replicate count. Repeated pair replicates are grouped by fixture ID; report pair count and distinct fixture-cluster count separately. The fixture cluster is not evidence of a representative production sample.

## Preregistered comparison controls

- Freeze the fixture pack, rubric, cohort, case list, and comparison outcome before a run. Keep held-out targets unavailable to the runner until the output is frozen and scoring begins.
- Use isolated disposable workspaces created separately for each arm from one pinned template; never reuse mutable state. Record a digest for any frozen memory snapshot. If a scenario has no memory input, state `not_applicable` with a reason rather than omitting the field.
- Record the reset protocol and washout duration for each arm and apply the same values to both sides of a pair. A future memory-on/off comparison requires separately frozen snapshots and explicit reset/washout tests.
- Assign and record baseline-first or candidate-first order; counterbalance across preregistered pair blocks. Do not infer order from timestamps.
- Record `cold` or `warm` cache conditions and analyze them as separate strata; never pool them implicitly.
- Record actual harness commit/version, model ID/resolved version, and all artifact digests. A configured/default model name is not proof of the model that ran.

The cohort is the three frozen fixture IDs crossed with the preregistered arm, replicate, order, and cache conditions: a census of this named synthetic pack, not a probability sample. The v1 protocol caps four pair replicates per fixture/arm and names the four cold/warm × baseline-first/candidate-first cells; if a scheduled cell is missing or incomplete, preserve it rather than silently substituting another condition. Close only when each scheduled arm is terminal or its declared budget/deadline makes it censored. The primary descriptive outcome is exact rubric-field agreement against the separate curated synthetic targets. For a valid matched replicate, the paired effect is candidate minus baseline agreement proportion. If later repeated runs are analyzed, compute within-fixture repeat variability separately (range and sample standard deviation only for at least two completed repeats) and summarize at the fixture cluster; do not pool unlike fields or treat retries as independent observations. The three-case pack is a smoke fixture set: no population generalization, causal claim, or inferential interval is supported. Uncertainty is therefore explicitly unavailable, not zero. A broader comparison must preregister cohort admission/closure, missing/aborted disposition, sampling, effect interpretation, independent cluster, uncertainty method, and multiplicity before running.

## Budgets and execution boundary

The v1 protocol records three fixture cases per cohort arm, up to four paired replicate records per fixture/arm, and three attempts per arm run. Each arm run covers one fixture. It also caps inputs at 4,000 characters per field and 12,000 per case, responses at 65,536 bytes and 256 generated tokens per attempt, and each request at 120 seconds. `automatic_execution`, model calls, automatic retries, and automatic real-content selection are disabled. These are declarations for a separately authorized future runner; this work adds no runner and makes no calls to local or paid models, telemetry, services, or the existing evaluator.

Before any future execution that processes real evidence or uses an optional judge, require explicit operator-selected scope, an independently reviewed privacy/security boundary, validated local-only routing, and a separately authorized runner. The fixture identities and pair helper do not authorize production-data selection, dashboard use, source adapters, historical backfill, or changes to the metric registry. Synthetic agreement is not task acceptance and must not advance X1/X6 or any other metric delivery state by itself.
