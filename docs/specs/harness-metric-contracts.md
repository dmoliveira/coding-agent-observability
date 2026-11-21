# Harness metric contract and identity model

**Contract version:** `aiobs.metric-contract.v1.2`
**Plan:** `epic_133`, pickup map `memory_415`, shared planning contract `memory_398`
**State:** schema, component registry, and cohort/statistical contracts are specified; metric producers remain individually gated by the readiness and delivery state in `harness-metric-contracts.json`.

This contract makes a dashboard value auditable. It does not claim that every listed source is installed, every event is captured, or every metric is currently available. The registry contains the 82 approved family IDs and their primary `oc` owner IDs; composite families are decomposed into 268 named, single-unit components without changing ownership. Component count is versioned detail, not a new stable family catalog.

## Required metric instance

Every emitted or computed metric instance must carry these fields, even if some values are `null` with a reason:

| Field | Requirement |
| --- | --- |
| `schema_version`, `metric_id`, `definition_version`, `formula` | Stable component ID, versioned formula and definition; never reuse an ID for changed semantics. |
| `population`, `eligibility`, `exclusions` | Explicit time basis, cohort state/closure, entry and follow-up rules, truncation/censoring, sampling design/probability, pooling method and late-data policy. Snapshot selection is not lifecycle evidence. |
| `value`, `unit`, `aggregation`, `sample_size` | One atomic measure and declared aggregation. `value` may be null only with an availability reason; null sample size needs a reason. Percentiles name their finite-sample quantile method. Do not combine sibling components. |
| `numerator`, `denominator` | Required for rates/fractions; report both and sample size. A zero or undefined denominator yields unavailable, never zero or success. Paired differences use their own aggregation and are not rates. |
| `evidence_class` | `observed`, `derived`, `attested`, `judged`, or `unavailable`; do not collapse classes. |
| `source`, `source_version`, `producer_version`, `transform_version` | Exact source and transformation provenance. A null version needs a reason; readiness is a proposal, not evidence of installed capture. |
| `readiness`, `delivery_state` | Readiness is `N`, `R`, `I`, `E`, or a declared combination. Delivery is one of `planned`, `source-supported`, `implemented`, `validated`, or `explicitly-unavailable-with-reason`. |
| `coverage` | Separate eligible, reported, missing, excluded, ambiguous, pending/censored, left-truncated, unresolved and unavailable counts, plus whether the population is complete and reason codes. Counts may be null when unknown; missing is never zero. |
| `time`, `counter_epoch` | Exact timestamp basis, UTC window (`from` inclusive, `to` exclusive), timezone, numeric clock uncertainty or reason, late-data watermark/policy, and `computed_at`. Counter resets/restarts are scoped to an explicit epoch; non-counter measures explain why epoch is not applicable. |
| `uncertainty` | Explicit reported/not-applicable/small-sample/no-target/unavailable status; method, independent cluster count, cluster unit, effect interpretation, comparison family/multiplicity method and interval where justified. Never invent precision. |
| `dimensions`, `interpretation`, `drilldown` | Only evidenced dimensions and authorized opaque references. Explain limits and keep private evidence opt-in. |

The JSON registry includes JSON Schemas for metric instances and per-component cohort/statistical contracts, named atomic component IDs, formulas, numerator/denominator semantics, eligibility, exclusions, timestamp basis, definition version, delivery evidence, chart designs, readiness and ownership. For future/unsupported sources, `timestamp_basis_status: requires_source_probe` is explicit; the producer must replace that proposal with a verified field before emitting data. Runtime producers must supply the instance fields above; the registry itself is not evidence that those values have been collected.

## Evidence and readiness

- **Observed:** directly recorded source fields or events. Preserve the producer's meaning, including missing fields and explicit zero.
- **Derived:** deterministic computation over observed records. Include its source IDs, transform version, cohort, denominator, exclusions and ambiguity.
- **Attested:** an explicit human/agent/automation claim. It is not independently verified by its presence.
- **Judged:** a versioned human/model assessment with rubric, assessor, selected evidence, uncertainty and abstention. It is not an observed fact.
- **Unavailable:** unsupported, inaccessible, uncaptured, invalid, truncated, ambiguous, pending, censored or not yet measured. Include the specific reason; never substitute zero.

