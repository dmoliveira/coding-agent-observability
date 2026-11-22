# Harness capability and instrumentation coverage matrix

**Plan:** `task_6276`, brief `memory_370`, contract `memory_398`, pickup map `memory_415`
**Contract:** `aiobs.metric-contract.v1.2`; families C1, C2 and C10
**State:** source-backed inventory specification only; no capability collector, dashboard or viewer is implemented here
**Compatibility reviewed:** repository baseline OpenCode `2.2.5`, `@langfuse/opencode-observability-plugin` `0.7.0`, Langfuse `4.82.0` (Langfuse v2 observations/metrics interfaces). Other versions are unverified.
**Synthetic source probes:** on 2026-11-22, all three repository smoke targets passed with OpenCode `2.2.5`, the integrity-prepared/lockfile-pinned `0.7.0` adapter, a fake local model and loopback collectors. These probes do not use a Langfuse `4.82.0` server or live telemetry; that server/API version remains a separately pinned contract, not a result of these probes.

This document defines how to inventory what an OpenCode request can use and what the repository's instrumentation can actually observe. It does not claim that the local runtime's configuration, plugins, MCP servers, permissions, models, or tools were enumerated. No live capability list, trace, prompt, tool payload or private content was read to produce this specification. The local CLI's help/version surface was checked; active plugin, MCP and model configuration was deliberately not queried.

Read this with [`harness-metric-contracts.md`](harness-metric-contracts.md), [`opencode.md`](../opencode.md), and [`agent-observability.md`](../agent-observability.md). The metric registry currently marks all seven C1, three C2 and four C10 components `planned`; code or test presence does not make them emitted or validated.

## Inventory boundary and snapshot model

Inventory the **effective capability set for a specific location and request**, not just declared configuration or tools that happened to run. A future producer should preserve two related snapshots:

1. **Location snapshot:** effective configuration revision and precedence, loaded plugin/agent/tool registries, commands and skills, MCP server/catalog generation, workspace identity, and source/runtime versions.
2. **Request snapshot:** source-issued request/attempt identity, the location snapshot it used, active agent, effective tool set, per-capability permission decision, selected and actually used model route, and time/availability evidence. If a catalog or permission refresh changes the effective surface, create a new versioned snapshot; do not silently carry forward the previous state.

Each snapshot needs an opaque snapshot ID, explicit `as_of`, scope, source namespace/version, producer version, and a completeness/pagination result. Bind capabilities by a stable source-issued identity (or a private, project-scoped mapping), not a display name, path, timestamp, prompt, or content hash. Keep workspace/location scope explicit: the global tracing adapter keeps some location ownership in memory but sends to one configured Langfuse destination, so locations sharing a project are pooled there, not isolated tenants.

Do not include prompt/context text, command lines, tool arguments/results, memory, raw paths, credentials or unrestricted metadata. Names and IDs can themselves be sensitive and hostile; validate and encode them for their eventual display context. Keep the detailed matrix in a private operator-selected surface if one is implemented; this spec does not authorize a browser viewer.

### Capability state semantics

These are independent, evidenced state dimensions, not stages in a guaranteed pipeline. A capability can be tested while disabled, instrumented but never observed, or observed in the window but disabled in the current snapshot. Permission is a separate per-request attribute; an `ask` or `deny` decision is not synonymous with unregistered or unavailable.

