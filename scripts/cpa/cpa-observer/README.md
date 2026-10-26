# CPA Observer

本页命令从工具包根目录执行：先在仓库根目录运行 `cd scripts/cpa`。实际配置和会话放在仓库外。

Daily administrator operations are available through `python3 cpa-tools/api_keys.py` from the project root: add, revoke, rename, rotate, export privately, and query usage. It keeps labels synchronized with CPA and preserves historical records. GPT is not required. See the [self-service SOP](../cpa-tools/README.md). New Claude accounts use the paired `claude_auth.py` entry point; the observer remains the server-side collector and read API.

The TUI's `g` view and `api_keys.py quota --admin` join anonymized account IDs with private account emails over administrator SSH. This mapping is not added to this service's public responses. Ordinary users can run `api_keys.py quota --base-url https://CPA_HOST --key-env CPA_API_KEY_CLIENT_A` with only their Base URL and Key; no deployment configuration or SSH access is required.

Native Python 3 service for CPA v8. No Docker or external database is required. It polls the local CPA usage queue into SQLite and serves two read interfaces behind the existing HTTPS reverse proxy.

For the complete VPS deployment procedure, restricted configuration template, Nginx routes and rollback steps, see section 9.1 of the accompanying [CPA + Claude Code + VPS agent guide](../CPA_CLAUDE_GENERAL_GUIDE.md). Share this source folder together with that guide; neither contains production credentials.

## Client API

Both interfaces use the same root Base URL and `Authorization: Bearer <client CPA Key>` as model calls. Keys are validated by the actual CPA `/v1/models` interface. Positive validation is cached for at most five seconds; revocation therefore takes effect within five seconds. A management Key is never given to clients.

| Method and path | Result |
| --- | --- |
| `GET /v1/quota` | Shared Claude account-pool usage windows; cached for approximately five minutes |
| `GET /v1/usage?period=day` | Only the calling Key's usage for the current Beijing calendar day |
| `GET /v1/usage?period=month&date=2026-10` | Only that Key's October usage |
| `GET /v1/usage?period=all` | Only that Key's retained usage |
| `GET /v1/usage?period=day&recent=20` | That Key's totals, anonymous account breakdown and latest 20 execution attempts in the same day |

Missing/invalid keys return 401, unsupported account/client selectors return 400, and request limits return 429. These endpoints are extensions supplied by this service, not native CPA endpoints. All valid downstream Keys in this deployment use the same shared Claude pool. If per-Key upstream routing restrictions are introduced, pool selection must be updated to match them before exposing restricted pools.

The standalone [quota.sh](../quota.sh) and [quota.ps1](../quota.ps1) clients show own-Key usage and pool quota together. They need only a Base URL and downstream Key. One-command execution from a pinned GitHub commit is documented in [guide section 9.3](../../../docs/09-cpa-claude.md).

### Own-Key usage response

`totals` and `models` retain the existing totals and model breakdown. `accounts` adds one aggregate per anonymous upstream account/provider: `id`, `provider`, request and failure counts, token fields, estimated cost bounds, pricing coverage and `last_used_at`. `recent` adds execution records with `timestamp`, `account_id`, `model`, `provider`, `failed`, token fields, accounting quality, latency and estimated cost bounds. Neither list contains a raw client Key, credential name, email, auth index, execution ID or request/response body.

Account IDs use the same `account-` HMAC prefix as `/v1/quota`, so a caller can match the account that handled a request to its current quota windows. Removed accounts retain their historical pseudonym. Missing account telemetry produces a null ID, never a guessed account. These aggregates describe only the calling Key's use of each account; shared pool quota remains shared.

`recent` is an optional integer query parameter from 0 to 50, default 10. Records are sorted newest first and filtered to the same Beijing day/month/all period as the aggregates. `recent=0` omits execution details while retaining totals. Invalid or duplicate parameters return 400. Client identity is derived exclusively from the validated Bearer Key; callers cannot select another client or account. All three usage queries share the same client/date filter and one SQLite read transaction.

Use `query.py` to avoid exposing a secret in process arguments. Set `CPA_BASEURL` (or `ANTHROPIC_BASE_URL`) to your HTTPS gateway root. With no `--client`, it reads `ANTHROPIC_AUTH_TOKEN` or prompts without echoing. With `--client client_a`, it reads `CPA_API_KEY_CLIENT_A` from the environment:

```bash
python3 query.py quota --client client_a
python3 query.py usage --client client_a --period day
python3 query.py usage --client client_a --period month --date 2026-10
```

## Interpreting usage

Records identify upstream execution attempts, not necessarily unique user messages. Retries can generate multiple records. Canonical CPA v2 accounting is used: `input_tokens` includes the non-overlapping uncached, cache-read and cache-write buckets; output includes reasoning tokens. Do not add cache or reasoning fields again to the totals.

The `estimated_cost_usd_low` / `estimated_cost_usd_high` fields use the API price snapshot in `prices.json`; they are not subscription charges or a monetary balance. When telemetry does not distinguish five-minute and one-hour cache writes, the bounds use those respective rates. Prices apply only to explicitly configured models and standard token pricing; unknown models or incomplete accounting are unpriced. The response reports `priced_requests` and `incomplete_accounting_requests`, so partial pricing cannot be mistaken for a complete bill. Special service tiers, geography adjustments or separate tool fees are not calculated.

