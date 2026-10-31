# VPS + CLIProxyAPI + Claude Code

在 Linux VPS 上部署 CLIProxyAPI（CPA），通过 Nginx 提供 HTTPS 入口，让 Claude Code 使用独立 API Key 调用模型。CPA 由 systemd 管理。

服务端需要具备 sudo 权限的 Debian/Ubuntu。Python 工具统一使用 Python 3.10+，请分别检查本机和 VPS 的版本。管理员工具在 macOS、Linux 或 Windows WSL 中运行；Windows 个人查询使用 PowerShell。

| 读者 | 阅读路径 |
| --- | --- |
| 部署者 | 按 1—8 节完成部署和真实请求验证。使用双授权工具时，先完成 9.1 的 observer 准备。 |
| 仅使用服务的人 | 取得入口、自己的客户端 Key 和模型 ID，阅读 7—8 节；9.3 可查自身用量、近期调用和匿名剩余额度。 |
| 管理员 | 9.2 管理 Key、查看用量和账号剩余额度；10—11 节用于维护。 |
| 执行任务的 Agent | 先读第 12 节，再执行对应部署步骤；第 11 节定义交付标准。 |

“VPS”指远端服务器，“本机”指使用者的电脑。执行命令前，按标注选择运行位置，并替换示例中的占位符。

## 1. 准备需求

先整理已有信息，只询问仍缺失且影响操作的项目。有域名走 5.1，仅有公网 IP 走 5.2。

| 需要准备 | 确认内容 | 提供方式 |
| --- | --- | --- |
| 目标与权限 | VPS、SSH 端口/用户、sudo 权限、允许修改的服务 | 复用本次会话和 SSH 配置 |
| SSH 登录 | 密钥文件、SSH agent 或可用会话 | 给本机路径，不发送私钥内容 |
| 入口 | 域名或 IP、80/443 占用、SNI 分流 | 域名需控制 DNS；IP 需控制该地址 |
| 上游认证 | 复用有效凭据、新增 OAuth 或已有上游 API Key | OAuth 由持有人浏览器授权；Key 写入私密配置 |
| 使用者 | 客户端 OS、shell、CLI 状态、代号和 Key 数量 | 先复用已有 Key |
| 用量与额度 | 按 Key 统计、匿名号池查询、管理员账号视图 | 需要这些功能时部署第 9 节 |

### 1.1 部署前只读检查

**位置：VPS。前提：目标和访问方式已确认。完成：记录环境、端口和保留业务。**

- 检查 OS、CPU 架构、磁盘、内存、CPA 版本/路径、systemd、Nginx 有效配置与监听。
- 确认当前 SSH 实际端口、防火墙、云安全组和 443 的处理方式。无法读取云安全组时记为“待用户确认”。
- 备份将修改的文件，确定恢复路径。认证目录和配置备份按秘密数据保存。
- 影响现有网站、节点、默认 SNI 路由时，先说明具体影响并取得对应授权。

### 1.2 占位符与命令规则

| 占位符 | 填写内容 |
| --- | --- |
| `VPS_HOST` / `SSH_PORT` / `admin` | 本次目标、实际 SSH 端口和用户 |
| `~/.ssh/YOUR_PRIVATE_KEY` | 本机私钥路径 |
| `api.example.com` / `PUBLIC_IP` | 自己的域名或公网 IP |
| `CERTIFICATE_NAME` | `certbot certificates` 显示的实际名称 |
| `REPLACE_WITH_*_KEY` | 私密生成或已有的对应 Key |
| `REPLACE_WITH_MODEL_ID` | `/v1/models` 列出且上游有权限的模型 ID |

配置块保存到标注路径，不作为 shell 命令执行。上一阶段失败时，先处理失败项，再执行依赖步骤。

## 2. 请求链路与凭据分工

```text
Claude Code ── HTTPS + 客户端 Key ──> Nginx:443
                                      │
                               127.0.0.1:8317 / CPA ──> Claude 上游
                                      │
                               observer:8318（可选）
                               用量 / 匿名剩余额度
```

| 凭据 | 用途 | 保存位置 |
| --- | --- | --- |
| SSH 私钥 | 管理 VPS | 管理员本机 |
| CPA 客户端 Key | 使用者访问模型、查询自身用量和匿名号池 | 使用者私密 env；VPS 鉴权配置 |
| CPA 管理 Key | 本机管理、observer 采集 | VPS 受限配置；不分发给普通使用者 |
| setup-token | CPA 上游模型调用 | VPS 认证目录 |
| profile/quota OAuth | 同账号身份和剩余额度查询 | VPS 独立凭据，禁用模型调度 |
| 上游 API Key | 使用 API 计费来源时访问上游 | VPS 受限 provider 配置 |

VLESS、SOCKS、HTTP CONNECT 是通用转发链路。它们连通不能代替模型生成验证。日常模型请求走 HTTPS，不需要 SSH 回调隧道。

## 3. 确认版本与文件约定

**位置：VPS。前提：明确新装或复用。完成：记录 Release、架构、模板和路径。**