| State / family component | Required evidence for a positive state | Do not infer it from |
| --- | --- | --- |
| **Configured — `C1.configured`** | Capability is present in the effective, precedence-resolved configuration for this location/request snapshot. Record configuration source/revision without secrets. | A file existing somewhere, a default, or a similarly named entry in a different scope. |
| **Registered — `C1.registered`** | Runtime registry/resolver confirms the capability identity is loaded for the applicable source/version and scope. | A declaration, package installation, plugin file, or an observation from another runtime. |
| **Enabled — `C1.enabled`** | Effective runtime feature/policy says the capability is enabled for this scope. | Registration, availability, a permission grant in another request, or previous use. |
| **Available — `C1.available`** | A source-native snapshot confirms the implementation/endpoint/model route is resolvable and ready at `as_of`. Record provider health and permission separately. | Configuration alone, an `ask`/`allow` rule, successful use in the past, or no recent errors. |
| **Instrumented — `C1.instrumented`** | A versioned producer/hook contract covers this exact capability class and execution path, with its scope and known loss modes declared. | An installed plugin, a similar wrapper hook, or an event from another path. |
| **Tested — `C1.tested`** | A passing contract test is matched to the capability identity/schema and relevant source, runtime, adapter and test versions. | A green repository suite that does not exercise this capability/path, a live observation, or an unversioned manual check. |
| **Observed — `C1.observed`** | At least one exact source-issued invocation/event identity appears in the declared observation window and scope. Preserve the window and source. | Availability, a tool name in config, a successful wrapper envelope, or absence of errors. |

For every state, missing, invalid, conflicting, ambiguous, inaccessible, stale, partially paginated and unsupported evidence remains a separate coverage reason. Unknown is not false, and no observation is not evidence of “unused.” `C1.observed` is windowed evidence ending at the snapshot `as_of`; it does not prove the capability remains enabled or available now.

## C1, C2 and C10 calculation contracts

### C1 — separate state counts

For each state, count distinct stable capability identities within one declared effective location/request snapshot and its stated observation window. A capability identity is namespaced by source and source version, capability kind/namespace, and applicable scope. Report each component separately; the counts overlap and must not be added into a total. A zero is valid only when the relevant state source is complete: the catalog/registry for snapshot states, the event window for `observed`, and the versioned manifests for instrumentation/tests. Otherwise the count is unavailable with explicit coverage reasons.

Do not collapse tool registration, permission, availability or observation into one “active” flag. Keep permission decision (`allow`, `ask`, `deny`, `pending`, `not_applicable`, `unknown`), policy source/version and decision time as separate fields. Model catalog membership and actual model use are also different facts.

### C2 — version-matched contract coverage (`C2.contract_coverage`)

For a declared scope and version set:

```text
contract_coverage = version-matched contract-tested eligible capabilities
                    / eligible discovered capabilities
```

The denominator is the fully discovered set for which the named source/version exposes a contract in scope. Explicitly unsupported capabilities are reported separately (`C2.unsupported`) and are not counted as untested eligible capabilities. Discovered-but-eligible entries with no passing exact-version contract are `C2.untested`. Unknown, invalid or unenumerated entries are not silently omitted: show them in coverage, and make the fraction unavailable if they prevent a complete eligible denominator. A zero denominator or incomplete inventory is unavailable, not zero percent or full coverage. Runtime-disabled capabilities can still be contract-testable; disabled is not the same as unsupported.

Tests should be synthetic and exercise the relevant path boundary. Contract-test coverage does not mean the capability ran in production, was available for every request, or produced a correct task outcome.

### C10 — separate attribution completeness

Compute one numerator/denominator per dimension, using a source-defined eligible-run cohort and stable source-issued run identity. The denominator must not be guessed from observation row count, session count, busy-period count, turn count, task title, or dashboard filters. Preserve unknown, invalid, conflicting and not-applicable counts beside each ratio. A zero/incomplete denominator or absent run identity makes that dimension unavailable.

| Component | “Known” requires | Must not substitute |
| --- | --- | --- |
| `C10.repository` | Explicit, authorized, stable repository/workspace identity bound to the eligible run. A private opaque ID is preferred. | Raw path, Git remote/name, current working directory, inferred location, or a display label. |
| `C10.agent` | Source-reported agent actually used for the run, with namespace/version and explicit handling of multiple agents. | Configured default agent, prompt wording, child-agent tool label, or a guessed role. |
| `C10.actual_model` | Actual provider/model reported for every eligible model invocation in the run; define dimension-specific eligibility and count no-model runs as not applicable when appropriate. | Requested/default model, provider availability, an alias, or another generation's model. |
| `C10.harness_version` | Runtime-reported harness version bound to the eligible run. | Repository branch, package version, current CLI version, or a version inferred from a timestamp. |

