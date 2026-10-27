# CPA 账号权限后台与网关

管理员在网页或原 TUI 中设置“一个下游 Key → 多个 Claude 账号”。两端共用 VPS 上的 SQLite 策略、版本检查和操作记录。下游使用者只能查询自己的历史用量、获准账号的匿名额度，不能修改白名单。

本项目保留官方 CPA，不维护 CPA 分支，不使用 Docker。Python 3.10+ 标准库运行，无 Node.js 构建或 pip 依赖。当前验证对象为 CPA 8.0.42；升级 CPA 后必须重新运行隔离路由测试。

## 使用

先在仓库根目录运行 `cd scripts/cpa`。私密部署配置放在仓库外，通过 `CPA_TOOLS_CONFIG` 或 `--config` 指定。

网页部署在原 HTTPS 域名的 `/admin/`。独立管理员密码和 SSH 短时链接均可使用。

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

“账号剩余额度”显示每个账号的 5 小时、7 天等服务商窗口、采集时间和重置倒计时。页面每 30 秒读取 observer 的现有快照；“刷新额度”立即重读快照。observer 按自身周期采集（当前默认 5 分钟），刷新页面不会额外请求服务商。额度刷新不覆盖未保存的权限修改。查询失败与账号停用分别显示；Key 列表中的近 1h 条数是 observer 采集记录数。

新建或轮换 Key 后，其新身份默认没有账号权限。管理员在 `p` 权限页或网页授权后才能调用。已有 Key 改名保留权限。既有 Key 值与本机原 `.cpa-env` 路径不变。

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
5. 运行 `verify_remote.py` 验收四个滚动/历史窗口、旧日/月接口、每 Key 号池范围、401、管理隔离、短时链接单次使用、CSRF、历史保留和原服务 PID。该脚本不发送有效的模型生成请求。

密码设置需要操作者亲自输入；自动部署不生成或展示密码。服务源码升级前先备份，替换必要文件后仅重启 `cpa-admin`。读接口和 UI 不要求重启 CPA、observer 或 XBoard。

额度和近 1h 用量通过回环 observer 的已鉴权 `GET /v1/quota`、`GET /v1/usage?period=1h` 读取。下游请求向 observer 传递当前调用者的 Key，各 Key 使用自己的查询限额。后台不直接打开 observer 的数据库，也不导入其内部 Python 模块。管理员只读额度接口为 `/admin/api/quota`，要求管理员会话；下游 Key 只能通过 `/v1/quota` 查看获准账号的匿名额度，聚合值在过滤后重新计算。响应保留 `scope: shared_pool`，新增 `visibility: authorized_accounts`；已发布的 sh/ps1 客户端需要这个稳定的数据类别。

旧版后台直接以 `mode=ro` 读取 WAL 数据库；systemd 的只读挂载内，当 SQLite 的 `-wal`/`-shm` 文件不存在时会报 `unable to open database file`，造成正常采集被误显示为“额度未知”。[SQLite 官方说明](https://sqlite.org/wal.html#read_only_databases)列出了只读 WAL 的条件。复用 observer HTTP 接口可避免此生命周期依赖。不要用 `immutable=1` 读取仍在写入的用量数据库，也不要为此关闭服务隔离或重建历史库。

服务故障时网关拒绝请求，不自动绕过权限层。保持管理服务、官方 CPA 和 observer 三者健康。新服务使用线程上限和 512M 内存上限，大请求控制并发；Nginx 的 128M 上限不改变模型提供方自身的限制。

### 既有服务修复

审查修复按以下范围更新：

- 多用户额度查询互相限流：一起更新 `backend.py` 和 `service.py`，只重启 `cpa-admin`。下游查询会携带当前调用者的 Key，仍按白名单过滤账号。
- 更换管理员后旧账号仍可登录：更新 `authentication.py`，同步本机 `cpa-tools/api_keys.py` 的操作提示。源码更新保留现有登录信息；需要撤销旧账号时，由操作者通过 SSH 执行 `admin-password 新用户名`。成功后旧账号、会话及未使用短时链接全部失效。
- 初始受限 Key 名称填错：更新 `deploy_remote.py`，供后续首次部署使用。已部署环境通过网页或 TUI 核对并修正受限 Key 的白名单；不要重新导入整套初始权限。

先按 [observer Operation 与修复 Prompt](../cpa-observer/README.md#repair-prompt)核对运行源码、Python、路由和备份。若额度返回 HTTP 200 但客户端报“数据范围错误”，检查 `/v1/quota` 的 `scope`；网关应返回 `shared_pool` 并保留白名单过滤。若总览缺少每窗口 `models`，更新至包含模型分项的 observer。两种问题分别核对源码，按需替换 `backend.py` 或 `observer.py`，分别只重启对应服务。

备份两个源码、配置、unit、实际 Nginx 路由以及两份一致 SQLite 快照。快照连接用数据库所属服务用户打开，避免以 root 在 live WAL 目录创建 sidecar。记录策略版本、原历史行数和 CPA/XBoard/Nginx PID。验收四窗口、模型分项合计、旧接口、全部 Key 的额度范围与未授权 401，运行未改造的公开客户端；HTTP 200 本身不足以证明客户端可用。

源码回滚只恢复备份的源码并重启相应服务，保留当前 `usage.sqlite`、`control.sqlite`、配置、Key 和 OAuth。不得用快照覆盖当前数据库而丢弃修复后的调用记录。只有初次引入网关才需要下面的路由回滚；日常源码修复不应绕过权限网关。

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
