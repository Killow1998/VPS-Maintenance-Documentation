# VPS + CLIProxyAPI + Claude Code

在 Linux VPS 上部署 CLIProxyAPI（CPA），通过 Nginx 提供 HTTPS 入口，让电脑上的 Claude Code CLI 使用独立客户端 Key 调用 Claude。服务端采用原生二进制与 systemd，无需 Docker。

适用环境：具备 sudo 权限的 Debian/Ubuntu VPS；客户端为 macOS、Linux 或 Windows。Python 运维脚本统一要求 Python 3.10+，与 XBoard 工具一致；分别检查本机和 VPS 的实际解释器，低版本会在读取凭据和执行操作前退出。自助双授权与 TUI 使用 macOS/Linux，Windows 使用 WSL；独立 PowerShell 查询脚本无需 Python。本文是部署步骤和配置模板，不是整篇粘贴执行的脚本。

| 读者 | 阅读路径 |
| --- | --- |
| 部署者 | 按 1—8 节完成部署和真实请求验证。使用双授权工具时，先完成 9.1 的 observer 准备。 |
| 仅使用服务的人 | 取得入口、自己的客户端 Key 和模型 ID，阅读 7—8 节；9.3 可查自身用量、近期调用和匿名剩余额度。 |
| 管理员 | 9.2 管理 Key、查看用量和账号剩余额度；10—11 节用于维护。 |
| 执行任务的 Agent | 先读第 12 节，再执行对应部署步骤；第 11 节定义交付标准。 |

“VPS”指远端服务器，“本机”指运行浏览器或 Claude Code 的电脑。代码块前标明位置、用途和条件。占位符未补齐时不执行。本文示例不代表目标服务器已部署成功。

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

### 9.3 使用者只用 Base URL + Key 查询

**位置：使用者本机。无需 SSH、管理员配置或 OAuth token。API 查询必须指定本机 HTTP/mixed 代理，不能直连。**

临时查询使用独立 [quota.sh](../scripts/cpa/quota.sh) 或 [quota.ps1](../scripts/cpa/quota.ps1)，无需安装 pip 包。先在 Clash/FlClash 中确认 HTTP/mixed 监听端口；`7890` 只是示例。纯 SOCKS 端口不能用作这里的 HTTP 代理。