An explicitly known value in one dimension does not fill another. If a run makes multiple model calls, attribution is known only when the actual model is present for all calls covered by the dimension's rule; retain the model set or relevant source identities. The repository currently has no universal eligible-run ledger or source-backed repository/run binding, so C10 is not currently computable from the structural report.

## Source-backed execution-path inventory

“Potentially captured” below means a source-backed path exists when its plugin and hooks are active; it is not proof of complete reach, availability, or delivery to Langfuse. The global plugin intentionally records rich tracing content. Structural capture is a separate optional, allowlisted event stream. Neither currently emits the effective capability snapshots defined above.

| Capability or execution path | What the current sources cover | Inventory/attribution boundary |
| --- | --- | --- |
| **Direct session, generation and tool execution** | The pinned Langfuse integration hooks session prompt/context and tool `execute.before`/`execute.after`; its event path records session/generation/tool observations. The structural report consumes selected emitted observation fields and ancestry. | Can evidence an observed operation if its source IDs arrive. It does not enumerate configured/registered tools, effective permission rules, or the complete available tool set. Project-wide tool observations may come from other emitters. |
| **Tool wrappers / Code Mode** | The outer wrapper can appear as a tool observation when it crosses the instrumented tool hook. | An outer envelope does not prove which inner operations ran. Count inner calls only when each has its own source identity and explicit parent/child link; otherwise mark nested coverage opaque/unavailable. |
| **Parallel wrappers / fan-out** | Same conditional wrapper/tool-hook coverage as direct calls. Independently emitted child operations may be visible. | No guaranteed fan-out list, completion ordering, cancellation state or parent/child linkage for hidden children. Do not equate one wrapper span with its child count or sum a parent-inclusive value with descendants. |
| **MCP tools** | An MCP tool may be captured if OpenCode dispatches it through the instrumented tool execution hook. | No MCP server/catalog snapshot or refresh generation is exported by this integration. Dynamic tool additions/removals and authentication/availability changes need a new source snapshot; previous catalog state cannot be carried forward. |
| **MCP resources and prompts** | No explicit resource-list/read or prompt-catalog instrumentation is present in these adapters. | Unsupported by current instrumentation. Do not infer that resources/prompts are absent because no tool span exists. |
| **Shell and subprocesses** | A shell tool invocation may be represented by its outer tool observation; rich global tracing can include sensitive tool inputs/outputs. | Child processes, commands launched from the shell, process trees and their nested tool/API calls are not a separate guaranteed inventory. No subprocess should be run for this documentation task. |
| **Skills and slash commands** | Session prompt/context tracing may contain assembled context when the upstream plugin emits it. | No stable applied-skill/command catalog, resolution, enabled state or per-request invocation identity is emitted. Context text is not a safe inventory or proof that an item was applied. |
| **Child agents / delegation** | A generation may carry a source-reported agent/model label; separate child sessions may yield separate observations. | Dispatch, handoff, child lifecycle, parent-child relationship and useful/accepted consumption are not complete. Join only explicit source IDs; a child-agent tool name or matching timestamp is insufficient. |
| **Background work** | Events may be seen if the work uses a traced session and the relevant hooks remain connected. | No generic background-job identity, queue/admission, completion or cancellation ledger is provided. Disconnect, shutdown and best-effort capture can leave state unknown. |
| **Main loop, prompt turns and retries** | The official plugin records prompt/session, generation and tool activity; runtime execution events are busy-period events. Retry/generation details may appear when the source emits them. | Hidden orchestration steps and loop decisions are not a complete audit log. A busy period may coalesce prompts; a turn/session is not a task or eligible run. Missing events do not establish that the loop or retry did not occur. |
| **Permissions** | When the separate structural plugin is enabled, it records permission ask/reply categories with opaque IDs, timestamps and limited enumerated replies; it omits action/resource text. | This is not a snapshot of effective permissions or all allow/deny decisions. The policy can vary by request/location; missing asks/replies do not mean unrestricted access or no prompt. |
| **Models and model availability** | The integration can record actual provider/model information on emitted generation observations; the documented plugin does not promise every experimental or non-session model call is traced. | No complete available-model/provider-health catalog or request-time model-routing snapshot is exported. Requested model and actually invoked model remain distinct. Auxiliary/embedding/title/other non-session calls are unverified unless an exact source contract proves otherwise. |
| **Compaction** | Optional structural capture records compaction start/end/failure, limited reason categories and valid terminal token fields. | It does not capture summaries/content, guarantee start-to-terminal pairing, or measure compaction quality. Keep these events separate from prompt turns and generation usage. |
| **Session titles** | No title lifecycle is in the structural event allowlist or report contract. | Not covered by these instrumentation paths; the runtime capability itself is not classified. Do not use a title as a request, task or capability identity; it may contain sensitive user text. |
| **Repository/location and plugin state** | The global adapter keeps per-location session ownership in memory. The structural plugin filters events by its configured location/workspace. | These controls do not export a repository identity or create storage isolation. A shared Langfuse project can pool locations. Current plugin configuration, load/enable state and effective per-request capability state are not in the metric report. |

