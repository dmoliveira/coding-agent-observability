![AI Observatory — Private traces. Clearer agents.](assets/observatory-hero.png)

# 🔭 AI Observatory — Local-First Coding Agent Observability

![Local first](https://img.shields.io/badge/access-local%20first-117f78) ![Langfuse](https://img.shields.io/badge/Langfuse-4.82.0-6149aa) ![OpenCode](https://img.shields.io/badge/OpenCode-official%20plugin-252e46) ![License](https://img.shields.io/badge/license-MIT-blue)

AI Observatory is a local-first, self-hosted observability stack for coding agents. Trace OpenCode sessions, model generations, and tool calls with [Langfuse](https://langfuse.com/) and OpenTelemetry, using Docker Compose on macOS or Linux. Optionally reach the private dashboard from other devices on your Tailscale network.

OpenCode is the coding-agent integration currently supported by this repository; additional integrations are future work.

[Read the documentation](https://dmoliveira.github.io/coding-agent-observability/) · [Security and privacy](docs/security.md) · [Third-party notices](THIRD_PARTY_NOTICES.md)

**By default nothing is published to the LAN or tailnet.** Docker publishes the web and media ports on localhost only. Tailscale sharing is a separate, explicit step; it is not Tailscale Funnel or public internet access.

> Once enabled, traces can contain prompts, previous conversation context, reasoning, source code, tool inputs and outputs, and secrets. Only trace data you are allowed to store. See [security and privacy](docs/security.md).

## 🚀 Start the server

Requirements: macOS or Linux, Python 3.10+, Docker Engine **28+** with Docker Compose, Make, and free localhost ports `3000` and `9090`. The initial image downloads require internet access. On macOS, start Docker Desktop first.

```sh
git clone https://github.com/dmoliveira/coding-agent-observability.git
cd coding-agent-observability
make help
make doctor
make setup
make up
make verify
make open
```

`make setup` asks for an administrator password and creates a private `.env` with unique instance secrets. The default account is `admin@ai-observatory.local`; set `AI_OBSERVATORY_ADMIN_EMAIL` before setup to use a different address. Use a password of at least 16 letters, numbers, underscores or hyphens. Setup will not overwrite an existing configuration.

Sign in at <http://127.0.0.1:3000>. In the **Coding Agents** project, create a Langfuse API key under project settings; keep both the public and secret key for the next step. Signups are disabled by default.

```sh
make status              # container health
make logs                # may contain sensitive data
make down                # stop and remove containers, keep volumes
make up                  # start again with the same data
```

Keep `.env` private and backed up: it includes your administrator password and the keys needed to read stored data. Docker administrators and other users with sufficient host access can also access container data.

### Optional: Bitwarden administrator password

The [Bitwarden CLI](https://bitwarden.com/help/cli/) can supply the administrator
password during startup. Store the existing Langfuse login in Bitwarden first;
the original password is in `ADMIN_PASSWORD` in `.env` unless it was changed in
Langfuse. Do not print or paste passwords or session keys into an agent session.

In your own terminal, with shell tracing (`set -x`) disabled, authenticate and
unlock the CLI separately from the browser extension. These commands capture the
session key instead of printing it:

```sh
export BW_SESSION="$(bw login --raw)"  # first CLI login
# OR, if already logged in but locked:
export BW_SESSION="$(bw unlock --raw)"
```

Sync the CLI, then find the login's ID without displaying its password; replace
the search text with the name you used in Bitwarden. The command lists only
login item IDs and names:

```sh
bw sync
bw list items --search "AI Observatory" | python3 -c 'import json,sys; [print(i["id"], i["name"]) for i in json.load(sys.stdin) if i.get("type") == 1]'
```

Select the correct ID and start the stack in the same terminal:

```sh
export AI_OBSERVATORY_BITWARDEN_ITEM="<login-item-id>"
make up
```

The item password must have at least 16 letters, digits, underscores or hyphens,
matching the setup rules. If the CLI is missing, locked, times out, the ID is
invalid, or the password does not meet those rules, startup warns and uses the
existing `.env` value instead. On success it confirms the source without printing
the password. A different valid Bitwarden password is used with a warning; it
does **not** change an existing Langfuse login. Initialization only creates the
account if it does not already exist. Change an existing login in Langfuse itself,
not by editing Bitwarden or `.env`. Changing the startup value can cause Compose
to recreate the web container; its data volumes are preserved.

Keep `.env` private, unchanged and backed up: it remains required and still
contains plaintext secrets. The CLI session is not passed to Docker, but Docker
administrators can inspect container configuration. A new shell needs its own
unlocked CLI session and item selection; run `unset BW_SESSION` when finished.
You can save the non-secret item ID in your shell profile, but never save the
session key or master password there.

## 🧩 Trace OpenCode by default

Install OpenCode 2 and Node.js 22+ on the machine running OpenCode. From a terminal, run once:

```sh
make connect-opencode-global
```

Enter the **Coding Agents** project's public and secret Langfuse keys when prompted. The installer verifies them and saves them privately under `~/.config/ai-observatory/` (or `$XDG_CONFIG_HOME`). It then enables tracing for ordinary OpenCode sessions across projects without changing your existing OpenCode configuration. An already-running service can load the plugin immediately; a future request can include prior session context in its traces.

To start a new session without **this integration's** tracing:

```sh
AI_OBSERVATORY_DISABLED=1 opencode --standalone
```

The standalone flag is important: a normal client connects to the already-running shared service, whose environment it cannot change. Other plugins and telemetry exporters are independent and may still record data. Existing traces are not erased. See the [integration guide](docs/opencode.md) for coverage, limitations, and the older per-project launcher.

## 📊 Explore agent operations

```sh
make agent-dashboards  # measured Langfuse dashboards; prints their URLs
make agent-report      # structural, machine-readable JSON for the last seven days
```

The dashboards show recorded OpenCode prompt turns and model activity, alongside clearly labelled **project-wide** tool metrics. Optionally, `make connect-opencode-events` adds structural busy-period, compaction and permission events for future work; these are not per-task outcomes and can be lost. The report joins available observations to their nearest recorded OpenCode turn and reports missing coverage. A turn ending is not proof of task success, and tool charts can include other integrations. Tool/model names and optional trace IDs can be sensitive; see the [agent observability guide](docs/agent-observability.md) for interpretation, privacy, windows and future phases. Neither dashboard/report command runs an evaluator or reads observation input/output.

For an optional local-model task-assessment example, `make agent-eval-demo` judges three synthetic cases. Its labels are **unverified judgments**, not facts or reliable success decisions. The guide describes explicit, bounded evaluation of selected evidence; nothing is backfilled or scheduled automatically.

## 🧪 Evaluate with local LM Studio models

On **macOS with a local Docker Desktop or OrbStack engine**, install LM Studio 0.4.0 or newer and its `lms` CLI. Remote Docker engines are not supported. Downloaded models and their licenses are managed in LM Studio. This integration does not download models or start its server for you.

1. In LM Studio's Developer settings, run the API on **`127.0.0.1:1234`**. Keep **Serve on Local Network** and **LM Link** off. Enable **Require Authentication** and create a token for stronger protection. Enable **Just in Time Model Loading** to make downloaded models available on demand, or load the ones you need manually.
2. Start Langfuse, then connect and inspect the available models:

   ```sh
   make up
   make lmstudio-models       # list downloaded models, including embeddings
   make connect-lmstudio      # enable gateway and add available language models
   make sync-lmstudio-models  # add newly available language models later
   ```

   Connecting may briefly recreate only the Langfuse web and worker containers; it does not remove volumes or change `.env`. The command reuses the private global OpenCode project key only when it points to this local server; otherwise it prompts for a project-scoped Langfuse key in a terminal. If LM Studio authentication is enabled, it prompts for that token without echoing it. Alternatively, provide `AI_OBSERVATORY_LM_STUDIO_TOKEN` privately in the environment; do not put tokens in command history. The LM Studio token is stored encrypted by Langfuse, not by the script.
3. In **Project Settings → LLM Connections**, find the printed `ai-observatory-lmstudio-…` provider. In **Evaluators**, create an LLM-as-a-Judge evaluator and select that provider and a model. Test synthetic examples first and compare its scores with human-labeled cases before trusting them. A model's presence in the list is not proof of structured-output support or evaluation quality.

Sync is **add-only**: it never removes previously registered model IDs, changes the project default, or edits another connection. It adds only language-model IDs currently advertised by LM Studio's OpenAI API; embeddings are listed but not used as judges. If JIT is off, unloaded models may not be advertised. The script stops rather than overwrite its connection if it detects external edits. Langfuse's API has no conditional update, so avoid editing that connection in the UI while sync is running.

The opt-in adds a non-published gateway to the Compose network. Langfuse trusts only that gateway's hostname, and the gateway forwards only model-list and chat-completion requests to the fixed local LM Studio port. LM Studio authentication is off by default: without it, other local users and Docker workloads may access its API. The host-to-container connection uses local HTTP, not encryption. Models, prompts, trace inputs, and outputs may still be accessible to local administrators; LM Link or external integrations can move data off the Mac. See [security and privacy](docs/security.md).

The integration marker and connection-ownership state are private, ignored local files and are **not included in the existing backup archive**. After restoring to a fresh checkout, run `make connect-lmstudio` again; it creates a new managed connection without overwriting the restored one. Do not delete the marker while the gateway is running. If LM Studio is stopped, evaluations may fail or pause; restart the server and check the evaluator before resuming.

If setup reports that an interrupted operation may have created a connection, inspect the managed provider in Langfuse before retrying. To start a fresh managed connection without altering the old one, stop other observatory commands, move `.ai-observatory.lmstudio-state.json` to a different private filename outside the repository, and run `make connect-lmstudio` again. Existing evaluators continue to refer to the old connection until you explicitly change them. If a container startup fails, follow the existing `make down` / `make up` recovery instructions; these commands preserve the named volumes.

### Example: classify spans on a dashboard

After connecting LM Studio, create an editable **AI Observatory — Span activity** dashboard and its local evaluator:

```sh
make span-dashboard       # create or update the managed resources
make span-dashboard-demo  # add three synthetic spans
```

The first command prints the dashboard URL. It creates four widgets, a categorical LLM-as-a-Judge evaluator and a rule that classifies **only** new spans with the example's generated name in the `ai-observatory-demo` environment. It does not backfill existing traces, change project defaults, or restart containers. The second command adds three labeled synthetic examples; each invocation adds three more. Results are asynchronous and may take a minute to appear. The widgets show spans received, spans classified, category breakdown and activity over time. Click the printed synthetic trace URL to inspect those examples.

The example sends the span **input** to `google/gemma-3-4b` through the managed LM Studio connection and assigns `inspect`, `change`, `test`, or `other`. It is a small demonstration, not a reliable quality or security judgment. LM Studio must be running and the model registered; change `MODEL` in `scripts/integrations/span_dashboard_definition.py` to use another compatible registered model. Review classification quality with your own labeled samples. The rule's filters, categories, prompt, widget definitions and demo examples live in that file; after editing them, run `make span-dashboard` again. Deliberately expanding the rule can cause future real span inputs to be sent to the model, including sensitive content; check LM Link, network access, sampling and data permissions first. A long input can also be slow or exceed the model's context window.

The commands use the existing local project credentials without printing them. They keep a private ignored `.ai-observatory.span-dashboard-state.json` to identify only resources created by this checkout. Reapplying never adopts resources by name and stops on detected UI edits instead of overwriting them; edit the code for managed changes. Langfuse's dashboard APIs are currently unstable and writes are not conditional, so avoid concurrent UI edits during apply. If an interrupted API request leaves an uncertain operation, inspect the specifically named example resources in Langfuse before recovery; preserve or move the private state file and reconcile any old demo rule rather than deleting unrelated resources. The state is not included in the existing backup archive; if it is lost or after a restore, reconnect LM Studio and inspect/disable any old example rule before recreating the dashboard. The demo refuses to send spans while other enabled evaluation rules could process them.

## 🔐 Share with another Tailscale machine

On the **server**, connect Tailscale, enable MagicDNS and HTTPS certificates in your tailnet, and review the tailnet's ACLs or grants for ports `15443` and `15444`. Start the stack, then explicitly enable sharing:

```sh
make up
make tailscale-on
```

The command prints the private HTTPS web address. Both dedicated ports must be unused; existing Serve and Funnel configuration is preserved, and concurrent changes are rejected. Enabling Tailscale HTTPS can publish the machine and tailnet DNS name in Certificate Transparency, even though the service is tailnet-only. Tailnet administrators and devices allowed by your grants can reach it; Tailscale does not replace Langfuse authentication.

On the **client machine**, join the same tailnet and clone the repository. It needs Python, Make, OpenCode 2 and Node.js 22+, but **not Docker or a local server**. Set the trusted server URL printed above and connect OpenCode once:

```sh
AI_OBSERVATORY_URL=https://your-server.your-tailnet.ts.net:15443 \
  make connect-opencode-global
```

Use the hostname and key from your own server; a `.ts.net` suffix alone does not prove that a host is private or trustworthy. This client-only path is intentionally simple; a dedicated installer can be added later.

To stop sharing, run `make tailscale-off` on the server. `make down` also attempts to remove this instance's routes and stops the stack. Never use `tailscale serve reset`: it can remove unrelated routes. If cleanup fails, inspect `tailscale serve status` and follow [the security guide](docs/security.md#tailscale-recovery).

## 📦 Backup and restore

Backups include PostgreSQL, ClickHouse, Redis queues, MinIO, and `.env` secrets. **They are unencrypted and contain credentials and all trace data. Encrypt them before copying or storing them elsewhere.**

1. Stop traced clients, flush their telemetry, and allow ingestion queues to drain.
2. On the server, stop the stack and create a backup:

   ```sh
   make down
   make backup
   make up                 # optional: restart after backup completes
   ```

3. Store the resulting `backups/*.tar.gz` securely. Only restore archives you trust.
4. To restore, use a **fresh checkout** of the same repository version on the same Docker architecture, without an `.env` or running instance in that checkout:

   ```sh
   make restore BACKUP=/absolute/path/to/backup.tar.gz
   make up
   make verify
   ```

Restore creates newly named volumes, preserves the original instance and archive, and defaults to localhost. If both instances are on the same machine, stop the original before starting the restored copy, or deliberately choose different local ports in its `.env` and matching URLs. Validate login and representative traces/media before relying on a restore. Checksums detect accidental corruption, not malicious modification. Do not run backup concurrently with another Docker command that starts or attaches the same volumes.

## 🛠️ Develop the documentation site

The [documentation site](https://dmoliveira.github.io/coding-agent-observability/) is built from this README and the Markdown guides in `docs/`. With Node.js 22 or newer, run `make site-build` and open `site/dist/index.html` locally. The generated site is ignored by Git and contains only an explicit allowlist of public files. `make check` includes the site tests; after building, `npm --prefix site run test:browser` optionally checks responsive layouts using a local Chrome installation.

## 🗺️ Later

Task outcome and context instrumentation, richer trajectory analysis, opt-in batch evaluations, a dedicated multi-machine installer, and deeper `my_opencode` integration remain future work. See the [agent observability guide](docs/agent-observability.md).

## 💜 Support

If this project helps you, [support its development](https://buy.stripe.com/8x200i8bSgVe3Vl3g8bfO00).

The repository code is MIT licensed; Langfuse and other components have their own licenses and terms. See [third-party notices](THIRD_PARTY_NOTICES.md).