无需克隆、运行后再输入三项信息的最短入口见[首页 CPA 一键查询](../README.md#cpa-一键查询)。首次公开启动器下载使用当前网络，之后强制代理下载固定客户端、验证 SHA256，再查询 API。下面保留已取得文件的运行方法，以及连首次下载也强制指定代理的完整命令；两种入口不需要同时运行。

**macOS / Linux：Bash 和 Python 3.10+。已取得新版仓库时，在仓库根目录执行：**

```bash
bash scripts/cpa/quota.sh
```

脚本依次询问 **代理端口 → HTTPS Base URL → API Key（隐藏输入）**，默认进入可刷新的总览。端口必须是 1–65535；URL 必须是 HTTPS，末尾 `/v1` 会自动规范化。端口或 URL 格式错误时可重新输入，Ctrl+C 取消。连接信息和 Key 不写文件。只取得独立文件时，直接 `bash quota.sh`，无需克隆整个仓库。

**首次不落盘启动：还需要 curl。下载脚本本身也必须先知道代理端口，所以这一步在下载前询问一次，再交给脚本复用；不会直连下载。**

```bash
read -r -p '本机 HTTP/mixed 代理端口: ' port
proxy="http://127.0.0.1:$port"
script="$(curl -fsS --proxy "$proxy" --noproxy '' --proto '=https' --max-time 60 https://raw.githubusercontent.com/Killow1998/VPS-Maintenance-Documentation/1a1a9188f03e2937c86cabbb721376e04225a1eb/scripts/cpa/quota.sh)" &&
  printf '%s\n' "$script" | CPA_PROXY="$proxy" bash -s --
```

**Windows：PowerShell 5.1+，无需 Python。已取得新版仓库时，在仓库根目录执行，同样不传参数：**

```powershell
.\scripts\cpa\quota.ps1
```

同样按 **代理端口 → HTTPS Base URL → 隐藏 Key** 输入并进入看板。脚本文件运行受本机执行策略约束，不修改系统策略。首次不落盘启动可使用下面的固定源码入口，同样在运行后询问端口，不把 Key 写进命令：

```powershell
& {
    $ErrorActionPreference = 'Stop'
    $port = Read-Host '本机 HTTP/mixed 代理端口'
    if ($port -cnotmatch '^[0-9]{1,5}$' -or [int]$port -lt 1 -or [int]$port -gt 65535) { throw '端口需为 1–65535' }
    $proxy = "http://127.0.0.1:$port"
    $source = Invoke-RestMethod -UseBasicParsing -Proxy $proxy -MaximumRedirection 0 -TimeoutSec 60 -Uri 'https://raw.githubusercontent.com/Killow1998/VPS-Maintenance-Documentation/1a1a9188f03e2937c86cabbb721376e04225a1eb/scripts/cpa/quota.ps1'
    & ([scriptblock]::Create($source.TrimStart([char]0xFEFF))) -ProxyUrl $proxy
}
```

随后隐藏输入自己的客户端 Key。同屏显示当前 Key 的**滚动近 1 小时、7 天、30 天、总历史**请求/失败数、输入/输出/总 token，以及各匿名账号的号池剩余额度和重置时间。`R` 或 Enter 刷新，`Q` 退出；Key 只输入一次，不自动轮询。Bash 加 `--once`，PowerShell 加 `-Once`，即输出一次后退出。原有完整参数调用仍是单次模式，显式 `--tui` / `-Tui` 可进入看板。每次刷新仅请求一次 `/v1/usage?period=overview` 和一次 `/v1/quota`，不发起模型调用或强制刷新上游额度。

窗口共用服务端截止时间及 SQLite 读取快照，按 `[截止时间-时长, 截止时间)` 计算；近 30 天不等于本月。总历史也只统计截止时间前、当前数据库已保留的记录。界面显示采集开始、最后采集时间、状态及拒收数；这些时间不能证明期间无缺口。窗口相互重叠，不可相加。号池余量来自独立缓存，可能比个人用量旧；多个账号的百分比也不可相加。

**代理边界：** 两端在缺少 `--proxy` / `-ProxyUrl` / `CPA_PROXY` 时向交互终端询问端口；没有终端则在读取 Key 前停止。只接受 `http://127.0.0.1:端口` 或 `http://[::1]:端口`；端口交互生成前者。代理失败时不回退直连，忽略 `NO_PROXY` 的绕过意图，不跟随重定向，仍校验 HTTPS 证书。下载失败也不会执行残留脚本。PowerShell 显式使用 [`-Proxy`](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/invoke-restmethod#-proxy)；Python 覆盖 [`ProxyHandler`](https://docs.python.org/3/library/urllib.request.html#urllib.request.ProxyHandler) 的默认绕过行为。不要使用跳过证书校验的选项。

这保证请求进入指定本机代理，**不保证 Clash 最终走某个节点**。在 Clash 的高优先级规则中将 CPA 的实际域名定向到真实代理节点，例如 `DOMAIN,api.example.com,节点选择`，放在宽泛直连规则前，并确认该策略组没有选 `DIRECT`。也可临时使用全局模式并选真实节点。在连接页确认 CPA 域名的最终链路；本脚本不擅自修改客户端规则。

已有环境变量时，Bash 调用追加 `--key-env CPA_API_KEY_CLIENT_A`；PowerShell 脚本调用行追加 `-KeyEnv CPA_API_KEY_CLIENT_A`。参数只填写变量名，不填写 Key 值。省略入口时读取 `CPA_BASEURL`，省略代理参数时读取 `CPA_PROXY`；两端都只询问仍缺失的连接项。自动化环境没有交互终端时，完整提供入口、代理和 Key 环境变量，用 `--once` / `-Once`；脚本不从管道中读取密钥。

以下参数只供自动化或高级查询使用，日常无需填写。参数加在本地脚本命令末尾；不落盘示例中，加在 Bash 的 `bash -s --` 后或 PowerShell 的脚本调用同一行。单期间或单视图查询不要指定 TUI 开关，不能加在 PowerShell 外层 `}` 之后。

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

总览不请求逐次明细。选择具体期间后，显示缓存 token、API 标价估算、计价覆盖率、按匿名账号汇总及最近 10 条执行。条数支持 0–50；`0` 只返回汇总。调用中的 `account-…` 与额度页编号一致，表示实际承接上游执行的账号；账号汇总仅含当前 Key。缺失归属显示“未归属”。API 标价估算不等于订阅扣款，重试也可能生成多条执行。

已安装完整工具包的用户也可运行：

```bash
python3 scripts/cpa/cpa-tools/api_keys.py quota --base-url https://api.example.com
```

该旧 Python 管理入口支持 `--key-env CPA_API_KEY_CLIENT_A` 和 `--json`，但**不具备本次新增的强制代理约束**。要求强制代理的普通使用者应使用上方独立脚本。本次未修改模型客户端或 `cpa_verify.py` 的网络路径。

| 接口 | 数据 |
| --- | --- |
| `GET /v1/quota` | 匿名号池和匿名账号的剩余额度窗口 |
| `GET /v1/usage?period=day&recent=10` | 当前 Key 今日已采集用量、按匿名账号汇总、最近 10 条调用 |
| `GET /v1/usage?period=month&date=2026-10` | 当前 Key 指定月用量 |
| `GET /v1/usage?period=all&recent=50` | 当前 Key 全部汇总及最近 50 条调用 |

公网请求使用 `Authorization: Bearer <自己的客户端Key>`。无效/无 Key 返回 401。普通用户不获得邮箱或其他 Key 的私有用量。

`/v1/usage` 新增 `accounts`（各匿名账号的请求、失败、token、估算费用、最近调用时间）和 `recent`（逐次执行记录），保留原 `totals`、`models`。身份由请求 Key 决定，不接受 `client` 或 `account` 选择参数；非法条数返回 400。升级已有 observer 时，先备份代码和 SQLite 一致快照，复用原数据库、HMAC secret 与路由，替换 `observer.py` 后重启现有 unit，再验证接口。

管理员用 `quota --admin` 或 TUI `g` 通过 SSH 将匿名 ID 匹配邮箱；公网响应不增加邮箱。管理员直接读取 observer 已保存的快照，不临时调用上游。TUI 初次加载预取快照，之后打开面板直接显示；面板 `r` 后台更新，旧画面保留。

当前 Key 共用上游号池，不固定绑定单一账号。各账号百分比不可相加；限额窗口不是剩余 token 数或现金余额。失败数据保留时间与旧值标识，不填成 100%。

### 9.4 用量与费用口径

日报/月报按 `Asia/Shanghai`，原始事件使用 UTC。输入 token 已包含互不重叠的普通输入、缓存读取和写入，不重复相加；输出包含 reasoning。

`prices.json` 是带日期的价格快照。费用是 API 标价估算，不是 Claude 订阅账单；缺价格和缺计量数据保留未知。模型调用成本、订阅和 VPS 流量分别记录。

队列是内存队列，弹出后落 SQLite；进程在两步之间崩溃会丢事件。该实现用于观察用量，不是事务计费账本。部署前历史不可补造；不同时启动两个消费者。

### 9.5 使用者试用清单

**位置：使用者本机。需要：已有 HTTPS Base URL、自己的客户端 Key、可用的本机 HTTP/mixed 代理。以下查询只读，不创建账号、不重置用量，也不发起模型请求。**

1. 使用 9.3 固定命令，填写已有入口和实际代理端口。隐藏输入自己的 Key，不输入面板密码或 SSH 信息。在 Clash 连接页确认 CPA 域名经过真实节点而非 DIRECT。
2. 同屏显示近 1h/7d/30d/总历史和号池余量。按 R/Enter 只刷新当前屏幕，Q 退出。个人汇总仅属于当前 Key；共享百分比不是个人剩余 token。
3. Bash 加 `--once`，PowerShell 加 `-Once` 可单次查询；再追加 `--view usage` / `-View usage` 仅看个人，或 `--view quota` / `-View quota` 仅看号池。
4. 具体期间用 `--period 1h` / `-Period 1h` 等查看账号和调用明细。`day/month` 才是北京时间日历期间，滚动 30d 不是本月；`--recent 0` / `-Recent 0` 隐藏逐次明细。
5. 需要核对真实增量时，先记下本 Key 请求数，完成自己原本就要进行的模型调用，再等采集更新后查询。不要为了查询而额外运行生成验证；重试可能对应多条上游执行，不能要求每条客户端消息恰好增加一条记录。
6. 返回 401 时检查 Key；总览返回 400 或缺字段时交管理员更新 observer，不能把旧版今日/月数据当作滚动窗口。兼容旧版可去掉 TUI 并用 `-Period day` / `--period day`，仍强制代理。代理关闭时应报错，不应显示新查询成功；不需要关闭整个 VPN 来测试，可临时在此命令指定一个已确认未监听的本地端口。
7. 反馈时记录系统、选项、错误状态及采集时间，移除入口地址、Key、邮箱及截图中的私有信息。不要将真实 Key 放入命令参数或发给他人。

本轮仓库合并只包含源码与文档，不表示任何一台 VPS 已升级。已有服务是否具备新接口，以该服务的实际返回为准；管理员升级步骤见[observer 维护说明](../scripts/cpa/cpa-observer/README.md)。

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