本工具包面向 CPA v8，采用 `8.0.42` 的管理接口布局作为实现基线。部署时选择明确 Release，读取该 tag 的模板和帮助；不把 `config-version: 8` 当作二进制版本。[CPA Releases](https://github.com/router-for-me/CLIProxyAPI/releases)、[v8 配置说明](https://help.router-for.me/configuration/options)。

```bash
/opt/cli-proxy-api/cli-proxy-api --help
```

| 用途 | 旧布局 | v8 布局 |
| --- | --- | --- |
| 监听 | `host` / `port` | `server.host` / `server.port` |
| 客户端 Key | `api-keys` | `access.api-keys` |
| 认证目录 | `auth-dir` | `oauth.auth-dir` |
| 管理入口 | `remote-management` | `management` |

| 文件 | 新装默认路径 |
| --- | --- |
| 二进制 | `/opt/cli-proxy-api/cli-proxy-api` |
| 配置 | `/etc/cli-proxy-api/config.yaml` |
| 工作目录 / 认证目录 | `/var/lib/cli-proxy-api` / `/var/lib/cli-proxy-api/auth` |
| systemd | `/etc/systemd/system/cliproxyapi.service` |
| Nginx 站点 | `/etc/nginx/sites-available/cpa.conf` |

已有部署保留实际路径。自助脚本从运行中的 `cliproxyapi.service` 定位二进制，从管理接口读取认证目录；改 unit 名时同步修改脚本中的查找项。

## 4. 安装 CPA 与 systemd

### 4.1 安装依赖和二进制

**位置：VPS Bash。前提：新装目录和账号不存在；已有部署按第 11 节升级。完成：安装经过校验的二进制。**

从选定 Release 下载匹配 Linux amd64/arm64 的发布包，核对校验和再解压。下面的 `./cli-proxy-api` 是已校验文件。

```bash
sudo apt update
sudo apt install nginx certbot openssl python3 tzdata
sudo useradd --system --home-dir /var/lib/cli-proxy-api --shell /usr/sbin/nologin cpa
sudo install -d -m 755 /opt/cli-proxy-api
sudo install -d -m 750 -o cpa -g cpa /etc/cli-proxy-api
sudo install -d -m 700 -o cpa -g cpa /var/lib/cli-proxy-api
sudo install -d -m 700 -o cpa -g cpa /var/lib/cli-proxy-api/auth
sudo install -m 755 ./cli-proxy-api /opt/cli-proxy-api/cli-proxy-api
```

### 4.2 创建 Key 与配置

**位置：VPS 私密终端。条件：首次创建；已有文件不覆盖。完成：占位符替换，配置权限正确。**

```bash
umask 077
mkdir -p "$HOME/.config/cpa-admin"
(set -o noclobber; openssl rand -hex 32 > "$HOME/.config/cpa-admin/client-a.key")
(set -o noclobber; openssl rand -hex 32 > "$HOME/.config/cpa-admin/management.key")
```

通过私密编辑器或受保护配置渲染器，将 Key 写入 `/etc/cli-proxy-api/config.yaml`。不要把值放入命令参数、聊天或 Git。管理 Key 与客户端 Key 不同。

```yaml
config-version: 8
server:
  host: "127.0.0.1"
  port: 8317
access:
  api-keys:
    - "REPLACE_WITH_CLIENT_KEY"
oauth:
  auth-dir: "/var/lib/cli-proxy-api/auth"
management:
  allow-remote: false
  secret-key: "REPLACE_WITH_MANAGEMENT_KEY"
  disable-control-panel: true
observability:
  usage:
    usage-statistics-enabled: true
```

不用管理功能或 observer 时，将 `management.secret-key` 设为空以关闭管理接口；第 6.1 的自助工具需要管理接口和 observer。旧布局实例先完成版本迁移，不把两套字段混入同一文件。

```bash
sudo chown cpa:cpa /etc/cli-proxy-api/config.yaml
sudo chmod 600 /etc/cli-proxy-api/config.yaml
```

CPA 管理接口需要保存 Key 变更，因此服务用户必须能写入自己的配置文件；该目录不对其他用户开放。

### 4.3 创建并启动服务

**配置位置：`/etc/systemd/system/cliproxyapi.service`。完成：服务 active，且只监听回环。**

```ini
[Unit]
Description=CLIProxyAPI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=cpa
Group=cpa
WorkingDirectory=/var/lib/cli-proxy-api
ExecStart=/opt/cli-proxy-api/cli-proxy-api --config /etc/cli-proxy-api/config.yaml
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
```

**位置：VPS。**

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cliproxyapi
sudo systemctl is-active cliproxyapi
sudo ss -lntp
```

预期监听 `127.0.0.1:8317`。监听在公网地址时先修正。服务 active 不代表上游生成已通过。

## 5. 配置 HTTPS：域名与公网 IP 二选一

**位置：VPS，DNS 在域名管理平台。前提：80/443 冲突已解决。完成：客户端信任实际入口证书。**

### 5.1 分支 A：有域名

将 `api.example.com` 的 A 记录指向目标。仅在实际支持 IPv6 时添加 AAAA。将下面配置写入 `/etc/nginx/sites-available/cpa.conf`。

```nginx
server {
    listen 80;
    server_name api.example.com;
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
    }
    location / { return 301 https://$host$request_uri; }
}
```

**位置：VPS。条件：同名启用文件不存在。**

```bash
sudo install -d /var/www/letsencrypt
sudo ln -s /etc/nginx/sites-available/cpa.conf /etc/nginx/sites-enabled/cpa.conf
sudo nginx -t && sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/www/letsencrypt -d api.example.com
```

签发后在同一文件增加 HTTPS server。证书目录以 `certbot certificates` 为准。

```nginx
server {
    listen 443 ssl;
    server_name api.example.com;
    ssl_certificate /etc/letsencrypt/live/api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;
    client_max_body_size 128M;

    location /v1/ {
        proxy_pass http://127.0.0.1:8317;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Connection "";
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
    location / { return 404; }
}
```

### 5.2 分支 B：只有公网 IP

使用 `https://PUBLIC_IP`。Let's Encrypt 的 IP 证书有效期为 160 小时，必须自动续期；webroot IP 签发使用 Certbot 5.4+。[IP 证书](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)、[Certbot IP 签发](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)。

先按 5.1 启用 HTTP challenge 站点，把 `server_name` 改为 `PUBLIC_IP`，普通路径返回 404。

**位置：VPS。条件：替换实际 IPv4；Certbot 版本满足要求。**

```bash
certbot --version
sudo certbot certonly --preferred-profile shortlived \
  --webroot --webroot-path /var/www/letsencrypt --ip-address PUBLIC_IP
sudo certbot certificates
```

复用 5.1 的完整 HTTPS server，替换 `server_name` 和证书目录。IPv6 入口另核对监听地址与 URL 方括号。IP 证书不使用 DNS-01。公网连接不关闭证书校验。

### 5.3 共同步骤：加载、端口与续期

**位置：VPS。完成：Nginx 校验通过、续期调度可用、外部证书正确。**

```bash
sudo nginx -t && sudo systemctl reload nginx
```

防火墙和云安全组保留实际 SSH 管理端口，放行 443 和 HTTP-01 所需的 80；关闭公网 8317、8318、54545。域名使用自动 DNS-01 时可不依赖 80。

将下面 hook 保存为 `/etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh`，不覆盖已有业务 hook。

```sh
#!/bin/sh
set -eu
nginx -t
systemctl reload nginx
```

```bash
sudo chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
sudo systemctl enable --now certbot.timer
sudo systemctl list-timers --all
sudo certbot renew --dry-run
```

timer 命令用于已有 `certbot.timer` 的安装；snap/cron 检查其现有调度。单独确认 deploy hook 执行，再从外部检查证书。[Certbot 续期](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates)。

### 5.4 已有 443 / SNI 分流

公网 443 已由 `stream` 接收时，将 CPA HTTPS server 改为 `listen 127.0.0.1:4443 ssl;`，再把域名 SNI 路由接到内层。请求体限制仍放在内层 HTTP server。纯 IP 请求需明确默认后端；改变默认路由前确认现有业务影响。备份放在 Nginx include 目录之外。

## 6. 完成上游授权

已有凭据通过身份、真实生成及所需额度查询时直接复用。新增账号默认使用 setup-token 调用模型，并补齐同账号 profile/quota；普通 OAuth 是明确选择的备用分支。

### 6.1 默认：setup-token + 同账号资料授权

**位置：本机 macOS/Linux/WSL，浏览器也在该电脑。前提：CPA v8、管理接口、第 9.1 节 observer、SSH 和本机 Claude Code 已就绪。完成：工具报告模型、身份和额度三项通过。**

**本机，在仓库根目录运行：**

```bash
export CPA_TOOLS_CONFIG="$HOME/.config/cpa/deployment.json"
python3 scripts/cpa/cpa-tools/claude_auth.py init
python3 scripts/cpa/cpa-tools/claude_auth.py add
```

`init` 仅首次执行。按提示填写目标、私钥路径、HTTPS 根地址、原 env 路径和模型 ID。非 root SSH 用户需可非交互执行管理员 Python；配置中的 `remote_python` 使用 `sudo -n python3`。程序不修改 sudo 规则。

1. 输入账号邮箱和唯一代号。已有凭据文件名不覆盖。
2. 工具隔离本机 Claude 配置，启动 `claude setup-token`。确认本次实际回调监听后给出链接。
3. 用户在自己的隐私窗口打开链接，选择账号、完成邮箱验证并授权。工具不打开浏览器，不要求复制 token。
4. 工具自动捕获凭据，私密回传 VPS，以禁用状态导入。临时 CPA 仅加载新凭据，等待模型注册后执行真实生成。
5. 工具给出第二条 profile/quota 链接。用户选择同一账号。此步骤临时转发本机 `127.0.0.1:54545` 到 VPS 回调。
6. 校验资料邮箱、UUID 和实际 usage 窗口。用户确认两次选择同一账号。推理 token 本身不披露邮箱，身份关联不伪称为 token 自证。
7. 备份配置、两份凭据和 SQLite；绑定额度来源，保持辅助凭据禁用模型调度；只在全部通过后启用模型凭据。

两份凭据只算一个逻辑账号。setup-token 的长期有效期不增加订阅额度，也不保证不可撤销；实际到期信息由签发结果决定。当前标准 setup-token 的推理权限不包含 `user:profile`，额度由独立资料授权提供。[Claude setup-token](https://code.claude.com/docs/en/cli-reference)、[CPA v8 Claude OAuth 实现](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.42/internal/auth/claude/anthropic_auth.go)。

**中断恢复：本机，使用开始时显示的同一会话目录。**

```bash
python3 scripts/cpa/cpa-tools/claude_auth.py resume --session /PRIVATE/SESSION_DIRECTORY
```

回调进程退出后旧链接失效。恢复时先启动接收端，再使用新链接。浏览器成功页不是完整验收证据；以工具三项结果为准。详细操作见[自助 SOP](../scripts/cpa/cpa-tools/README.md)。

### 6.2 备用：VPS 普通 OAuth

**位置：本机 Bash/Zsh；登录进程在 VPS。条件：明确选择普通 OAuth，本机 54545 空闲。完成：凭据保存和第 8 节真实生成通过。**

```bash
ssh -t -i ~/.ssh/YOUR_PRIVATE_KEY -p SSH_PORT \
  -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:54545:127.0.0.1:54545 admin@VPS_HOST \
  'sudo -u cpa /opt/cli-proxy-api/cli-proxy-api --config /etc/cli-proxy-api/config.yaml --claude-login --no-browser'
```

Windows PowerShell 将命令写成一行，私钥换为实际 Windows 路径。保持终端运行，用户自行打开本次 URL。浏览器的 localhost 指这台电脑；不从另一台机器建隧道。完成保存后关闭临时会话。

本机 CPA 登录后上传也是可选分支：使用独立认证目录，只上传本次文件至 VPS 实际认证目录，设为 `cpa:cpa`、`600`；不复制整个个人目录。

### 6.3 已有上游 API Key

**位置：VPS 私密配置。条件：已确认 provider 模板。完成：真实生成通过。**

以下 v8 片段合并到现有配置，不替换 `access`、`oauth` 或 `server`：

```yaml
api-keys:
  claude:
    - name: claude-primary
      keys:
        - api-key: "REPLACE_WITH_UPSTREAM_CLAUDE_KEY"
```

顶层 `api-keys.claude` 是上游；`access.api-keys` 才是客户端。API 计费来源不等同于 Claude OAuth 订阅号池，后者的额度接口不提供 API 现金余额。

## 7. 在电脑上接入 Claude Code CLI

**位置：使用者电脑。前提：取得 HTTPS 根地址、自己的 Key 和模型 ID。完成：`/status` 与实际对话通过。**

按[官方安装说明](https://code.claude.com/docs/en/setup)安装，运行 `claude --version`。根地址不追加 `/v1`。以下按 shell 选择一组。

**macOS / Zsh：隐藏输入。**

```zsh
export ANTHROPIC_BASE_URL='https://api.example.com'
read -rs 'ANTHROPIC_AUTH_TOKEN?CPA API Key: '
print
export ANTHROPIC_AUTH_TOKEN
```

**Linux / Bash：**

```bash
export ANTHROPIC_BASE_URL='https://api.example.com'
read -rsp 'CPA API Key: ' ANTHROPIC_AUTH_TOKEN
printf '\n'
export ANTHROPIC_AUTH_TOKEN
```

**Windows / PowerShell：**

```powershell
$env:ANTHROPIC_BASE_URL = 'https://api.example.com'
$secureKey = Read-Host 'CPA API Key' -AsSecureString
$env:ANTHROPIC_AUTH_TOKEN = [System.Net.NetworkCredential]::new('', $secureKey).Password
```

管理员工具保存的原 env 可在 Bash/Zsh `source /PRIVATE/EXISTING/.cpa-env`，再将自己的 `CPA_API_KEY_CLIENT_A` 赋给 `ANTHROPIC_AUTH_TOKEN`。不把管理员整份 env 分发给用户。

环境变量影响当前终端和子进程。验证后再合并到个人 `~/.claude/settings.json` 的 `env`；Windows 为 `%USERPROFILE%\.claude\settings.json`。保留现有设置，不放进共享项目配置。`ANTHROPIC_AUTH_TOKEN` 使用 Bearer 鉴权；`/status` 核对实际来源。[网关配置](https://code.claude.com/docs/en/llm-gateway)。

### 7.1 推荐 1 小时提示词缓存

经常暂停 5–60 分钟再继续的长会话，推荐主会话使用 `1h`；持续密集运行的短子任务保留 `5m`。子任务也会长时间等待时，再把第二项改为 `1h`。Claude Code **v2.1.242+** 可将下面两项合并到个人 `~/.claude/settings.json` 顶层；保留原有 `env`、Key 和其他设置，不整份覆盖。Windows 路径见上节。[官方配置说明](https://code.claude.com/docs/en/prompt-caching#choose-the-ttl-yourself)

```json
{
  "promptCacheTtl": "1h",
  "subagentPromptCacheTtl": "5m"
}
```

设置被忽略时，先检查环境中的 `FORCE_PROMPT_CACHING_5M=1` 和更高优先级配置。经 CPA 转发时，网关还需保留 `anthropic-beta` 请求头及缓存参数；本节不改写服务器或既有客户端配置。[Claude Code 缓存与网关](https://code.claude.com/docs/en/prompt-caching#choose-the-ttl-yourself)

5 分钟缓存不是每隔 5 分钟固定清空：命中会刷新有效期。`1h` 缓存写入价格比 `5m` 高 60%，适合减少长停顿后的重写；持续高频使用通常选 `5m`。更换模型、上游账号或提示词前缀仍可能未命中。[缓存时长与费用](https://platform.claude.com/docs/en/build-with-claude/prompt-caching#1-hour-cache-duration)

用一次原本就要进行的调用观察 `usage.cache_creation.ephemeral_1h_input_tokens`；非零表示发生过 1 小时缓存写入。已经命中时，写入数可为零，结合缓存读取量判断，不额外发付费探测请求。observer 按上游返回的 `5m/1h` 写入量计价，不根据客户端推荐值猜测时长。CPA 透传与采集端升级见[缓存时长更新](../scripts/cpa/cpa-observer/CACHE_TTL_UPDATE.md)。[缓存用量字段](https://platform.claude.com/docs/en/build-with-claude/prompt-caching#1-hour-cache-duration)

## 8. 验证鉴权、模型与真实生成

**位置：已设置第 7 节环境变量的本机终端。前提：HTTPS 和上游就绪。完成：无 Key 401、有效 Key 200、真实生成含文本且退出码 0。**

仓库提供[独立验证脚本](../scripts/cpa/cpa_verify.py)，只输出状态、模型 ID 和是否含文本，不输出 Key、token 或回答正文。真实生成消耗少量用量。安装脚本与验证脚本分开，避免误触发。

```bash
python3 scripts/cpa/cpa_verify.py --list-models
export CPA_TEST_MODEL='REPLACE_WITH_MODEL_ID'
python3 scripts/cpa/cpa_verify.py
# 完整验证退出码为 0 后执行：
claude --model "$CPA_TEST_MODEL"
```

PowerShell 用 `py -3` 或实际 Python 命令，模型设置为 `$env:CPA_TEST_MODEL`。`--list-models` 只验证鉴权和列表，不验证生成。退出码 1 表示验证失败，2 表示输入条件缺失。Claude Code 再执行 `/status` 并进行真实对话。

每个分发的客户端 Key 分别验证鉴权。要求按 Key 计量时，逐 Key 发送可归属请求并查询第 9 节数据。

## 9. 持久用量与剩余额度

### 9.1 部署 observer

**位置：VPS。前提：CPA v8 管理 Key 和统计队列已启用。完成：observer 只监听回环、保存 SQLite。**

observer 是本仓库的原生 Python 配套服务，不是 CPA 内置账本。只运行一个队列消费者。[源码和数据口径](../scripts/cpa/cpa-observer/README.md)。

将仓库 `scripts/cpa/cpa-observer/` 上传到 VPS 的私密准备目录，在该目录执行。已有账号/目录/配置时先复用。

```bash
sudo useradd --system --home-dir /var/lib/cpa-observer --shell /usr/sbin/nologin cpa-observer
sudo install -d -m 755 /opt/cpa-observer
sudo install -d -m 750 -o root -g cpa-observer /etc/cpa-observer
sudo install -d -m 700 -o cpa-observer -g cpa-observer /var/lib/cpa-observer
sudo install -m 644 observer.py prices.json /opt/cpa-observer/
sudo install -m 644 cpa-observer.service /etc/systemd/system/
```

私密生成 `/etc/cpa-observer/config.json`。`management_key` 是原始管理 Key，不是 CPA 保存后的哈希；`hmac_secret` 安全随机生成后永久保留。

```json
{
  "cpa_base_url": "http://127.0.0.1:8317",
  "management_key": "REPLACE_WITH_MANAGEMENT_KEY",
  "hmac_secret": "REPLACE_WITH_PERSISTENT_RANDOM_SECRET",
  "database": "/var/lib/cpa-observer/usage.sqlite",
  "prices_file": "/opt/cpa-observer/prices.json",
  "labels": {},
  "quota_bindings": {},
  "usage_poll_seconds": 3,
  "quota_poll_seconds": 300,
  "port": 8318
}
```

```bash
sudo chown root:cpa-observer /etc/cpa-observer/config.json
sudo chmod 640 /etc/cpa-observer/config.json
sudo systemctl daemon-reload
sudo systemctl enable --now cpa-observer
sudo systemctl is-active cpa-observer
sudo ss -lntp
```

只在实际 CPA HTTPS server 内加入下列精确路由，保留 `/v1/` 模型路由。已有 SNI 分流时放在内层 HTTP server。

```nginx
location = /v1/quota {
    proxy_pass http://127.0.0.1:8318;
    proxy_set_header Authorization $http_authorization;
    proxy_buffering off;
    proxy_read_timeout 35s;
}
location = /v1/usage {
    proxy_pass http://127.0.0.1:8318;
    proxy_set_header Authorization $http_authorization;
    proxy_buffering off;
    proxy_read_timeout 35s;
}
```

备份站点后执行 `nginx -t`，通过才 reload。不要公开 `/healthz`、管理接口或 8318。已有 CPA 配置满足要求时，不为安装 observer 重启 CPA。

### 9.2 管理员 TUI 与 Key 管理

**位置：本机，仓库根目录。前提：第 6.1 的私密配置已建立。**

```bash
python3 scripts/cpa/cpa-tools/api_keys.py
```

| 操作 | 按键 / 行为 |
| --- | --- |
| 选择与明细 | ↑↓ / Enter |
| 新增、改名、换 Key、删除、导出 | `n` / `e` / `k` / `x` / `s` |
| 今日 / 本月 / 全部用量 | `1` / `2` / `3`，北京时间 |
| 账号剩余额度 | `g`，邮箱对应各额度窗口 |
| 筛选 / 历史 / 更新 / 退出 | `/` / `h` / `r` / `q` |

所有页面共用黑底主题。剩余额度条越满代表剩余越多；绿色 >50%，黄色 >20%，红色 ≤20%，旧值变灰，未知值显示“未知”。页面保留百分比、快照时间和重置时间。重置时间后接短倒计时，例如 `10-28 08:00 (2d3h left)`；不足一天显示 `3h20m left`，不足一小时显示 `20m30s left`。TUI 倒计时在本机更新，不发起网络请求；CLI 显示查询时的倒计时。

工具沿用首次部署的同一 env，新变量采用 `export CPA_API_KEY_<名称>=...`，新 Key 使用 `sk-cpa-` 加随机串。已有 Key 不因整理写法而换发。新增、修改、删除成功后显示一次 env 绝对路径。删除保留用量历史；换 Key 后旧历史单列。详见[自助 SOP](../scripts/cpa/cpa-tools/README.md)。

### 9.2.1 可选的账号权限管理

部署 [cpa-admin 权限网关](../scripts/cpa/cpa-admin/README.md)后，管理员可在原 HTTPS 域名 `/admin/` 或 TUI 的 `p` 页，为每个 Key 勾选允许使用的账号。`b` 生成短时网页登录链接；独立管理员密码通过 SSH 设置。新建或轮换 Key 默认无账号权限，需要重新授权；改名保留权限。所有额度查询仍经权限网关筛选，不能直接公开 observer 来绕过筛选。

### 9.3 使用者只用 Base URL + Key 查询

**在使用者本机运行。** 准备自己的 HTTPS Base URL、API Key，并打开本机 VPN。查询脚本使用 HTTP/mixed 代理端口，例如 `7890`。

运行[首页一键命令](../README.md#cpa-一键查询)，在终端表单填写 **代理端口、Base URL、API Key**。Windows 和 Linux/macOS 的操作相同：Tab 或方向键切换字段，回车确认，空格切换复选框，Esc 退出。Key 隐藏输入。Base URL 须为 HTTPS；末尾的 `/v1` 由脚本自动处理。

#### 保存连接配置

表单中的“记住连接（含 Key）”默认不勾选。勾选后，首次查询成功才保存到 `~/.cpa/query.json`。下次启动可用方向键选择“载入已有配置”或“重新配置”。选择载入时，启动器用保存的代理下载已校验的客户端，再读取已存 Base URL 和 Key；不重复询问端口。Key 失效或代理端口改变时，选择重新配置。

配置目录为 `~/.cpa`，Windows 对应 `%USERPROFILE%\.cpa`。文件只保存版本、代理地址（含端口）、Base URL、API Key。Key 以明文存入本机私密文件：Linux/macOS 目录权限为 `700`、文件为 `600`；Windows ACL 仅允许当前用户。不要上传或分享该目录。保存失败保留旧配置；不读取符号链接或允许其他用户访问的配置。删除 `query.json` 可清除已存连接信息，不撤销服务器上的 Key。

未同意保存、查询失败或无交互终端时不写配置。直接运行查询脚本时，显式连接参数或环境变量继续使用；无参数交互启动才显示配置菜单。个人查询配置与管理员原 `.cpa-env`、`CPA_TOOLS_CONFIG` 相互独立，不迁移或替换原管理配置。

#### 查看用量

看板分为总览、模型明细、账号额度三个视图。总览同时显示当前 Key 的近 1 小时、7 天、30 天和历史用量，并保留主要额度条与模型费用。模型明细默认展示近 1 小时，按估算费用从高到低排列。账号额度页显示全部匿名账号的余量、重置时间和数据更新时间。

额度条越满，剩余越多。绿色 >50%，黄色 >20%，红色 ≤20%；旧值、禁用和未知额度用灰色。若终端设置了非空的 `NO_COLOR`，默认使用单色，按 `C` 可在本次运行中切换。

| 操作 | 按键 |
| --- | --- |
| 切换总览、模型明细、账号额度 | `Tab` / `Shift+Tab` 或 `←` / `→` |
| 查看近 1 小时 / 7 天 / 30 天 / 历史的模型明细 | `1` / `2` / `3` / `4` |
| 翻页，总览每页 3 个模型，明细每页 5 个 | `N` / `P` |
| 滚动内容 | `↑` / `↓`、`PgUp` / `PgDn`；`Home` / `End` 到顶部 / 底部 |
| 切换彩色 / 单色 | `C` |
| 刷新 | `R` 或回车 |
| 退出 | `Q` |

看板每 5 分钟后台刷新；`R` 或回车立即刷新，每轮分别查询一次用量和账户池接口。查询不会重叠，期间可继续切换视图，关闭工具后停止查询。切换、翻页、滚动和缩放使用已有数据。表中的 K/M/B/T 表示千、百万、十亿、万亿；具体统计含义见下一节。

#### 已有查询端检查与更新

PR #6 更新个人查询端：根目录 `cpa.sh` / `cpa.ps1` 是下载启动器，`scripts/cpa/quota.sh` / `quota.ps1` 是查询程序。它们与管理员的 `cpa-tools/api_keys.py`、VPS 上的 `cpa-admin` / `cpa-observer` 独立更新。记录最新 main 的完整 commit，再核对实际运行路径、解释器、源码与接口；仓库有新提交不代表 VPS 需要升级。

先用已有 Key 经指定代理检查 `/v1/usage?period=overview` 和 `/v1/quota`：总览需含 1h、7d、30d、all 四窗口及各自 `models`；号池保留 `scope=shared_pool`、`visibility=authorized_accounts` 和调用者白名单过滤。接口满足要求时，只更新查询源码，保留 `~/.cpa/query.json` 的内容、所有者和权限，不部署或重启 VPS，不替换管理员工具。若缺少接口能力，先报告证据、最小修复与源码回滚方式，取得确认后再按 [admin 既有服务修复](../scripts/cpa/cpa-admin/README.md#既有服务修复)和 [observer 维护说明](../scripts/cpa/cpa-observer/README.md#operation)处理。

验收使用隔离配置与合成 Key，检查三个视图、四个时段、模型费用、缓存命中率、彩色及灰色额度条。真实终端中验证 Tab、C、隐藏输入、取消和保存。缩小再放大窗口后，应在**不按任何键**时重绘；切换和缩放不得增加 API 请求，R/回车刷新只增加两次请求。Windows 启动器和表单也需验证空闲缩放。使用现有账户做公网只读查询，不调用付费模型；未实测的平台单独说明。

客户端源码变更后，先提交客户端，再把两个启动器与本文固定链接更新到该完整 commit，重算各自 SHA256。Bash 校验去除末尾换行后的下载内容，PowerShell 校验原始字节。回滚只需从此前记录的 Git commit 恢复启动器和对应查询源码，保留连接配置；不恢复 VPS 数据库或重启服务。

#### 代理设置

首页启动器的首次下载使用当前网络。输入端口后，脚本通过指定代理下载固定版本客户端、校验 SHA256，并查询 API。

在 Clash 的高优先级规则中，将 CPA 域名交给代理节点，例如 `DOMAIN,api.example.com,节点选择`，再到连接页确认实际使用的节点。脚本使用本机 HTTP 代理 `http://127.0.0.1:端口` 或 `http://[::1]:端口`。

代理失败时查询停止，HTTPS 证书校验始终开启。查询会忽略 `NO_PROXY` 绕过设置并拒绝重定向。纯 SOCKS 端口请改用 Clash 的 HTTP/mixed 端口。

<details>
<summary>其他启动方式：本地文件与全程代理下载</summary>

已下载仓库时，在仓库根目录运行。

**Linux / macOS**：Bash、Python 3.10+。

```bash
bash scripts/cpa/quota.sh
```

**Windows**：PowerShell 5.1 或 7。脚本文件运行遵循本机执行策略。

```powershell
.\scripts\cpa\quota.ps1
```

如果首次下载也要使用代理，运行以下固定版本命令。

**Linux / macOS**：另需 curl。

```bash
read -r -p '本机 HTTP/mixed 代理端口: ' port
proxy="http://127.0.0.1:$port"
script="$(curl -fsS --proxy "$proxy" --noproxy '' --proto '=https' --max-time 60 https://raw.githubusercontent.com/Killow1998/VPS-Maintenance-Documentation/87571c98f4e533d2d28caef4fdfc7b74f12408a2/scripts/cpa/quota.sh)" &&
  printf '%s\n' "$script" | CPA_PROXY="$proxy" bash -s --
```

**Windows**：

```powershell
& {
    $ErrorActionPreference = 'Stop'
    $port = Read-Host '本机 HTTP/mixed 代理端口'
    if ($port -cnotmatch '^[0-9]{1,5}$' -or [int]$port -lt 1 -or [int]$port -gt 65535) { throw '端口需为 1–65535' }
    $proxy = "http://127.0.0.1:$port"
    $source = Invoke-RestMethod -UseBasicParsing -Proxy $proxy -MaximumRedirection 0 -TimeoutSec 60 -Uri 'https://raw.githubusercontent.com/Killow1998/VPS-Maintenance-Documentation/87571c98f4e533d2d28caef4fdfc7b74f12408a2/scripts/cpa/quota.ps1'
    & ([scriptblock]::Create($source.TrimStart([char]0xFEFF))) -ProxyUrl $proxy
}
```

</details>

#### 单次查询与自动化

Bash 加 `--once`，PowerShell 加 `-Once`，查询一次后退出。默认输出近 1 小时的全部模型，也可指定以下选项：

| 查询内容 | Bash 参数 | PowerShell 参数 |
| --- | --- | --- |
| 仅自己的四段总览 | `--view usage` | `-View usage` |
| 仅号池剩余额度 | `--view quota` | `-View quota` |
| 近 1 小时明细与计价 | `--period 1h` | `-Period 1h` |
| 近 7 天 / 30 天明细 | `--period 7d` / `--period 30d` | `-Period 7d` / `-Period 30d` |
| 本月用量 | `--period month` | `-Period month` |
| 全部已采集用量 | `--period all` | `-Period all` |
| 指定某日 | `--period day --date 2026-10-26` | `-Period day -Date 2026-10-26` |
| 今日最近 20 条调用 | `--period day --recent 20` | `-Period day -Recent 20` |

选择具体期间后，可查看模型、匿名上游账号汇总和最近 10 条执行记录。`--recent` / `-Recent` 接受 0–50，设为 0 时仅显示汇总。同一个 `account-…` 编号在用量与额度页对应同一上游账号。请求重试可能产生多条执行记录。

已有环境变量时，用 `--key-env CPA_API_KEY_CLIENT_A` 或 `-KeyEnv CPA_API_KEY_CLIENT_A` 读取 Key。Base URL 和代理分别可由 `CPA_BASEURL`、`CPA_PROXY` 提供。自动化运行须提供完整连接信息，并使用单次查询模式；真实 Key 保存在环境变量中。

参数加在脚本调用后；上方下载命令中，分别加在 Bash 的 `bash -s --` 后或 PowerShell 的脚本调用行末尾。具体期间和单视图查询使用单次模式。

#### 兼容与故障处理

| 现象 | 处理 |
| --- | --- |
| 401 | 核对 API Key 是否有效 |
| “查询参数不受支持”或缺少模型明细 | 管理员按 [observer 更新说明](../scripts/cpa/cpa-observer/README.md#repair-prompt)检查运行版本 |
| HTTP 200 但提示“数据范围错误，请检查 observer 路由” | 检查 JSON 的 `scope`：额度必须为 `shared_pool`，个人用量为 `own_key`。若网关按 Key 筛选账号，仍保留 `shared_pool` 并增加 `visibility: authorized_accounts`，不能把公网入口改回未筛选的 observer 来消除报错 |
| 数据为零、未知或过时 | 检查采集开始时间、最后采集时间与采集状态 |
| 代理连接失败 | 核对 Clash 端口，并检查 CPA 域名所用节点 |

旧 observer 可先用 `--period day` / `-Period day` 查询今日数据。服务更新保留原数据库、HMAC、Key 和路由，仅替换并重启必要的 observer 服务。

完整工具包中的旧 `api_keys.py quota` 入口按原网络设置运行。需要强制代理查询时，使用本节的 `quota.sh` 或 `quota.ps1`。API 字段与管理员邮箱查询方式见 [observer 文档](../scripts/cpa/cpa-observer/README.md)和[管理员工具手册](../scripts/cpa/cpa-tools/README.md)。

### 9.4 用量与费用口径

| 指标 | 含义 |
| --- | --- |
| 当前 Key 用量 | 这个 Key 已被采集的调用记录 |
| token | 模型处理文本等内容时使用的计量单位 |
| 缓存命中率 | 输入中复用缓存的比例。例如 100 个输入 token 中有 80 个来自缓存，命中率为 80% |
| 美元估算 | 根据每次请求记录的模型价格与 token 数计算，实际支出以服务商账单为准 |
| 计价覆盖数 | 能够估算费用的请求数 / 全部请求数；费用前的 `*` 表示只统计了部分请求 |
| 账户池余量 | 共享上游账号各额度窗口的剩余百分比与重置时间 |

**时间范围。** 近 1 小时、7 天、30 天都从同一查询截止时间向前计算；日历日报和月报使用北京时间 `Asia/Shanghai`。历史范围取决于数据库保留的记录。各时间范围重叠，应分别查看。账户池按自己的采集周期更新，页面标有更新时间。

**缓存。** 输入总数由普通输入、缓存读取和缓存写入组成；缓存命中率取该期间缓存读取总数占输入总数的比例。没有输入或计量字段缺失时，显示“未知”。

**费用。** 普通输入、缓存读取、缓存写入和输出分别计价，输出包含推理 token。金额显示单个美元估算值；缓存写入按上游报告的 5 分钟和 1 小时用量分别计算。时长未采集的旧记录按 1 小时费率估算，金额前标 `~`，不重写旧记录。缺少价格或计量信息的请求标为未知。价格在入库时固定，后续调价不改变历史金额。升级方法见[缓存时长更新](../scripts/cpa/cpa-observer/CACHE_TTL_UPDATE.md)，价格依据见 [Anthropic 官方价格](https://platform.claude.com/docs/en/about-claude/pricing)。

**记录范围。** observer 从部署后开始采集。内存队列到 SQLite 写入之间发生故障时，可能丢失记录，因此该工具用于用量观察，费用核对以服务商账单为准。服务端只运行一个队列消费者，维护时保留数据库。

### 9.5 使用者试用清单

1. 运行[一键查询](../README.md#cpa-一键查询)，输入代理端口、Base URL 和自己的 Key。在 Clash 连接页确认 CPA 域名使用了代理节点。
2. 检查四个时间范围、模型明细及账户池余量；试用切换、翻页、刷新和退出。
3. 检查需要的单次查询选项，例如 `--period 7d` / `-Period 7d`。
4. 完成一次原本就要进行的模型调用，等待采集更新，再核对请求和 token 增量。重试可能增加执行记录数。
5. 测试代理故障时，可在查询命令中指定一个确认未监听的本机端口；查询应报连接错误。
6. 反馈时提供系统版本、选项、错误状态和采集时间，隐去 Key、邮箱及私有地址。

## 10. 常见故障：定位失败层

| 表现 | 优先核对 |
| --- | --- |
| 401 | Key 配置、鉴权头、Claude Code 实际配置来源 |
| unknown provider | 精确模型 ID、CPA 版本、可用凭据和别名 |
| 413 / Request too large | Nginx、其他 gateway、CPA、上游分别的 body limit |
| 502 | CPA 服务、`proxy_pass` 与监听 |
| 504 / 等待 | 上游网络、额度、超时、SSE 缓冲 |
| localhost 回调失败 | 浏览器所在电脑、临时转发、登录进程是否存活 |
| 登录成功但无法生成 | 实际认证目录、文件权限、上游权限 |
| TLS 错误 | DNS、SAN、证书链、期限和 SNI |
| TUI 查询慢 | 首次 SSH、observer 最近采集时间；额度页 `r` 只更新快照 |

Nginx `client_max_body_size` 默认 1m，超限返回 413；`128M` 只调整 Nginx，不改变上游限制。不能用客户端“32MB”文字直接定位拒绝层。[Nginx 指令](https://nginx.org/en/docs/http/ngx_http_core_module.html#client_max_body_size)。

**位置：VPS 私密终端。输出先脱敏再分享。**

```bash
sudo systemctl is-active cliproxyapi nginx cpa-observer
sudo nginx -t
sudo ss -lntp
sudo journalctl -u cliproxyapi --since '30 minutes ago' --no-pager
```

不公开原始日志、配置、OAuth JSON、完整环境变量或 `curl -v`。

## 11. 维护、回滚与交付验收

### 11.1 续期与凭据

检查实际证书期限、续期任务和 reload hook。setup-token 失效时换发模型凭据并重验生成；资料来源有效时复用，失效时单独补授权。observer 遇额度来源 401 只定向刷新一次；403 缺 scope 需授权，429 按上游等待时间退避。

换发不是 `add` 覆盖：先备份，保留稳定代号和绑定，替换目标角色后验收，不让同一角色新旧凭据同时调度。自助工具的 `resume` 用于未完成新增会话，不代替已上线账号的凭据替换。

### 11.2 升级与回滚

仓库发布、管理员电脑脚本和 VPS 运行服务是三个独立版本。先按[同步边界与只读验收](../scripts/cpa/cpa-admin/README.md#仓库本机与-vps-的同步边界)记录完整 commit，逐文件核对实际运行路径。确认运行进程已加载目标版本，且接口验收通过时，无需重启；若源码在进程启动后被替换，应按受影响范围重启并验收。不要重跑首次部署来更新既有网关。网页入口使用 `/admin/`，Base URL 保持域名根路径。

1. 记录版本、路径和状态，备份二进制、配置、unit、Nginx、必要凭据。SQLite 用 backup API，不单拷活动 WAL 主文件。
2. 阅读目标 Release 迁移说明，确认管理接口和 observer 兼容性。
3. 替换匹配文件，按受影响范围重启；Nginx 只在 `nginx -t` 通过后 reload。
4. 执行第 8 节及受影响验收。失败时恢复配套版本和配置，不用旧数据库覆盖新增用量。
5. 回滚后再次验证模型、HTTPS 和已有业务。不要恢复已撤销 refresh token。

### 11.3 交付验收表

| 检查项 | 通过标准 | 保留证据 |
| --- | --- | --- |
| 服务隔离 | CPA active、回环 8317；公网不能直连 8317/8318/54545 | 服务、监听和授权外部检查 |
| HTTPS / 续期 | 客户端信任证书；调度、dry-run、hook 通过 | 名称、期限和执行结果 |
| 客户端鉴权 | 无 Key 401；每个有效 Key models 200 | 代号和状态码 |
| 真实生成 | `generation_pass: true`，退出码 0 | 模型、时间、状态 |
| Claude Code | `/status` 正确、真实对话成功 | 脱敏状态；人工确认单列 |
| 用量（如要求） | 逐 Key 请求归属正确，重启采集器后记录仍在 | 数量和查询结果 |
| 剩余额度（如要求） | 匿名公开、邮箱仅管理员可见，身份/模型/窗口通过 | HTTP 状态、覆盖数量、时间 |
| 恢复 | 私密交付 Key 获取位置，备份和恢复路径可用 | 路径和未完成项 |

状态分别记为“已验证”“由用户确认”“未验证”“不适用”，不把服务 active 或模型列表当作生成成功。

## 12. Agent 执行约定与文档维护

### 12.1 协作规则

| ID | 执行要求 |
| --- | --- |
| A01 授权 | 阅读/改文档不操作 VPS；已授权范围内完成工作，不重复确认同一授权 |
| A02 先读后问 | 先查会话和获准环境，集中询问缺失目标/权限；不猜主机或账号 |
| A03 分支 | 新装/复用、域名/IP、双授权/普通 OAuth/API Key 按实际条件选择 |
| A04 顺序 | 准备→版本→安装→HTTPS→授权→客户端→验证→交付；失败先停依赖步骤 |
| A05 人工 OAuth | 账号持有人选择账号、验证并授权；工具只给有效链接，保持回调进程 |
| A06 秘密 | 不回传 Key/token/密码/code/state；配置 600，服务组读取可 640，认证目录 700 |
| A07 重试 | 复用已有服务和凭据，只重试定位过的失败项，不重建有效账号 |
| A08 交付 | 按验收表给证据和恢复路径；清理本次临时文件，保留必要私密备份 |

### 12.2 任务与报告模板

```text
请按本指南在我指定的 VPS 部署 CPA，并配置指定电脑的 Claude Code。
先检查现状，只询问缺失信息，保护已有业务，使用原生二进制。
OAuth 由我在隐私窗口完成，请给链接，不打开浏览器，不让我中转 token。
完成鉴权、真实生成、客户端对话、HTTPS、续期及指定的计量/额度验收后交付。
```

以下是脱敏执行记录，不是 CPA 配置；实际记录放在仓库外。

```yaml
task: cpa_claude_deployment
target_alias: null
authorization_scope: null
release: null
config_layout: null
entry_mode: null
upstream_auth_mode: null
client_ids: []
checks:
  service_and_loopback: {status: pending, evidence: null}
  tls_and_renewal: {status: pending, evidence: null}
  each_client_key: {status: pending, evidence: null}
  real_generation: {status: pending, evidence: null}
  claude_code_session: {status: pending, evidence: null}
  persistent_metering: {status: pending, evidence: null}
  shared_quota: {status: pending, evidence: null}
rollback_reference: null
secret_delivery_reference: null
blockers: []
next_action: null
```

状态仅使用 `pending`、`passed`、`failed`、`user_confirmed`、`not_applicable`。工作记录使用 `Asia/Shanghai`。

### 12.3 维护记录与来源

2026-10-26（Asia/Shanghai）：按“准备—部署—授权—使用—维护—Agent 交付”整理；加入原生 observer、双授权、Key TUI、用户匿名/管理员邮箱额度视图。工具包不包含真实主机、Key、OAuth、账号名单或数据库。

界面经离线预览，额度读取经已有部署只读计时；新机安装、生产 Key 写入、双授权组合和故障回滚没有在本次发布中执行。部署到新服务器时按第 11 节独立验收。

2026-10-26（Asia/Shanghai）：增加独立 Bash / PowerShell 匿名额度查询脚本和固定提交的一条命令入口。初次提交完成 Bash 与内嵌 Python 语法、帮助入口、管道执行、隐藏输入和已有服务只读查询；输出不含 Key 或邮箱。PR #1 合并时补充 PowerShell 7.6 离线接口测试，覆盖单次查询、不跟随重定向、无效 HTTP 入口拒绝和错误正文隐藏；测试不连接生产服务。Windows PowerShell 5.1 的文件语法和内存入口离线查询通过；本机文件执行策略不允许直接运行未签名脚本，不修改该策略，使用上方内存命令入口。

2026-10-26（Asia/Shanghai）：PR #2 补齐按 Key 隔离的匿名调用账号汇总与近期明细，独立脚本默认合并个人用量和号池余量。贡献者报告 17 项 observer 检查通过、其已有服务更新后历史数量保持、真实管道隐藏输入与只读组合查询通过；提交时新增 PowerShell 用量视图尚未执行。合并方独立通过 17 项离线测试，并在 PowerShell 5.1/7.6 用模拟接口验证 all/usage/quota 三个视图、请求数量、鉴权头和敏感字段不显示；没有真实网络请求。此预检不代表复验了贡献者的生产环境；实际使用由使用者按 9.5 验收，未因此更新运行中的 VPS。

2026-10-26（Asia/Shanghai）：独立查询客户端默认总览，强制显式本机代理，并提供可刷新终端模式。observer 增加同一截止时间和 SQLite 快照的 1h/7d/30d/总历史汇总，以及滚动期间明细。20 项 observer 测试及 10 项客户端测试通过，后者包含 PowerShell 5.1/7 的本地 CONNECT 测试、NO_PROXY 绕过防护、失败拒绝和合成总览；PowerShell 7 的伪终端验证同屏、刷新、退出。Bash 语法通过，Python 查询核心经本地测试；macOS/Linux 真实 TUI 尚未实机验收。无生产 API 请求、VPS 部署或数据库迁移。旧 API 保持兼容；新总览需要管理员升级现有 observer，见其维护说明。

2026-10-26（Asia/Shanghai）：Bash 查询改为无参数交互启动，按端口、HTTPS 入口、隐藏 Key 的顺序收集信息，默认进入总览；增加 `--once`，保留原有参数自动化方式。统一 XBoard/CPA Python 运维工具最低版本为 3.10，并在主要运行入口先检查版本。16 项客户端及版本检查、27 项 XBoard 和 20 项 observer 离线测试通过；实际测试解释器为 Windows Python 3.11.14，另检查 3.10 语法及低版本拒绝路径，不冒充在 3.10 上实跑。Bash 语法和帮助入口通过；本机无已安装 WSL，Linux/macOS 真实交互仍待使用者验收。没有升级生产 Python、部署 observer 或修改业务数据。

2026-10-26（Asia/Shanghai）：按多端一致要求，PowerShell 5.1/7 也支持无参数启动，按端口、HTTPS 入口、隐藏 Key 交互填写后默认进入总览；增加 `-Once`，端口和 URL 格式错误可重输。18 项客户端及版本检查通过，覆盖两版 PowerShell 的模拟交互、单次模式、无终端拒绝和真实回环 CONNECT；PowerShell 7 实际伪终端完成无参数启动、遮掩 Key、R 刷新、Q 退出，接口使用合成返回，无生产查询。Windows 临时验收脚本已删除。

2026-10-25（America/Los_Angeles）：首页新增 Linux/macOS 与 Windows 各一行启动入口；最初公开启动器下载允许使用当前网络，后续固定版本客户端下载和 API 查询强制代理，执行前核对 SHA256。25 项客户端/启动器测试及 20 项 observer 测试通过；PowerShell 实际终端经代理下载并通过校验后到达 Base URL 提示，未输入真实 Key。Bash 在本机 Git Bash 完成语法、模拟执行、真实下载及校验；串接 Windows Python 时其终端检测拒绝继续，未将此当作 Linux/macOS 实机交互验收。另补齐旧 observer 总览 400 的说明和[可转发修复 Prompt](../scripts/cpa/cpa-observer/README.md#repair-prompt)。没有更新生产 observer 或修改任何 VPS。

2026-10-26（America/Los_Angeles）：四段总览补充 USD 标价估算、计价覆盖及 token 加权缓存命中率；observer 返回每个窗口的模型分项，复用现有聚合和数据库列，不改 schema。Linux/Windows 查询支持在当前快照切换模型窗口及翻页，无额外请求；每次刷新仍为两个只读接口。已有具体期间查询也补齐模型表。费用只累加入库快照，未知价格/计量不补零，部分计价标 `*`；输入含缓存、输出含 reasoning，不重复收费。27 项客户端/启动器和 21 项 observer 测试通过，覆盖跨 Key 隔离、多模型成本、未知计量、历史价格不重算、代理拒绝及 PowerShell 5.1/7 交互；PowerShell 7 真实终端使用合成返回验证切换、翻页、刷新与退出。Bash 语法及内嵌 Python 验证通过；Linux/macOS 真实终端和生产数据仍待目标使用者验收。启动器及固定下载命令更新到源码 `8335db5d27211d6af37704db85ec8efb0172b5c0` 并重新核对哈希。维护 Prompt 已补充模型/计价/缓存验收；本轮没有升级 VPS、改 prices.json、调用付费模型或写入任何生产用量。