### Source and test evidence reviewed

- `integrations/opencode-global/prepare.js` and `integrations/opencode-global/index.js`: integrity-pinned `0.7.0` adapter patch, session/context and tool hooks, location-owned in-memory state, activation/disable boundaries, and generation recovery behavior.
- `integrations/opencode-events/index.js`: explicit execution/compaction/permission event allowlist, location/workspace filtering, permission/reason enumeration, and best-effort bounded export.
- `scripts/integrations/agent_report.py`: selected observation fields and derived trace/report views; it reads emitted observations, not live runtime registries.
- `docs/opencode.md` and `docs/agent-observability.md`: documented versions, plugin coverage, rich-trace disclosure, optional event limitations, and report/dashboard scope.
- `integrations/opencode-global/test/instrumentation.test.js`, `integrations/opencode-events/test/events.test.js`, and `tests/test_agent_observability.py`: synthetic instrumentation, event filtering and report semantics. These tests do not provide a complete runtime capability catalog or prove coverage of every wrapper/MCP/background path.

## Bounded synthetic source-probe results (2026-11-22)

The task-scoped probes used only temporary projects/configuration, synthetic markers, an OpenAI-compatible fake local model and loopback collectors. The OpenCode shared-server test used its own temporary server and exercised its local reload, compaction and permission APIs; nothing was sent to a production plugin, real model, Langfuse server or external telemetry destination. `opencode --version` returned `v2.2.5`; `npm ls @langfuse/opencode-observability-plugin --depth=0` returned `0.7.0`.

| Probe | Result and exact scope | Does not establish |
| --- | --- | --- |
| `make smoke-opencode-global` | **Pass.** Synthetic `read` call produced `opencode.turn`, `opencode.generation` and the direct `read` tool observation. The test checked the private loopback destination, poisoned environment isolation and opt-out on a new standalone process. | Nested tool behavior, MCP, parallel children, shell descendants, child agents, auxiliary calls or complete delivery/ingestion. |
| `make smoke-opencode-shared` | **Pass.** Two synthetic locations used one isolated OpenCode server; B was reloaded while A was in flight. The probe observed distinct session IDs, one turn/generation and structural execution start/success per session, recovered A's generation, continued after manual compaction, and captured a same-request permission ask/reply `once` pair. Compaction token fields and content-free permission export were checked. | Process-crash recovery, arbitrary reload races, parent/child delegation, permission rejection/cancellation behavior or task acceptance. |
| `make smoke-opencode-events` | **Pass.** Structural capture coexisted with tracing in a temporary config, emitted content-free execution start/success events and disconnected without disabling global tracing. | Every event type, source replay/completeness, failure counters or real-project routing. |
| In-memory `encodeEvent` assertion | **Pass.** Synthetic `permission.replied` with `reject` encoded as `aiobs.permission.replied.reject`; a synthetic user interruption encoded as `aiobs.execution.interrupted.user`; private marker fields were absent. | That the OpenCode runtime emits these exact events for every denial/cancellation path; this tested the adapter encoder only. |