Readiness meanings: `N` existing native Langfuse data; `R` existing structural report data; `I` new instrumentation/adapter required; `E` independent assessment or controlled experiment required. A combined value means the component has multiple required inputs, not that it is partly available. A family can contain components with different readiness; component rows retain the family's declared readiness until an owner validates a more precise source.

## Identity and relationship rules

| Identity | Evidence rule |
| --- | --- |
| Source event / observation | Use the exact source-issued ID with source namespace/version. Deduplicate only by that identity. Conflicting duplicates fail closed. |
| Request attempt | One source-issued attempt ID. Retries are separate attempts and may link to one logical operation only when the producer provides that relation. |
| Logical operation | Explicit source grouping or an exact authored binding. Never infer it from adjacent timestamps, matching names, similar arguments or a wrapper count. |
| Nested envelope / child operation | Keep the wrapper and each child distinct. Link only via source parent/child IDs. An outer Code Mode or parallel call does not prove which nested operations ran. |
| Session / turn / busy period | Keep source session, observed Langfuse turn, and runtime busy-period identities separate. A terminal busy-period event may cover coalesced prompts; never attach it to the latest prompt by time. |
| Task / artifact revision / check | Require an explicit opaque task binding, immutable artifact revision identity, and check record linked to that revision. Snapshot status and command presence are claims, not verified outcomes. Do not join by title, path, timestamp or text similarity. |
| Agent / delegation | Use the actual source role and explicit parent/child session and handoff IDs. Dispatch, acknowledgement, child start, terminal state and accepted/useful consumption are separate facts. |
| Producer epoch / counter | Scope monotonic counters to an explicit producer/process epoch. Reset, restart, wrap, drop or missing upstream accounting makes completeness unavailable for the affected interval. |

IDs in exported views must be opaque and project-scoped. If source IDs require pseudonymization, use a private keyed mapping with controlled rotation; unsalted hashes of prompts, arguments, paths, tool output or memory are not an acceptable identity scheme. Never export those payloads to create a join key.

## Current source limitations

The version-specific evidence and handling rules are enumerated in `unsupported_correlations` in the JSON registry. In particular, OpenCode terminal execution events describe session busy periods that may coalesce prompts and cannot be joined to a turn/task; the default event bus does not guarantee replay or completeness; Langfuse native metrics cannot generally join parent/child observations; and project-wide tool observations are not necessarily OpenCode-originated. Auxiliary model-call purpose, nested wrapper children, reported-versus-inferred cost, and compaction start-to-terminal pairing remain unknown or unavailable without new source evidence. Existing structural IDs are legacy deterministic digests without project-keying and must not be treated as v1.2-conformant.

## Execution state and result are separate

Execution state: `queued`, `running`, `terminal`, `unknown`.
Terminal result: `acceptable`, `expected-negative`, `unexpected-error`, `timeout`, `canceled`, `policy-blocked`, `unknown`.

An outcome classifier needs a source-backed, versioned producer/task contract. Empty search results, correct denials, and tool-specific negative responses are not failures by default. A non-error level, successful drain, acknowledgement, or lack of an explicit failure is not proof of acceptance or task success. Keep unresolved, interrupted, censored, expected-negative and policy-blocked observations distinct.

## Dashboard semantics

Keep reliability, effectiveness, efficiency and measurement coverage as separate views, with one overview and focused drilldowns. Do not create a universal AI/harness health score. A health state requires a configured objective, eligible sample and adequate evidence coverage; `no-target`, `unknown`, and measured zero are distinct, and low sample size must not hide explicit severe failures.

## Cohorts, time, and aggregation

