# CPA Observer

本页命令从工具包根目录执行：先在仓库根目录运行 `cd scripts/cpa`。实际配置和会话放在仓库外。

Daily administrator operations are available through `python3 cpa-tools/api_keys.py` from the project root: add, revoke, rename, rotate, export privately, and query usage. It keeps labels synchronized with CPA and preserves historical records. GPT is not required. See the [self-service SOP](../cpa-tools/README.md). New Claude accounts use the paired `claude_auth.py` entry point; the observer remains the server-side collector and read API.

The TUI's `g` view and `api_keys.py quota --admin` join anonymized account IDs with private account emails over administrator SSH. This mapping is not added to this service's public responses. Ordinary users can run `api_keys.py quota --base-url https://CPA_HOST --key-env CPA_API_KEY_CLIENT_A` with only their Base URL and Key; no deployment configuration or SSH access is required.

Native Python 3.10+ service for CPA v8, using the same minimum version as the XBoard/CPA operation tools. Check the interpreter in `ExecStart` before replacing an existing observer; the new entry point rejects older versions before reading configuration. Do not upgrade the OS Python or restart production just to update this documentation. No Docker or external database is required. It polls the local CPA usage queue into SQLite and serves two read interfaces behind the existing HTTPS reverse proxy.

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
| `GET /v1/usage?period=overview` | Four own-Key windows: rolling 1h, 7d, 30d, and retained history, with one cutoff and one SQLite read snapshot |
| `GET /v1/usage?period=1h&recent=20` | Own-Key detail for the last hour; `7d` and `30d` are also supported |

Missing/invalid keys return 401, unsupported account/client selectors return 400, and request limits return 429. These endpoints are extensions supplied by this service, not native CPA endpoints. All valid downstream Keys in this deployment use the same shared Claude pool. If per-Key upstream routing restrictions are introduced, pool selection must be updated to match them before exposing restricted pools.

