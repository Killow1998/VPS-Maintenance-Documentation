# CPA 账号权限后台与网关

管理员在网页或原 TUI 中设置“一个下游 Key → 多个 Claude 账号”。两端共用 VPS 上的 SQLite 策略、版本检查和操作记录。下游使用者只能查询自己的历史用量、获准账号的匿名额度，不能修改白名单。

本项目保留官方 CPA，不维护 CPA 分支，不使用 Docker。Python 3.10+ 标准库运行，无 Node.js 构建或 pip 依赖。当前验证对象为 CPA 8.0.42；升级 CPA 后必须重新运行隔离路由测试。

## 使用

先在仓库根目录运行 `cd scripts/cpa`。私密部署配置放在仓库外，通过 `CPA_TOOLS_CONFIG` 或 `--config` 指定。

网页部署在原 HTTPS 域名的 `/admin/`。独立管理员密码和 SSH 短时链接均可使用。

浏览器打开 `https://CPA_HOST/admin/`；Claude Code 的 Base URL 仍为 `https://CPA_HOST`。根路径 `/` 是 API 入口，未带 Key 返回 401 是正常鉴权结果。排查“网页打不开”时分别检查 `/admin/`、带 Key 的 `/v1/models` 和本机健康接口；不要把根路径 401 当成服务故障，也不要为此重置登录或绕过网关。

```bash
# 设置或替换网页登录管理员；密码隐藏输入两次
python3 cpa-tools/api_keys.py admin-password admin

# 生成一次性登录链接，自行复制到浏览器；2 分钟有效
python3 cpa-tools/api_keys.py web-login

# 原 TUI 保留：选中 Key 按 p 编辑账号权限，空格切换，s 保存
# 主屏 b 生成网页登录链接；g 查询全部账号剩余额度
python3 cpa-tools/api_keys.py

# 紧凑只读权限表
python3 cpa-tools/api_keys.py permissions
```

网页登录使用一个管理员账号。首次设置或忘记用户名、密码时，通过已有 SSH 权限执行 `admin-password 新用户名`。执行成功后只保留这个管理员，旧用户名、密码、全部会话和未使用的短时链接立即失效。输入无效或写入失败时保留原登录信息。此操作不修改下游 Key、账号权限或历史用量。

密码只存 scrypt 哈希；会话和短时链接只存哈希。网页登录会话 8 小时有效，Cookie 使用 Secure、HttpOnly、SameSite=Strict，并限制在 `/admin/`。写操作核对同源 Origin。

网页每行是一个 Key，每列是一个逻辑账号。修改后点击“保存权限”。空白名单拒绝模型请求；停用 Key 保留其白名单和历史。多账号仅在获准范围内调度，订阅额度不会按 Key 分割。额度条表示剩余百分比，重置时间为北京时间，括号显示 `2d3h left`。旧快照标注旧值；采集未知不显示成零。

“账号剩余额度”显示服务商窗口、采集时间和重置倒计时。页面每 30 秒读缓存；“刷新额度”在允许时请求立即采集，全局至少间隔 30 秒，不重叠。等待期间按钮置灰并显示剩余秒数；官方 429 的重试时间也会阻止手动刷新。observer 正常每轮结束等待配置的间隔（默认 60 秒）；遇到 429 时按账号将间隔提高为 120、240、480、900 秒，并等待至少 Retry-After 指定的时间。恢复后沿用已提高的后台间隔，手动查询仍受本地间隔及官方退避约束。不要为追求实时而缩短已有安全间隔。失败保留同一授权来源的额度和 reset 旧值并注明旧值，未取得新数据不能报告刷新成功。普通 API 查询和页面轮询不会触发官方请求，也不覆盖未保存的权限修改。这是本站采用的频率，不代表 Claude 网页的官方刷新周期。