The price source is the [Anthropic API pricing table](https://platform.claude.com/docs/en/about-claude/pricing), checked on 2026-10-25. Prices are copied into each event's calculated cost and price version at ingestion; later price-file changes do not rewrite historical costs.

## Interpreting quota

`remaining_percent` is a provider-window percentage, not a token count. Data has `checked_at`, per-account `as_of` and `stale` flags. Missing/failed queries remain unknown or stale, never a fabricated 100%. With multiple accounts, percentages are not summed: the API provides anonymized account windows and a min/max interval. `remaining_percent` is populated at pool level only for a single known account.

The service queries the server-side OAuth usage endpoint through CPA's authenticated local management `requests/api-call` operation. No OAuth token is downloaded to this service or exposed downstream. This upstream endpoint is not a guaranteed stable third-party API; a valid server-side credential with the necessary scope is required. New or renewed model credentials default to agent-managed `claude setup-token`, following section 6.1 of the accompanying guide. The operator starts and supervises the process, checks its callback listener and gives the user a link without opening a browser. The user selects the account in their chosen private window, completes email verification and authorizes; never ask them to copy a token. This observer does not supervise login. Complete onboarding requires separate real generation, identity and quota checks.

### Pairing a quota credential

If the inference token cannot read account data, obtain a companion grant for the same user-confirmed account with `cpa-auth/managed_quota.py`. Verify its profile email and actual usage windows, then keep that credential disabled for model scheduling. Add this mapping to the restricted observer configuration, replacing every placeholder:

```json
"quota_bindings": {
  "MODEL_CREDENTIAL_NAME.json": {
    "credential": "QUOTA_ONLY_CREDENTIAL_NAME.json",
    "expected_email": "ACCOUNT_EMAIL_CONFIRMED_BY_USER"
  }
}
```

The source must be a disabled Claude credential with matching email. Binding supplements the existing inference credential; it never replaces its token. Source entries are excluded from logical account counts, so one model credential plus its quota helper remains one account. Email, filenames, auth indexes and internal source revisions are omitted from the public response. A mismatch fails closed and does not reuse another account's cached windows. Unbound ordinary OAuth credentials continue to use their own quota source.

On upstream 401, the observer calls CPA's local `/v8/management/credentials/refresh` for only the quota source, discards the response without reading credential-bearing metadata and retries quota once. Missing scope on 403 does not trigger repeated refreshes. A 429 honors the upstream retry delay and uses cached data with its age/status. Failures report safe reasons and unknown or stale data, never a fabricated balance. Restart only the observer after changing bindings; preserve its database and HMAC secret.

## Operation

Paths on the server:

| Path | Purpose |
| --- | --- |
| `/opt/cpa-observer/observer.py` | Service source |
| `/opt/cpa-observer/prices.json` | Non-secret price snapshot |
| `/etc/cpa-observer/config.json` | Restricted configuration: local management secret, HMAC salt, hashed Key labels |
| `/var/lib/cpa-observer/usage.sqlite` | Durable sanitized usage database, WAL mode |
| `/etc/nginx/cpa-observer-locations.conf` | Exact routing for the two client interfaces |

The service runs as dedicated user `cpa-observer`, listens only at `127.0.0.1:8318` and needs no additional public port. Its systemd unit limits filesystem writes to its data directory and caps memory at 128 MB. Nginx must include its location file only inside the correct CPA HTTPS server. Never include the health endpoint in the public route.

The configuration is provisioned privately and is not part of this source folder. It must contain `management_key`, `hmac_secret`, `cpa_base_url` (loopback only), `database`, `prices_file` and optional `labels` mapping `HMAC(secret, "client:" + key)` to non-secret labels. New CPA Keys still authenticate and receive a pseudonymous client label until the operator adds a friendly label. Raw client keys, OAuth credentials, account email addresses and request/response bodies are not stored in SQLite.

```bash
sudo systemctl status cpa-observer
sudo python3 /opt/cpa-observer/observer.py --report --period month
sudo journalctl -u cpa-observer --since '30 minutes ago'
```

The local `--report` command is for administrators and can summarize all client labels. It is not exposed over the client API. Back up SQLite using its backup API or a consistent snapshot, not a lone database-file copy while WAL writes are active.

For an existing installation, back up the current source and a consistent SQLite snapshot, replace the reviewed `observer.py` atomically and restart the existing observer unit. Preserve its HMAC secret, database and reverse-proxy routes. The account/recent additions reuse existing columns and require no schema migration. Verify own-Key isolation and the `/v1/usage` response after restarting; restore the prior source if the new read API fails.

## Reliability limits

The usage queue is memory-backed and destructively popped. Three-second collection plus durable SQLite commits reduces loss, but does not provide transactional acknowledgement: a crash between pop and commit or a long CPA/collector outage can lose events. This is usage observability, not a guaranteed financial ledger. Report the collector status, rejected-event count and missing historical coverage honestly. Historical data from before deployment is unavailable unless it was independently saved and imported.

Test the service using `python3 -m unittest -v test_observer.py`. Integration tests use only ephemeral local sockets and fake credentials.
