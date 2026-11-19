![AI Observatory — Private traces. Clearer agents.](assets/observatory-hero.png)

# 🔭 AI Observatory

![Local first](https://img.shields.io/badge/access-local%20first-117f78) ![Langfuse](https://img.shields.io/badge/Langfuse-4.82.0-6149aa) ![OpenCode](https://img.shields.io/badge/OpenCode-official%20plugin-252e46) ![License](https://img.shields.io/badge/license-MIT-blue)

A private-first home for coding-agent traces. Run [Langfuse](https://langfuse.com/) on your own Mac or Linux machine, connect projects through the official OpenCode plugin, and optionally reach it from other devices on your Tailscale network.

**By default nothing is published to the LAN or tailnet.** Docker publishes the web and media ports on localhost only. Tailscale sharing is a separate, explicit step; it is not Tailscale Funnel or public internet access.

> Traces can contain prompts, reasoning, source code, tool inputs and outputs, and secrets. Only trace projects and data you are allowed to store. See [security and privacy](docs/security.md).

## 🚀 Start the server

Requirements: macOS or Linux, Python 3.10+, Docker Engine **28+** with Docker Compose, Make, and free localhost ports `3000` and `9090`. The initial image downloads require internet access. On macOS, start Docker Desktop first.

```sh
git clone https://github.com/dmoliveira/ai-observatory.git
cd ai-observatory
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

## 🧩 Trace an OpenCode project

Install OpenCode 2 and Node.js 22+ on the machine running OpenCode. The integration uses the pinned official `@langfuse/opencode-observability-plugin@0.7.0`.

```sh
make connect-opencode PROJECT=/absolute/path/to/project
make run-opencode PROJECT=/absolute/path/to/project
```

Enter the project's Langfuse public and secret keys when prompted. The connection is verified before credentials are saved under `~/.config/ai-observatory/projects/` (or `$XDG_CONFIG_HOME`) with restrictive permissions. The project receives an `.opencode/opencode.json` that gates plugin loading on an environment variable: **ordinary OpenCode sessions do not enable this integration**. `make run-opencode` starts a standalone session with the protected environment, so a previously running OpenCode service cannot keep an old tracing destination.

If your project already has `.opencode/opencode.json` or `.jsonc`, the command refuses to overwrite it. Add the plugin entry as described in [the integration guide](docs/opencode.md), then rerun the command. The launcher does not automatically configure unrelated OpenCode plugins; existing global/project configuration still applies.

## 🔐 Share with another Tailscale machine

On the **server**, connect Tailscale, enable MagicDNS and HTTPS certificates in your tailnet, and review the tailnet's ACLs or grants for ports `15443` and `15444`. Start the stack, then explicitly enable sharing:

```sh
make up
make tailscale-on
```

The command prints the private HTTPS web address. Both dedicated ports must be unused; existing Serve and Funnel configuration is preserved, and concurrent changes are rejected. Enabling Tailscale HTTPS can publish the machine and tailnet DNS name in Certificate Transparency, even though the service is tailnet-only. Tailnet administrators and devices allowed by your grants can reach it; Tailscale does not replace Langfuse authentication.

On the **client machine**, join the same tailnet and clone this private repository. It needs Python, Make, OpenCode 2 and Node.js 22+, but **not Docker or a local server**. Set the server URL printed above and connect the project:

```sh
AI_OBSERVATORY_URL=https://your-server.your-tailnet.ts.net:15443 \
  make connect-opencode PROJECT=/absolute/path/to/project
make run-opencode PROJECT=/absolute/path/to/project
```

Use a real hostname and key from your own server. This client-only path is intentionally simple; tighter `my_opencode` integration and a dedicated installer can be added later.

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

## 🗺️ Later

Dashboards and extracted metrics, batch evaluation workflows, a dedicated multi-machine installer, and deeper `my_opencode` integration are future work—not requirements for the initial local stack.

## 💜 Support

If this project helps you, [support its development](https://buy.stripe.com/8x200i8bSgVe3Vl3g8bfO00).

This repository is private for now. The repository code is MIT licensed; Langfuse and other components have their own licenses and terms.
