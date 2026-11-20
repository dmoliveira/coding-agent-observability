# Security and privacy

## Boundaries

- Docker's published web and MinIO ports bind to `127.0.0.1`. Use Docker Engine 28 or later: older releases can expose localhost-published ports to other hosts on the same network. Unusual Docker direct-routing or firewall settings can alter this boundary; check your host configuration.
- Local access is not isolation from other local users or Docker administrators. Restrict host accounts, Docker access, file permissions, backups and physical access.
- The stack's containers can make outbound connections. Review any configured integrations, webhooks and scheduled work; this is not an air-gapped environment.
- Tailscale Serve is optional and private to permitted tailnet devices. Check grants for **both** HTTPS ports `15443` (Langfuse) and `15444` (media); do not use Funnel. HTTPS certificate issuance can publish the node/tailnet DNS name in Certificate Transparency.
- Langfuse signup is disabled and OTLP ingestion requires project API credentials. Use strong credentials, restrict project membership, and rotate keys if exposed. Never share `.env` or an unencrypted backup.
- Once installed, the global OpenCode adapter records sensitive content by default, including prompts, previous context, model output, reasoning and tool data. It can start in a running service as soon as the plugin entry is installed. Start a new untraced standalone process as described in [the integration guide](opencode.md); this does not disable other exporters or erase existing traces. Other plugins, model providers, OpenCode configuration and network egress are outside this repository's control; model calls can still leave your machine.
- Langfuse self-hosted OSS does not automatically enforce project retention. Establish your own review, deletion and backup-retention practices.
- Langfuse telemetry is disabled in this Compose configuration, but image/plugin downloads, Tailscale and configured AI providers still use network services. This is not an air-gapped deployment.

## Backups

Backups are raw, unencrypted offline volume snapshots with the original secrets. Encrypt them, restrict access, and verify a restore on a separate installation. Stop all producers and allow queues to drain before stopping the stack. Stack, sharing and backup commands in the same checkout use a maintenance lock, and the backup checks volumes are not in use. The lock cannot prevent an external Docker command or another checkout from attaching the volumes during a snapshot.

If a startup is interrupted or fails, the installation retains a marker to prevent an unsafe backup or another startup. Run `make down` and, once it succeeds, retry `make up` or `make backup`. Do not manually remove the marker while a Docker operation may still be running.

Only restore trusted archives: checksums are not signatures, volume data can contain executable or malicious content, and database applications may execute restored data. Restores also retain queued work, project settings and integrations that may contact external services when the restored instance starts. Isolate its outbound network at the host or firewall if those side effects are unacceptable. Restores do not overwrite existing `.env` files or existing volumes. Raw-volume restore checks the Langfuse version, configured service image references and Docker architecture; test migrations separately. Image tags are not cryptographic proof that the underlying image bytes are identical.

## Tailscale recovery

Serve and Funnel share configuration. The commands use an ETag conditional update and reject occupied, changed or shared ports. They never issue `reset` or restore a stale whole configuration.

If enabling fails after the server is reconfigured, the update may have succeeded even if its response was lost. Inspect `tailscale serve status` and `tailscale funnel status`; then run `make tailscale-off`. It removes routes only if both still match this installation, or returns to localhost if both dedicated ports are unused. If either route is shared or changed, it refuses to modify it; resolve the conflict with Tailscale's own tools without deleting unrelated routes.

If route cleanup fails during `make down`, the containers are still stopped and the command reports failure. The stale route must still be reviewed before starting the stack again. A tailnet administrator may need to enable HTTPS certificates or grant device access before sharing works.

## Reporting

Do not include credentials, private trace content, local API tokens, or backup archives in issue reports. Use [private vulnerability reporting](https://github.com/dmoliveira/coding-agent-observability/security/advisories/new) for security concerns, or contact the repository owner privately if it is unavailable.