The standalone [quota.sh](../quota.sh) and [quota.ps1](../quota.ps1) clients require an explicit local HTTP/mixed proxy as well as a Base URL and downstream Key. Missing or failed proxies cannot fall back to DIRECT; the local proxy must also route the gateway through a real proxy node. Interactive setup enables the four-window overview and cached pool view by default; each manual refresh uses two requests, with no automatic polling. The [short launchers](../../../README.md#cpa-一键查询) initially fetch public, credential-free bootstrap code over the current network; only after prompting for the port do they require the proxy for the pinned, hash-verified client download and all API calls. Fully proxied download commands remain in [guide section 9.3](../../../docs/09-cpa-claude.md).

### Own-Key usage response

`totals` and `models` retain the existing totals and model breakdown. `accounts` adds one aggregate per anonymous upstream account/provider: `id`, `provider`, request and failure counts, token fields, estimated cost bounds, pricing coverage and `last_used_at`. `recent` adds execution records with `timestamp`, `account_id`, `model`, `provider`, `failed`, token fields, accounting quality, latency and estimated cost bounds. Neither list contains a raw client Key, credential name, email, auth index, execution ID or request/response body.

Account IDs use the same `account-` HMAC prefix as `/v1/quota`, so a caller can match the account that handled a request to its current quota windows. Removed accounts retain their historical pseudonym. Missing account telemetry produces a null ID, never a guessed account. These aggregates describe only the calling Key's use of each account; shared pool quota remains shared.

`recent` is an optional integer query parameter from 0 to 50, default 10. Records are sorted newest first and filtered to the same Beijing day/month/all period as the aggregates. `recent=0` omits execution details while retaining totals. Invalid or duplicate parameters return 400. Client identity is derived exclusively from the validated Bearer Key; callers cannot select another client or account. All three usage queries share the same client/date filter and one SQLite read transaction.

`period=overview` is a separate compact response with `windows`, each containing `period`, `start_utc`, `end_utc`, `totals`, and `models`. Each model row has the same request/token/cost/coverage fields as a detail query; an empty window has `models: []`, not a missing field. All four use the same `as_of` cutoff and database read transaction. The rolling windows are `[as_of-duration, as_of)`; retained history has no lower time bound but excludes future records. No account identifiers or individual execution records are returned in this overview. It accepts no `date` or `recent` parameters. Ordinary detail queries also support `1h`, `7d` and `30d`; `date` remains limited to calendar day/month. These additions do not change existing day/month/all callers.

Clients calculate the token-weighted cache hit percentage as `100 * cache_read_tokens / input_tokens`, for both totals and each model. Any unknown component or zero denominator means unknown, not zero. Cache writes do not count as hits. USD estimates sum the stored per-record price snapshots; changing the current price file does not reprice history. `priced_requests / requests` is coverage: partial cost sums are marked `*`, and all-unpriced estimates remain null. Cache-write TTL uncertainty yields the existing 5m/1h cost bounds. This is API list pricing, not subscription charges or a complete invoice. Model/window selection and pagination reuse one response without additional requests; refresh still makes only the two existing calls. Older overview responses without `models` remain readable but show an explicit upgrade notice instead of claiming no calls.

Windows overlap and must not be added together. Thirty rolling days are not a calendar month. `collection_started_at`, `last_collected_at`, `collector_status`, `rejected_events` and the historical-coverage warning accompany the overview. They do not prove continuous collection: outages and earlier deletions may leave gaps. `/v1/quota` is a separate cached snapshot, not part of the usage transaction and not a per-client token allowance.

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

For an existing installation, back up the current source and a consistent SQLite snapshot, replace the reviewed `observer.py` atomically and restart only the existing observer unit. Preserve its HMAC secret, database and reverse-proxy routes. Account/recent and rolling-overview additions reuse existing columns and require no schema migration. Do not reinstall CPA, restart the forwarding node, reset usage or recreate client Keys. Never run a second usage-queue consumer for testing.

After the restart, verify `/healthz` locally, then authenticate as an existing client and request `period=overview`, a legacy day query, a rolling detail query, and `/v1/quota`. The overview must contain exactly four ordered windows and no other client's records. Check collection resumes and retained history remains; a new observation may arrive during verification, so an unchanged row count is not required. On failure, atomically restore the prior source and restart only observer. Keep the database in place to preserve observations collected after the source backup; database recovery is a separate decision, not part of routine source rollback. A repository update alone does not upgrade a running VPS. Old observers may return 400 for overview; clients must report this rather than substitute calendar-month data.

<a id="repair-prompt"></a>

## 已有部署修复 Prompt

适用于号池可读，但客户端提示 `HTTP 400`、总览/滚动时间段需要更新 observer，或四段总览已有但缺少按模型明细的情况。先核对入口路由、运行进程和源码，不要根据磁盘文件或 Git HEAD 推断服务已经更新。补充模型分项只复用现有列，不改 schema、不重算历史费用。

```text
请修复我已部署的 CPA 用量查询，不要重新安装或替换 XBoard/CPA。
仓库：https://github.com/Killow1998/VPS-Maintenance-Documentation
先阅读 scripts/cpa/cpa-observer/README.md 的 Operation 与本 Prompt，
以及 docs/09-cpa-claude.md 第 9.3 节。拉取后记录本次采用的完整 commit。

现象：客户端能显示共享号池，但个人用量提示参数不受支持，
或已有四段总览却缺少模型分项。目标是当前 Key 的近 1h、7d、30d、总历史，
每段包含 models，以及已有的 token、缓存读写、USD 估算和计价覆盖数。

先只读核对：实际主机、Base URL 的 /v1/usage 和 /v1/quota 路由、
systemd ExecStart/运行进程、observer 源码版本、解释器、配置路径、数据库和采集状态。
路径以现场为准，不假定仓库示例目录。用已有 Key 安全验证 overview 返回码和结构。
密钥只从既有私密配置读取或隐藏输入，不放命令行、日志、报告或 Git。

确认是旧 observer 后，在改动前备份源码、配置、unit、路由及一致的 SQLite 快照。
活动 WAL 数据库用 SQLite backup API，不只复制主 .sqlite 文件。
保持数据库、HMAC secret、Key/标签、历史用量、OAuth 授权及号池绑定不变。
检查实际 ExecStart 的 Python >=3.10、语法、配置与数据库兼容性，
运行仓库的 test_observer.py；不支持时先报告，不擅自升级系统 Python。
不得启动第二个采集器消费 usage queue，也不得直接拿生产数据库做写入测试。

预检通过并准备原源码回滚后，原子替换必要的 observer.py，
只重启现有 observer 服务。默认不改 prices.json、配置、Nginx、unit 或其他服务；
若发现路由等另有故障，先说明最小修复及影响，再取得对应授权。
不重启 CPA/XBoard/代理节点，不开公网端口，不重建 Key，不清零或重建数据库。

验收：本机 /healthz；经客户端代理及 HTTPS 查询 overview、1h/7d/30d、
旧 day/month/all 与 /v1/quota。overview 必须有按 1h、7d、30d、all 排列的四个窗口，
scope=own_key，各窗口共用截止时间；只含当前 Key，号池仍为独立缓存。
各窗口 models 的请求和已知 token/费用应与 totals 相符（费用允许输出舍入误差）；
缓存命中率用合计缓存读取除以合计输入，不平均请求比例，不把缓存写入算作命中。
任一计量缺失或零输入时比例未知；缺价格不当零，部分费用标明计价覆盖。
历史费用使用原入库快照，不覆盖现存价格文件或重新计价历史。
重新运行首页一行命令获取新版客户端，验证 1/2/3/4 切换模型窗口、N/P 翻页不联网，
R 刷新只查询两个既有只读接口。不得以客户端更新代替服务端验收。
验证既有记录保留、匿名账号映射不变、采集恢复；若没有新的真实调用，
明确“未验证新事件入库”，不要编造增量，也不要为验收调用付费模型。
没有已采集记录时应返回零记录并说明历史覆盖；零记录不证明实际没有调用。
不得补造采集前的数据，也不得用共享号池或其他 Key 的数据代替。

失败时恢复原源码，只重启 observer；保留当前数据库，不用旧快照覆盖新增用量。
数据库恢复须另行说明并获得批准。最后报告实际版本、变更文件、测试证据、
备份位置、回滚命令和历史覆盖缺口。报告与备份留在私有位置，不上传凭据。
```

## Reliability limits

The usage queue is memory-backed and destructively popped. Three-second collection plus durable SQLite commits reduces loss, but does not provide transactional acknowledgement: a crash between pop and commit or a long CPA/collector outage can lose events. This is usage observability, not a guaranteed financial ledger. Report the collector status, rejected-event count and missing historical coverage honestly. Historical data from before deployment is unavailable unless it was independently saved and imported.

Test the service using `python3 -m unittest -v test_observer.py`. Integration tests use only ephemeral local sockets and fake credentials.