- Every component declares an admission, observation-start, completion, event-time, snapshot, counter-window, process-sample or frozen-manifest basis. Never combine measures with different bases without an explicit transformation contract.
- Define lifecycle eligibility at admission/acceptance, before result filtering. Freeze the cohort entry window; close only after the required terminal/follow-up horizon and source event-time watermark (including declared maximum lateness and clock uncertainty) pass. A completed query or a snapshot alone does not close a lifecycle cohort.
- Exclude and count left-truncated identities whose entry predates the observable cohort or lacks required history. Keep running/open intervals right-censored with last observation and count; completed-only duration percentiles are descriptive of completers, not the whole admitted cohort. A survival-adjusted estimate requires a named method and assumptions.
- Record source clock identity and numeric uncertainty, or an explicit unavailable reason. Prefer same-process monotonic clocks for durations. Cross-source event ordering is ambiguous unless timestamp separation exceeds the combined uncertainty bounds; do not repair it with nearest-time matching. Events at uncertain window boundaries remain separately counted.
- Watermarks are source-native. A late arrival creates a new versioned result with a new computation time; never silently rewrite a prior report or automatically backfill history. If no watermark or lateness bound exists, keep the population open/incomplete and completeness ratios unavailable.
- For C3, separately count eligible producer receipt, queue admission, queue drop, unique emission, acknowledgement and independently confirmed ingestion. Each stage ratio requires independent counters over compatible identities/windows and unchanged producer epochs. A missing upstream counter, reset, incompatible boundary or unpassed watermark makes the dependent ratio unavailable; a send/drain is not an acknowledgement and an acknowledgement is not confirmed ingestion.
- T3 classification coverage uses all eligible admitted attempts in the closed cohort as denominator, including unresolved attempts. Pending, unknown, canceled, policy-blocked and unresolved states stay distinct. T4's unexpected-error/timeout rate uses only classified technical outcomes and is displayed with T3 coverage; it is not a success rate over observed survivors.
- Q6 includes only accepted tasks whose explicitly declared reopen horizon has matured past the source watermark and lateness allowance. Immature or censored follow-up is not a non-reopen. M3 fully labeled queries with no relevant memory have a separate explicit-abstention measure; precision/recall with a zero relevant denominator are unavailable, not zero.
- X4 full-cohort resource yield is unavailable when any eligible run lacks complete, compatible resource evidence. A known partial subtotal must not be ranked or labeled as full-cohort cost/yield.
- Report sampling design and inclusion probabilities. Show strata separately first. Known positive inclusion probabilities may support an explicitly named weighted estimator; unknown-probability, convenience or hand-selected samples are descriptive only and cannot be extrapolated to a production population. Label pooled raw summaries as unweighted.
- Repeated observations are not independent: cluster uncertainty at task and preserve run nesting. Report effect size, independent cluster count, interval method and inference scope. Mark small-sample when the chosen method's preregistered minimum is unmet; do not invent a universal minimum or precision. Predeclare primary outcomes, comparison family and multiplicity correction; otherwise label comparisons exploratory.
- Synthetic hand-calculated examples in the registry define the expected behavior for maturity, censored durations, counter epochs, weighting and zero denominators; they do not assert runtime availability.
- Attribute every leaf once. Parent-inclusive and descendant-inclusive usage are separate views and must not be added. Provider token fields may overlap; do not synthesize `total` from other fields.
- A fraction with no eligible denominator is unavailable. Empty/missing source maps are not zero observations. Explicit reported zero remains a reported value.
- For paired comparisons, publish pair count, unresolved/missing pairs, effect size and task/run-clustered uncertainty. A paired difference is not causal unless assignment and order controls support that claim.

## Version and privacy boundary

Version 1.2 adds required cohort, watermark, clock-uncertainty, sampling, coverage, quantile and clustered-inference fields. This is a contract/schema revision, not a runtime report migration: no producer or stored report is changed here. Existing v1.1 reports are not v1.2-conformant until a producer explicitly supplies the new evidence and passes the component's validation gates.

Compatibility claims must name the tested OpenCode, Langfuse/plugin, API and producer versions. The current repository documents OpenCode `2.2.5`, `@langfuse/opencode-observability-plugin` `0.7.0`, Langfuse `4.82.0`, and Langfuse v2 observations/metrics interfaces; any other deployment remains unverified until isolated contract tests pass.

Never add prompts, reasoning, tool arguments/results, command lines, error text, paths, memory contents, secrets or raw evidence to metric records. Names and IDs may themselves be sensitive. Keep explicitly selected private drilldowns separate from default aggregates. Do not backfill history, schedule processing, invoke private evaluations, change credentials/retention, or restart a live agent as part of contract validation.

## Delivery status

The shared ledger states are `planned`, `source-supported`, `implemented`, `validated`, and `explicitly-unavailable-with-reason`. Existing pre-v1.1 outputs are marked `implemented` with `contract_conformance: legacy`; they are not falsely labeled validated. `validated` requires the component to emit the v1.2 instance schema and pass cohort, missingness, privacy, and negative-path checks. A family only carries per-state component counts; family rollups are not metric instances. Unsupported components retain a specific unavailable reason. Task completion or a rendered chart alone cannot advance this ledger.
