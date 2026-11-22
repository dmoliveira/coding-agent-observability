# Private report-view feasibility and rendering decision

**Plan:** `task_6277`, brief `memory_407`, metric contract `memory_398`, plan revision 1.1

**Decision:** use Langfuse's authenticated UI for supported source aggregates and trace/observation drill-through; keep computed views in the existing bounded operator-controlled CLI output. Do not add a custom browser renderer in this slice.

**Evidence boundary:** repository code/tests and published Langfuse documentation plus the pinned Langfuse v4.82.0 source contract. No live API, browser session, credential, report, or trace content was accessed.

## Decision and rationale

The current product has two honest surfaces, not one unified viewer:

1. **Native Langfuse UI** is the private visual surface for source-native aggregates and explicit click-through to the corresponding observation/trace. Its authorization boundary is the Langfuse project and its membership; project membership is not repository-, location-, or task-level isolation.
2. **Bounded CLI JSON** is the operator-controlled, machine-readable surface for computed coverage, ancestry, UTC buckets, and searched observation pages. It is not automatically private: stdout redirection, logs, terminal capture, and copy/paste can persist it.

No repository HTTP endpoint, HTML report renderer, viewer authorization layer, or browser client currently exists. Do not claim that JSON is a rendered private view, expose the in-process query adapter over a network, or approximate unsupported relationships with native charts. Until an authenticated renderer receives its own implementation and threat-review task, derived visual drilldowns are explicitly **unavailable**; the native dashboards and CLI report remain usable without them.

## Capability comparison

| Need | Native Langfuse UI | Existing bounded report/query | Decision |
| --- | --- | --- | --- |
| Counts, latency percentiles, token/cost sums, time trends, and low-cardinality categories | Supported for declared observations metrics, dimensions, filters, and chart types; existing code owns three tested dashboards. | Report has bounded aggregates and coverage; query returns observation rows. | Keep native dashboards for direct source aggregates. Preserve each widget's source and interpretation caveat. |
| Observation table search and trace/observation inspection | Langfuse has an authenticated observations table and native trace/observation navigation. Clicking through can reveal raw trace content. | `agent_query.py` provides bounded server-side name/model substring pagination; IDs are omitted by default and the opaque cursor stays in the process. | Use native UI for manual source-row inspection; use query pages for bounded CLI/API consumers only. |
| Complete tool/model inventory | Native ranked bars and pivots are top-N/capped, not a complete inventory. | Cursor pagination can continue until the current source cursor ends, with explicit page/row/byte/time budgets and no total count. | Do not label a chart's top-N as complete. Preserve `query_complete`, continuation, and source-completeness caveats. |
| Linkage, missingness, per-turn attribution, UTC day/week/model summaries | Native widgets cannot express the report's exact parent/child joins or its coverage denominators. A sum/empty native chart is not a missingness report. | The bounded structural report carries derived linkage, reported/missing fields, exclusions, and UTC buckets. | Use report JSON as the authoritative derived evidence; do not calculate a different join in the browser. |
| Heatmap, per-turn timeline, or parent/child graph | The pinned v4.82.0 dashboard widget enum contains line/area/bar time series, horizontal/vertical bars, pie, number, histogram, and pivot table; it has no heatmap, per-record timeline, or graph type. Time series are aggregates, not a per-turn event timeline. | The report can emit optional identifier-backed timelines; the query adapter emits rows, not a joined graph or visual model. | These visual forms remain unavailable until a separately scoped renderer can preserve the source joins, uncertainty, and limits. |
| Shared filters and time window | Native controls apply only as supported by the native widget/query; existing code defines fixed widget filters and an empty dashboard-level filter set. | Report/query windows are from-inclusive/to-exclusive UTC; query search is a tool-name or model substring. The query adapter does not implement every native filter. | Show the exact filters applied per surface. Share the UTC window; never imply environment/level/session filters propagated where they were not applied. |
| Authorization | Langfuse project membership and project credentials gate the UI and API. | The CLI uses private local configuration; its output can leave the checkout through ordinary shell capture. | Keep credentials server/CLI-side. Neither project membership nor loopback access is repository/user isolation. |

The code-managed dashboard path calls Langfuse `/api/public/unstable` dashboard/widget management endpoints. The current Langfuse UI remains the supported human editor; any continued programmatic management is version-specific and must retain the repository's pinned contract tests. Do not make a new renderer depend on this unstable resource-management API merely to display a report.