### Capability-by-path result

“Verified” below is limited to the named synthetic path. “Conditional” is source-level support without a path-specific runtime probe. An untested or opaque path remains unavailable; it is not a zero or evidence of non-use.

| Path / identity | Bounded evidence | Result and remaining boundary |
| --- | --- | --- |
| Direct session, generation and tool call | Global smoke; pinned adapter hooks `session.prompt`, `session.context`, `tool.execute.before` and `tool.execute.after`; source IDs include session, message and tool-call IDs. | **Verified for one direct synthetic `read`.** This is a call-level observation, not an inventory of all configured tools or a general logical-operation ID. |
| Code Mode / nested tools | Adapter observes the OpenCode tool execution hook; no nested Code Mode fixture exists in the probes. | **Opaque/unverified.** An observed wrapper does not reveal inner calls. Count a child only when a source emits its own stable ID and explicit parent link. Add an invocation-boundary bridge/event contract before claiming nested coverage. |
| MCP tools, resources and prompts | No MCP-specific fixture or catalog snapshot is present. | **Conditional for a tool call only:** an MCP tool may be observed if OpenCode dispatches that invocation through the same tool hook; this has not been verified here. MCP catalog generations, resources/prompts, availability and hidden server-side work remain **unavailable**. Require source-issued invocation/catalog IDs and refresh events; do not infer from tool names. |
| Parallel wrappers / fan-out | No parallel or fan-out fixture. | **Opaque/unverified.** An outer envelope is not its children; ordering, cancellation and child linkage are not guaranteed. Require explicit child IDs, parent IDs and per-child terminal state. |
| Shell and subprocesses | No shell tool or child process was run by a synthetic OpenCode request. | **Outer tool only, conditional; descendants unavailable.** The hook has no process-tree contract. Do not infer commands/children from the rich tool payload. A future process/job producer needs explicit opaque job/parent IDs and start/terminal events, without command text. |
| Permission ask/reply and interruption | Shared-server smoke verified synthetic ask/reply `once`; encoder assertion covered `reject` and user interruption categories. Structural allowlist includes permission asked/replied and execution interrupted. | **Partially verified.** A reply category is not a snapshot of effective policy; a rejected permission is not interchangeable with canceled execution. Runtime denial/cancel variants and all reply paths remain unverified. Add isolated runtime cases with request IDs and explicit terminal reasons before promoting them. |
| Child launch/completion/follow-up | Pinned adapter reads source `session.parentID` from `session.get`/`session.created` and passes the parent relation to its trace SDK. | **Source identity exists conditionally; lifecycle is unverified.** No probe launches a child agent or checks completion/follow-up. Parent ID alone proves neither dispatch completeness nor accepted/consumed work. Add a fake child lifecycle with explicit launch, parent/child IDs and terminal/follow-up events. |
| Reload and cross-location session ownership | Shared-server smoke exercised two synthetic locations and reloaded B while A was in flight; both session IDs remained distinct and A's in-flight generation was recovered. | **Verified for this in-process reload scenario only.** It is not a process-restart/replay guarantee or repository isolation: both locations intentionally share one test destination. Cross-session joins require source `parentID`; never join by name/time. |
| Auxiliary/non-session model calls | The pinned adapter observes session prompt/context, session step events and tool execution; no auxiliary-call fixture or universal invocation event exists. | **Unavailable/unverified.** No complete provider/model catalog or evidence for title, embedding, background or other non-session calls. Require a source-issued invocation ID/kind and actual provider/model event; requested/default model is not a substitute. |