账号卡片展示官方 reset 的剩余/总次数、北京时间到期时间和 `2d3h left` 倒计时。完整重置与 Fable 用量返还分别展示；后者不退还其他模型周用量。点“重置 Claude 额度”，选择类型并确认后才消耗次数。停用、旧快照、未知次数和待确认操作不提供兑换。禁用按钮下方显示原因；选择框内也说明各类型不可用的原因，例如次数已用完、暂无可返还的 Fable 用量、尚未达到使用条件或已到期。服务端重新核对账号绑定、官方可用性和组织身份，生成并持久保存 request_id；结果不明时禁止自动重复兑换。官方 HTTP 200 不是成功证明，必须识别业务 result。真实验收只读，不兑换 reset。

官方 reset 与 CPA 本地冷却为独立状态。CPA 当前接口只能无条件清除所有模型状态，不能防止检查后新增冷却，因此后台不自动清除，需等待 CPA 的冷却到期；如长期未恢复，由管理员核对实际状态。网页不提供普通用户“清除冷却”按钮。额度查询遵守上游返回的等待时间，即使凭据元数据更新或账号暂时停用也不会提前重试；上游返回但无法解析的窗口显示为未知。

新建或轮换 Key 后，其新身份默认没有账号权限。管理员在 `p` 权限页或网页授权后才能调用。已有 Key 改名保留权限。既有 Key 值与本机原 `.cpa-env` 路径不变。

## 会话调度

网关按新会话轮询，不再逐请求轮询。绑定范围为 **当前 Key + 会话 ID + 基础模型名**；同一会话的后续请求固定使用一个账号。不同新会话在相同获准账号集合与模型的候选池中轮询，只剩一个候选账号时保持使用它。网页/TUI 的 Key 白名单仍是权限边界，绑定不能扩大权限。