## View and interaction contract

Any future report-backed view should preserve these properties in its view model and UI:

- **Shared window:** exact UTC `from` inclusive / `to` exclusive values and `computed_at`. A source's current end-of-cursor is not proof of complete, on-time, or loss-free ingestion.
- **Provenance banner:** schema version; source and tested source/API version; producer/transform version where applicable; `observed` versus `derived` evidence; project/scope label; window and timezone; `computed_at`; coverage/exclusions and freshness caveats.
- **Applied filters:** list the filters actually applied to this panel. Model/tool search is a `contains` query, not an exact match. Native-only filters stay native-only until the report/query supports and tests them; unsupported filters are not silently dropped.
- **Cardinality:** label ranked bars and pivots as top-N, show the configured cap and omitted-tail status when available, and never represent them as complete inventories. Query pages report continuation and `total_matches: null` because the cursor endpoint supplies no total count.
- **Availability states:** distinguish loading, a completed empty result, an empty page with continuation, unavailable/missing fields, incomplete/budget-exhausted results, and request errors. Never substitute zero for missing or an error for an empty result. A failed query requires a new bounded query, not a retry that hides exhaustion.
- **Accessible presentation:** give every chart a text title/description and a table alternative; use semantic table headers/captions; expose loading/error/completion updates to assistive technology; support keyboard focus; do not encode evidence states by color alone. Missing/unknown and measured zero need explicit text labels.
- **Scoped drilldown:** preserve only filters supported by the target; use exact opaque source IDs and project scope, never name/time matching. Keep trace/session/observation IDs opt-in and warn that a Langfuse trace link may reveal raw content. Do not put credentials or opaque continuation cursors in browser URLs.

## Future renderer gate (not implemented here)

If a later task requires visual coverage tables, heatmaps, timelines, or graphs, the smallest plausible extension is a narrowly scoped report-backed renderer that consumes the existing bounded report/query view models. Before implementation or exposure it must separately settle:

1. A user authentication and project-authorization model. A loopback bind, owner-only configuration file, or Langfuse project key alone does not authenticate a browser user or isolate repositories.
2. A server-side credential boundary. No project key or raw trace payload in a client bundle, static asset, URL, public site, or browser storage.
3. Context-specific HTML/attribute/URL encoding for untrusted tool/model names, strict CSP, and no template execution. JSON serialization alone is not HTML safety.
4. Explicit lifecycle and storage: no default persistent report cache, background refresh, schedule, history, or public export; any deliberate file output needs owner-only permissions and retention guidance.
5. Reuse of the declared request/page/row/response/output/wall-time/concurrency budgets and fail-closed empty/continuation/exhaustion behavior.
6. Synthetic tests for XSS/template strings, authorization across projects, filter propagation, empty versus incomplete/error, missing versus zero, top-N disclosure, keyboard/screen-reader states, and public-site exclusion.

If an acceptable user-auth model cannot be proven, retain the current Langfuse UI plus CLI JSON path and leave the derived visual capability unavailable. This feasibility result does not authorize a service, browser session, image, live query, or content processing.

## Sources checked

- Repository implementation: `scripts/integrations/agent_dashboard_definition.py`, `agent_dashboard.py`, `agent_report.py`, and `agent_query.py`.
- Repository tests: `tests/test_agent_observability.py` checks the Langfuse v4.82.0 chart enum, supported measures, row caps, layout, query pagination, and privacy exclusions.
- Repository boundaries: `docs/specs/harness-telemetry-threat-model.md`, `docs/specs/harness-metric-contracts.md`, `docs/security.md`, and `docs/agent-observability.md`.
- Langfuse [Custom Dashboards](https://langfuse.com/docs/metrics/features/custom-dashboards), [Chart any table](https://langfuse.com/docs/observability/features/events-table-charts), and [Metrics API](https://langfuse.com/docs/metrics/features/metrics-api) documentation.
- Pinned [Langfuse v4.82.0 dashboard widget chart types](https://github.com/langfuse/langfuse/blob/v4.82.0/web/src/features/widgets/lib/dashboardWidgetChartTypes.ts).

The repository version contract and published documentation were inspected read-only. The currently deployed Langfuse instance/version and browser authorization behavior were not tested; no live telemetry was queried.