### Identity and collector-health limits

- **Logical operation:** tool `callID`, `messageID`, source event `id` and `sessionID` exist at their respective boundaries. No general ID joins a wrapper's hidden descendants, multiple tools into one task, or activity across sessions. Keep IDs in their source namespace; no timestamp/name reconstruction.
- **Busy period:** `session.execution.started/succeeded/failed/interrupted` events are session-scoped source events. A terminal can cover coalesced prompts; it is not a prompt-turn or task outcome. The smoke verifies one synthetic execution interval, not one-event-per-prompt completeness.
- **Compaction:** structural start/end events carry distinct source event IDs, session ID and timestamps. The adapter derives separate event trace IDs from each event ID; the inspected contract supplies no shared compaction-operation ID. The smoke saw one start/end pair, but adjacency or timestamps cannot safely correlate arbitrary pairs. Keep unpaired events incomplete.
- **Permission correlation:** the adapter derives a shared opaque trace ID only from the exact `(sessionID, permission request ID)` for ask/reply; other structural events use their event ID. This is source-scoped correlation, not anonymization or a policy snapshot.
- **C3 delivery stages:** fake collectors saw synthetic HTTP requests, which proves only that the test server received a request. The adapter's flush/send success is not a producer receipt/admission count, per-record acknowledgement, or independent destination-ingestion confirmation. No compatible counters, watermarks or producer epoch are exported; C3 ratios remain unavailable, not zero.
- **C8 freshness/disconnect:** source code retries a disconnected event stream and emits a generic warning; the global exporter may warn that observations were lost. Neither emits a heartbeat, successful-export timestamp, explicit disconnect interval or producer epoch. Execution timestamps are not collector heartbeats; C8 remains unavailable.
- **C9 queue/export health:** the structural plugin has an internal 128-item queue and drops excess work with a generic warning; its 8192-entry dedupe map is also internal state. The code exports no queue-depth/capacity gauge, per-record drop count/identity, export-failure counter or epoch. Synthetic unit tests cover some failure/overflow behavior, not a complete loss ledger; C9 remains unavailable.
- **Reset and loss:** a generic warning, successful local drain, plugin reload, or absent observation cannot establish how many eligible records were lost. No process epoch or independent counter reconciliation exists. Do not advance C3/C8/C9 delivery state based on these probes; their owning task and registry remain authoritative.

**Future instrumentation proposal (not implemented):** add a separately authorized, versioned structural producer at the relevant OpenCode invocation/lifecycle boundaries. It should emit allowlisted source IDs, explicit parent/child links, source event time, producer epoch, terminal/result category, and per-stage counters/watermarks/health samples. It must exclude prompts, commands, arguments/results and raw errors; avoid nearest-time joins; and have synthetic tests for missing pages, restarts, duplicate IDs, failures, denial/cancellation, nested calls and child follow-up before any metric becomes available.

## Synthetic contract cases

Use fixture identities and fake sources only. These cases define future acceptance behavior; listing them is not evidence that a collector or dashboard exists or that every attack/path test has already run.