会话识别参考[官方 CPA 会话实现](https://github.com/router-for-me/CLIProxyAPI/blob/main/sdk/cliproxy/session/identity.go)，依次使用 `X-Claude-Code-Session-Id`、`Session-Id`、`X-Http-Session-Id`、`X-Session-ID`，再读取 Claude Code 的 `metadata.user_id` JSON 内 `session_id`、旧 `_session_…` 后缀或 `metadata.session_id`。标识必须可打印且不超过 256 字符；同一会话应保持同一标识，客户端不要每轮生成新 ID。

没有可靠 ID 的请求按 Key 与模型固定，不根据完整消息、时间戳或每轮变化的 request ID 生成身份。此时无法区分共用一个 Key 的独立会话。其他协议未提供上述标识时同样降级，不声称支持全部客户端自动识别或子会话继承。

路由使用实际模型请求结果。模型请求的 401/403/429 允许在当前白名单内尝试备用账号；备用返回有内容的 2xx 且 HTTP 传输完整后才迁移绑定，原账号恢复后不自动跳回。503 仅对本次请求回退，不改变长期绑定。每次尝试重读权限；账号停用、移出白名单或暂不支持当前模型时，只选择获准账号。没有获准备用账号时返回原错误，不借用其他号池。响应头或流式内容已经发出后不再次尝试，网络结果不明不自动重放。未声明长度、以连接关闭结束的上游响应不能可靠识别截断；HTTP 传输完整也不等于应用层生成一定成功。

需要迁移绑定的 SSE 流会正常转发中间事件，将可识别的完成事件留到传输完成且绑定保存后再发出，避免客户端提前发起下一轮。单个未完成事件或完成后的缓冲最多 1 MiB；超过时关闭该响应，不提交迁移，也不自动重放。绑定写入失败会记警告并保留已生成的回答，此时下一轮可能仍使用原账号。

**额度查询的 429 不参与模型调度**，不会因 observer 被限流而切号。路由不增加官方额度查询，CPA 仍负责认证、配额冷却与模型限制。新会话可能先分到已耗尽但仍启用的账号，再由 CPA 的实际错误触发回退；未知或旧额度不会被视为零额度，不把所有 429 都当作五小时额度耗尽。

`session_routing.py` 在现有策略 SQLite 新增 `session_routes`、`session_route_cursors` 两张附表。会话 ID 存 HMAC 摘要，Key、OAuth、请求正文、图片和工具结果不落库。绑定按请求续期，默认空闲 24 小时过期，最多保留 10,000 条，超限淘汰最久未使用的绑定；过期/淘汰后下一请求可能重新分配。候选池游标也持久保存并限量。事务防止并发首请求分到不同账号，代次校验防止旧响应覆盖后续迁移。策略、认证和用量表不重建。

### 缓存代价

旧轮询让一个增长中的会话在多个独立账号分别建立和更新缓存；新路由让它通常只在一个账号上建立。真正迁移时，备用账号若无匹配的有效缓存，需要重新处理当前输入前缀，不会重做历史回复。已有匹配缓存时也可能部分命中，不能把每次切换都算成全量重算。

绑定的 24 小时空闲期限不是提示词缓存 TTL。上游缓存仍取决于模型、前缀、账号隔离范围及实际 5m/1h TTL；本次不改 Claude Code 配置、不预热备用账号，不承诺命中率或订阅节省比例。[官方缓存范围](https://code.claude.com/docs/en/prompt-caching#cache-scope)

### 既有部署升级与验收

先核对实际运行文件及版本，保留现场定制。升级有两条路径：

- **从尚未安装 reset/刷新版本的 main 升级**：更新 admin 的 `backend.py`、`service.py`、`session_routing.py` 和 `web/app.js`、`web/index.html`、`web/style.css`，以及 observer 的 `observer.py`、`quota_actions.py`
- **已完整安装 PR #12 版本**：更新 admin 的 `backend.py`、`service.py`、`session_routing.py`、`web/app.js`、`web/index.html`，以及 observer 的 `observer.py`、`quota_actions.py`；其余文件先核对实际版本

使用管理 TUI 时，也要更新运行它的机器上的 `cpa-tools/remote_admin.py`，使其能读取新版额度快照；这不是 VPS 常驻服务。

备份将要替换的全部文件、配置、unit 和用量/策略一致 SQLite 快照，连接以数据库所属用户运行；此前不存在的文件记录为新增。先用临时库预检，再安装 companion 文件并替换其余文件，确认维护窗口后仅重启有变更的 `cpa-observer` 和 `cpa-admin`。admin 是转发网关，重启可能中断流式调用，需检查活跃任务并与用户确认。不更换 CPA 二进制，不重启 Nginx、XBoard，不改 HTTPS/代理，不运行首次部署脚本或重新导入权限。

合成测试运行 `python3 -B -m unittest discover -s cpa-admin -p 'test_*.py'`，覆盖同会话固定、新会话分配、相同 ID 跨 Key 隔离、Claude metadata、无 ID 降级、并发首请求、重启持久化、权限撤销、额度迁移与 503 回退。生产先验证只读接口、权限与历史保留；通过后续正常调用观察路由及实际 cache-read/cache-write，不发付费探测或预热请求。没有正常新调用就记为待验证，不用合成测试代替缓存节省证据。

Windows CI 额外安装固定版本的 `tzdata`，为权限审计时间提供 `Asia/Shanghai` IANA 数据；这是跨平台测试环境依赖，不增加 Linux VPS 的 Python 包依赖或改写系统时区。

回滚恢复实际变更前的全部 admin、web 和 observer 文件，**保留当前数据库**。若还有未到期的额度查询退避，必须按下方说明暂不启动旧 observer；不能只依赖旧版的定时轮询设置。新增附表可保留，旧版本会忽略；不以升级前快照覆盖新用量、管理员登录或权限。旧版没有逐操作防重放，回滚后须暂停 reset 重试并由管理员核对；不删除正在被新版服务导入的 companion 文件。

**回滚到旧 observer 前必须暂时阻断所有公网 `/v1/quota` 入口**。新版缓存可能包含 reset 和内部路由字段，而旧版读取缓存时不会过滤它们。先在现有 Nginx 站点配置中将该路径临时设为 503，`nginx -t` 后 reload，并验证实际公网入口已被阻断；不要暴露其他直连 observer 的路径。停止新版 observer，核对并等待所有退避期限到期，恢复旧源码后再启动旧 observer。保持公网阻断，直到旧 observer 自然产生回滚之后的新快照，且私下验证该快照的账号字段只包含旧版字段 `id`、`enabled`、`status`、`windows`、`as_of`、`stale`、`reason`、`retry_at`、`source_revision`，窗口只包含 `used_percent`、`remaining_percent`、`resets_at`，没有 reset、routing 或来源身份字段；通过 SSH 隧道验证旧服务的授权响应后，恢复原 Nginx 路由并再次验证公网响应。如果无法确认已生成安全快照，继续阻断，或保留新版 observer。不要用备份覆盖用量库，也不要通过删除数据库解决。

## 新增 Claude 账号

继续使用 `cpa-tools/claude_auth.py` 完成 setup-token 和同账号 profile/quota 授权。新增账号后执行：

```bash
python3 cpa-tools/api_keys.py sync-routes
python3 cpa-tools/api_keys.py
```

`sync-routes` 先备份被修改凭据、observer 配置和一致的 SQLite 快照，再通过官方管理接口设置唯一账号前缀。它不授予 Key 访问权限。额度辅助凭据不会加入模型号池。随后按 `p` 或打开网页授权。重新导入凭据、改文件名或升级 CPA 后也应检查路由状态。不要自行更改 observer HMAC、凭据文件名或账号前缀。

## 执行边界

```text
HTTPS /v1/* → Nginx → 权限网关 127.0.0.1:8319 → 官方 CPA 127.0.0.1:8317
HTTPS /admin/* → Nginx → 同一管理服务
/v1/usage → 网关鉴权 → 原 observer 127.0.0.1:8318
/v1/quota → 网关鉴权 → observer 已采集快照 → 按白名单筛选
TUI → 原 SSH 管理连接 → 仅回环 /internal/* → 同一策略库
```

网关把普通模型名改写成对应账号的唯一前缀模型。客户端自带账号前缀会被拒绝。账号模型目录重复前缀、同步超过 20 秒未成功、无授权或不支持的端点均拒绝转发。CPA 自身只能通过回环访问；Nginx 阻止公网 `/internal/`、`/healthz`、`/v0/management`、`/v8/management` 与旧管理页面。

支持 `/v1/messages`、`/v1/messages/count_tokens`、`/v1/chat/completions`、`/v1/responses`、`/v1/completions`，以及只读 `/v1/models`、`/v1/quota`、`/v1/usage`。不提供 WebSocket 或任意上游路径透传。模型列表按获准账号生成，客户端使用原模型名称。

网关仅在尚未向客户端发送响应、上游返回 401/403/429/503 时尝试另一个获准账号。每次尝试重新核对策略。单账号无额度或失效会返回错误，不借用其他账号。流式响应开始后不再切换账号。修改权限控制后续请求和后续尝试，不强行中断已开始的流式响应。

客户端原 Key 继续传给内部 CPA，observer 的 Key 标识和账号标识不变。本网关不消费 CPA 用量队列，不重建 observer 数据库。取消账号授权不删除该 Key 的历史用量。采集开始前或已丢失的记录不会凭空恢复，查询零条记录不等于从未发生调用。

## 部署与维护

部署前读取既有 `cpa-tools/.deployment.private.json`，确认域名、SSH、CPA/observer 版本、真实监听端口、配置路径、Nginx 内层 TLS 路由和账号身份。缺失信息应向操作者询问。不要将现有本机旧版 observer 覆盖到已修复的线上版本。

`deploy_remote.py` 是当前已有环境的受控部署入口，JSON 任务通过 SSH 标准输入传送。它要求已有 Nginx CPA 站点、observer、标准路径以及明确的初始受限 Key/账号。首次部署需要提供 `deployment`、`files`、`remote_admin`、`restricted_key`、`restricted_email`。`restricted_key` 填完整 Key ID 或唯一的显示名称，名称区分大小写；`restricted_email` 必须唯一对应一个已启用的账号。Key 匹配检查在写文件、启动服务和修改账号路由前完成。名称不存在、重名或部署期间 Key 清单变化时停止；不写入初始权限。其他已有 Key 保留全部当前账号，新 Key 默认无权限。不要脱离预检盲目执行或将它当成通用安装脚本。

1. 备份实际 Nginx 文件、CPA/observer 配置、被改凭据；使用 SQLite backup API 取得一致快照。记录原服务 PID、历史条数和源码校验值。
2. 安装 `/opt/cpa-admin/`、`/etc/cpa-admin/config.json`、`/var/lib/cpa-admin/control.sqlite` 与 `cpa-admin.service`。服务使用现有 `cpa-observer` 用户，只监听回环 8319。配置目录须显式设 750，不能依赖受 umask 影响的 mkdir 参数。
3. 设置账号前缀，核对所有账号模型目录，导入初始策略。先通过回环预检。
4. Nginx 原模型、quota、usage 路由指向 8319，增加 `/admin/` 和禁止公开的管理路由。保留 128M 请求体上限、流式转发、原 TLS/ACME。`nginx -t` 通过后只 reload Nginx。
5. 运行下方只读检查，并分别完成公网代理验收、历史保留和服务 PID 核对。短时链接单次使用与 CSRF 在隔离测试中验证；生产登录由管理员本人完成，不在只读检查中创建会话。

密码设置需要操作者亲自输入；自动部署不生成或展示密码。服务源码升级前先备份，替换必要文件后仅重启 `cpa-admin`。读接口和 UI 不要求重启 CPA、observer 或 XBoard。

额度和近 1h 用量通过回环 observer 的已鉴权 `GET /v1/quota`、`GET /v1/usage?period=1h` 读取。下游请求向 observer 传递当前调用者的 Key，各 Key 使用自己的查询限额。后台不直接打开 observer 的数据库，也不导入其内部 Python 模块。管理员只读额度接口为 `/admin/api/quota`，要求管理员会话；下游 Key 只能通过 `/v1/quota` 查看获准账号的匿名额度，聚合值在过滤后重新计算。响应保留 `scope: shared_pool`，新增 `visibility: authorized_accounts`；已发布的 sh/ps1 客户端需要这个稳定的数据类别。

旧版后台直接以 `mode=ro` 读取 WAL 数据库；systemd 的只读挂载内，当 SQLite 的 `-wal`/`-shm` 文件不存在时会报 `unable to open database file`，造成正常采集被误显示为“额度未知”。[SQLite 官方说明](https://sqlite.org/wal.html#read_only_databases)列出了只读 WAL 的条件。复用 observer HTTP 接口可避免此生命周期依赖。不要用 `immutable=1` 读取仍在写入的用量数据库，也不要为此关闭服务隔离或重建历史库。

服务故障时网关拒绝请求，不自动绕过权限层。保持管理服务、官方 CPA 和 observer 三者健康。新服务使用线程上限和 512M 内存上限，大请求控制并发；Nginx 的 128M 上限不改变模型提供方自身的限制。

### 既有服务修复

额度刷新、reset 与会话调度需要一起核对依赖文件，按上方[既有部署升级与验收](#既有部署升级与验收)选择完整升级路径。先安装 companion，避免导入失败。保留价格、TTL 采集、OAuth、权限和历史；只重启有变更的 admin/observer，不运行首次部署脚本。admin 重启可能中断转发中的流，切换前检查活跃请求并安排空闲窗口。

新增仅回环管理接口 `POST /internal/quota`，复用 management secret，支持 read/refresh/redeem。admin 通过现有管理员会话和 Origin 校验访问；普通 Key 无权访问，Nginx 不增加公共路由。公开 `/v1/quota` 不含 reset grant 或调度详情。操作按账号和操作 ID 保存在 observer metadata，不修改 records/cache_ttl 表。旧版只保留最后一笔，升级只能迁移仍存在的记录，无法恢复更早的操作 ID；切换前应等待进行中操作结束，不重放旧请求。unknown 结果需管理员核对后处理，不删除记录盲重试。

回滚恢复旧源码和原 `quota_poll_seconds`，保留当前数据库、reset 操作日志和新增用量。旧版读取新快照时会忽略单独保存的未知额度窗口，不应将其当作零；旧版既不执行新版源级退避，其定时采样也可能提前查询。恢复旧 observer 前，先停止它并核对快照及 `quota_backoff:*`、`quota_legacy_backoff:*` 记录中最晚的有效截止时间；截止前保持旧 observer 停止，无法确认期限时继续停用或保留修复后的 observer，不能宣称回滚完成。旧版公开接口可能暴露新版缓存字段，必须按[既有部署回滚步骤](#既有部署升级与验收)先阻断公网额度入口并验证安全快照。暂停期间管理端额度视图可能不可用。整个回滚期间暂停 reset 操作；旧版不具备完整操作 ID 防重放。不能用维护前快照覆盖数据库，也不能恢复旧 OAuth 文件覆盖新 token。已兑换的官方 reset 无法撤销。

审查修复按以下范围更新：

- 多用户额度查询互相限流：一起更新 `backend.py` 和 `service.py`，只重启 `cpa-admin`。下游查询会携带当前调用者的 Key，仍按白名单过滤账号。
- 更换管理员后旧账号仍可登录：更新 `authentication.py`，同步本机 `cpa-tools/api_keys.py` 的操作提示。源码更新保留现有登录信息；需要撤销旧账号时，由操作者通过 SSH 执行 `admin-password 新用户名`。成功后旧账号、会话及未使用短时链接全部失效。
- 初始受限 Key 名称填错：更新 `deploy_remote.py`，供后续首次部署使用。已部署环境通过网页或 TUI 核对并修正受限 Key 的白名单；不要重新导入整套初始权限。

先按 [observer Operation 与修复 Prompt](../cpa-observer/README.md#repair-prompt)核对运行源码、Python、路由和备份。若额度返回 HTTP 200 但客户端报“数据范围错误”，检查 `/v1/quota` 的 `scope`；网关应返回 `shared_pool` 并保留白名单过滤。若总览缺少每窗口 `models`，更新至包含模型分项的 observer。两种问题分别核对源码，按需替换 `backend.py` 或 `observer.py`，分别只重启对应服务。

备份所有受影响源码、配置、unit、实际 Nginx 路由以及两份一致 SQLite 快照。快照连接用数据库所属服务用户打开，避免以 root 在 live WAL 目录创建 sidecar。记录策略版本、原历史行数和 CPA/XBoard/Nginx PID。验收四窗口、模型分项合计、旧接口、全部 Key 的额度范围与未授权 401，运行未改造的公开客户端；HTTP 200 本身不足以证明客户端可用。

源码回滚只恢复备份的源码并重启相应服务，保留当前 `usage.sqlite`、`control.sqlite`、配置、Key 和 OAuth。不得用快照覆盖当前数据库而丢弃修复后的调用记录。只有初次引入网关才需要下面的路由回滚；日常源码修复不应绕过权限网关。

### 只读接口验收

在 VPS 上执行经过审查的脚本，`--config` 指向实际管理服务配置：

```bash
sudo python3 /PATH/TO/REVIEWED/cpa-admin/verify_remote.py --config /etc/cpa-admin/config.json
```

脚本只向配置指定的回环 HTTP 服务发 GET。它从现有管理配置读取 Key，并检查全部 Key 的模型目录、白名单内匿名额度和重算汇总、overview 与 1h/7d/30d/all/day/month、模型合计、当前 Key 标签、采集心跳、401 和管理隔离。端口从现有配置读取；不依赖某个固定模型名。报告只使用 Key 序号，不输出凭据、邮箱或原始响应。任一检查失败返回退出码 1；策略在检查期间改变也会报失败，重新读取现状后再验收，不回写旧策略。

该脚本不连接公网，不创建票据或登录会话，不直接打开数据库，不消费 usage queue，也不请求模型生成。输出 `ok=true` 只证明本轮回环读接口检查通过；`not_verified` 列出需要另外验证的项目：

- **公网链路**：在使用者电脑通过指定代理运行发布的 `quota.sh` / `quota.ps1`，以及带 Key 的 `/v1/models`。代理失败立即停止，不改成直连；另核对 HTTPS `/admin/`、无 Key 401 和公网管理路径 404。
- **历史与身份**：用备份与现库逐行比较已有记录、Key 身份、白名单、HMAC、账号绑定和历史计价。活动 SQLite 的读取和 backup 连接均使用数据库所属服务用户，不能让 root 在 live WAL 目录新建 sidecar。只读接口的总条数不能证明逐行保留。
- **自然新增事件**：等待已有调用后核对新增记录。采集心跳正常不等于已验证新事件入库；没有自然调用时明确标为未验证，零记录不代表没有实际调用。
- **进程与源码**：核对 systemd ExecStart、`/proc/PID/cmdline`、解释器和实际源码路径；源码相同不能单独证明运行进程已加载它。记录原 PID、服务启动时间、源码校验值和接口结果。

### 仓库、本机与 VPS 的同步边界

记录本次审查的完整 Git commit，再逐文件比较实际执行源码的 SHA256。分别检查本机 `cpa-tools` / `cpa-auth`（经 SSH 执行的源码也属于本机工具）、VPS `cpa-admin` / `cpa-observer`，以及 systemd、Nginx 的实际配置。不要把本机旧副本覆盖到已修复的 VPS。

确认运行进程已加载目标版本，且接口验收通过时，无需重启。核对服务启动时间与源码替换记录；若源码在进程启动后被替换，应只重启受影响服务并重新验收。发现差异先区分通用修复、现场定制和运行数据：通用修复通过 PR 汇入仓库；现场路径、端口、配置、价格文件、凭据与数据库保留私有，不能整目录覆盖。源码修复按前述备份和原子替换流程执行。`deploy_remote.py` 仍只用于首次部署，不是更新器。

本机同步前备份将被替换的工具文件，保留原 `.deployment.private.json` 和 `.cpa-env`。按仓库要求选择已安装的 Python 3.10+；例如系统 `python3` 为 3.9 而已有 `python3.11` 时，使用后者，不修改系统 Python。

### 回滚

本次部署备份目录中的 `rollback-routing.py` 将 Nginx 恢复到部署前路由，校验后 reload。**它保留当前所有数据库、Key、OAuth 凭据和新产生的历史用量。** 回滚路由也会恢复部署前的共享号池权限，应在维护窗口使用。

```bash
sudo python3 /var/backups/cpa-admin/DEPLOYMENT_TIME-initial/rollback-routing.py
```

不要用旧 usage.sqlite 覆盖现库。账号前缀可以保留，官方 CPA 仍支持原普通模型名。若确需还原前缀，读取账号路由备份中的 `route-changes.json`，只通过官方 credentials/fields PATCH 恢复 `previous_prefix`；不得整份恢复旧 OAuth 文件覆盖刷新后的 token。

## 测试与本机预览

```bash
cd cpa-admin
python3 -B -m unittest discover -p 'test_*.py' -v
python3 server.py
```

`server.py` 仅为本机演示入口，显示模拟账号与额度，不能反代到公网。正式入口是 `service.py --config /etc/cpa-admin/config.json`。UI 资源共用；生产模式有登录和真实策略状态。

`probe_cpa_routes.py` 使用隔离配置、假凭据、模拟 HTTP 上游和临时端口运行现有官方 CPA 二进制。它验证普通请求、流式请求、count_tokens、本账号 429 和未知前缀不会进入另一账号。不得把真实 OAuth 目录传给探针。标准库测试覆盖部署选择器、策略、并发版本、管理员替换与登录并发、单次票据、过期、CSRF、逐 Key 查询限额、前缀注入、重试和流式转发。

官方参考：[账号池 PR #6569](https://github.com/router-for-me/CLIProxyAPI/pull/6569)、[调度插件](https://help.router-for.me/plugin/scheduler)。本方案不依赖未合并账号池功能，也不将存在回退路径的调度插件单独视为权限边界。