| Case | Synthetic fixture | Required result |
| --- | --- | --- |
| **Complete overlapping states** | Four capability IDs; 3 configured, 3 registered, 2 enabled, 3 available, 2 instrumented, 2 tested and 1 observed. Two of three eligible discovered IDs have passing version-matched tests; one is explicitly unsupported. | Emit each C1 component independently (counts need not sum to four); C2 is `2/3`, untested is `1`, unsupported is `1`, all with the same declared scope/version. Never collapse to one status. |
| **Partial/inaccessible catalog** | Same fixture but pagination stops before the last page or one permission/provider source is unavailable. | Mark the snapshot incomplete and retain missing-source/page counts. Do not emit zero for unseen states or a partial C2 ratio as complete. |
| **Disabled vs permission variants** | Capability A is registered but disabled; B is enabled and available with permission `ask`; C is enabled but request permission is `deny`; D is available but not observed. | Preserve enabled/availability and permission independently. `ask`, `deny`, disabled and no observation remain distinguishable; no execution is required. |
| **Version mismatch** | Capability schema/runtime `v2`, contract test only for `v1`. | Do not count it as `C1.tested` or C2 numerator; classify as untested for the eligible v2 scope or explicitly unsupported if the source contract says v2 is unsupported. |
| **Wrapper, Code Mode and parallel nesting** | One wrapper source ID with two child IDs and explicit parent IDs; then a second fixture with only the wrapper envelope. | In the first, count each distinct capability/operation once and retain ancestry. In the second, mark child coverage unknown/opaque; do not manufacture child observations or attribute all wrapper time to each child. |
| **MCP refresh** | Snapshot `mcp-gen-10` has `tool_a`; refresh `mcp-gen-11` removes it and adds `tool_b`; a permission changes from `ask` to `deny`. | Produce two immutable as-of snapshots and tie each request to the generation actually used. Do not report stale `tool_a` as available or infer the permission transition from silence. |
| **MCP resource/prompt and unsupported path** | A source explicitly reports resource reads unsupported, while no resource event is observed. | Record explicit unsupported evidence separately from not-observed. If the source gives no support declaration, classify as unknown, not absent/unused. |
| **Shell and auxiliary work** | Outer `shell` tool event, a synthetic subprocess marker with no child event, and a non-session auxiliary model call with no producer identity. | Report only the outer observed envelope; leave child process and auxiliary call attribution unknown/unavailable. Do not execute commands or infer them from text. |
| **Child/background completion** | Parent request launches child session with explicit parent ID; child is still pending at snapshot `as_of`, and another fixture omits the parent link. | Keep pending/censored separate from terminal; do not join the unlinked child by time/name or claim completion/acceptance. |
| **Main loop, compaction and title** | Several prompt IDs coalesce into one busy-period terminal; compaction start is present but terminal is missing; a title-change event has no producer. | Keep busy-period evidence separate from per-prompt outcomes; show incomplete compaction pairing; title instrumentation remains unsupported/unknown, not proof that title capability is unused. |
| **C10 dimension missingness** | Four eligible synthetic run IDs: repository known `3/4`, agent `4/4`, actual-model eligible runs `2/3` known (one run had no model call and is not applicable), harness version known `3/4`. | Emit four independent fractions with numerator, denominator and missing/not-applicable reasons. A missing repository value cannot be filled from the path; a requested model cannot fill missing actual-model evidence. |
| **Location collision / hostile labels** | Two locations expose the same display label and local capability name; a synthetic label contains HTML/template characters. | Keep location/source namespaces distinct, treat labels as untrusted display strings, and do not merge using names. Never place credentials or raw paths in fixture output. |

## Validation and non-claims

For this documentation slice, validate with `git diff --check`, `make check`, `make site-build`, and a manual cross-check of C1/C2/C10 component IDs, formulas, source versions, path classifications and the synthetic cases against the contract registry and source files. The synthetic cases above are specification fixtures, not claimed execution results. Do not run destructive tools, real MCP calls, shell commands on user data, model calls, live capability discovery, private trace queries or real-content evaluation to “complete” this inventory.

Until a separately authorized implementation has a source-backed effective snapshot, complete scope/pagination, exact version-matched tests and privacy review, keep C1/C2/C10 `planned`/unavailable. Do not label an unobserved capability unused, infer task success, or use these families as an overall harness-quality score. Preserve reliability, effectiveness, efficiency and measurement coverage as separate views.
