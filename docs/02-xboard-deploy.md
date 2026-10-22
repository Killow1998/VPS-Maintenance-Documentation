# SSH、XBoard 与订阅交付

[返回目录](../README.md) · 上一步：[购买 VPS](01-vultr-purchase.md)

这部分供具有终端、文件编辑和 SSH 权限的部署助手执行。用户负责确认目标、提供必要凭据和完成客户端验收。以下是新实例流程；已有服务的机器必须先盘点和备份，不可直接覆盖。

所有 `SERVER_IP`、`ADMIN_EMAIL`、`PANEL_PATH`、`NODE_ID`、`USER_TOKEN` 都需在私有环境中替换。不要把替换后的文件提交回仓库。远端 Linux 命令以 root 执行为例，普通管理员使用 `sudo`。

## 1. 首次接管与边界确认

先确认服务器公网 IP、SSH 用户、当前端口、是否新装系统，以及允许操作的范围。没有购买、重装、删除旧服务器或修改其他应用的授权，就不做这些操作。

首次连接前，在 Vultr 控制台核对 SSH host key 指纹；不要为了省事全局关闭主机指纹验证。新机器通常用 22 端口，但以实际监听为准：

```powershell
ssh -p 22 root@SERVER_IP
```

密码由用户在 SSH 提示符输入，或通过受信任的凭据通道交给助手，不写进命令参数。登录后先读取系统版本、CPU、内存、磁盘、IPv4/IPv6 路由、监听端口及已有容器。

```bash
cat /etc/os-release
nproc
free -h
df -h /
ip -4 route
ip -6 route
ss -lntup
```

还要检查时钟同步和内核 OOM 记录。1 GB 实例先控制组件数量，不额外堆装第二套面板、独立数据库或多个代理后端。若确需 swap，先查看是否已有 swap、剩余磁盘及负载，再有针对性配置；swap 不等于增加物理性能。

## 2. 配置 SSH 密钥登录

### 本地密钥

先检查用户是否已有合适的密钥。需要新建时，确认目标文件不存在后执行；为不同部署使用独立名称，不覆盖已有私钥。

```powershell
ssh-keygen -t ed25519 -a 64 -f "$HOME/.ssh/vps_example_ed25519" -C "vps-admin"
Get-Content -LiteralPath "$HOME/.ssh/vps_example_ed25519.pub"
```

设置私钥口令并使用 SSH agent。所谓免密是免服务器密码，不要求私钥无口令。只把 `.pub` 的一行内容提供给 Vultr 或远端 `authorized_keys`，私钥留在用户自己的设备上。[密钥保护原则](https://docs.vultr.com/support/platform/ssh-keys/how-do-i-secure-my-ssh-keys)

### 远端追加公钥

若部署时未注入公钥，在首次 SSH 会话中：

```bash
install -d -m 700 /root/.ssh
```

用编辑器把新公钥追加到 `/root/.ssh/authorized_keys`，不要覆盖原有合法公钥，再设置文件权限：

```bash
chmod 600 /root/.ssh/authorized_keys
```

非 root 用户应使用该用户的家目录并保证属主正确。保留当前会话，另开终端测试：

```powershell
ssh -i "$HOME/.ssh/vps_example_ed25519" -o BatchMode=yes -o PreferredAuthentications=publickey -p 22 root@SERVER_IP 'echo KEY_LOGIN_OK'
```

只有实际看到 `KEY_LOGIN_OK` 才进入关闭密码的步骤。无本地私钥访问能力的云端助手不能替用户凭空完成本机免密；应说明执行位置并让用户完成必要的本地步骤。

## 3. 调整 SSH 端口与认证，避免锁在门外

参考最终 SSH 端口为 `17724`，新机器可以另选未占用端口。更换端口只能减少背景扫描，不能代替密钥认证。

1. 保留旧 SSH 会话和 Vultr 控制台救援入口，备份现有 SSH 配置。
2. 在主机防火墙和 Vultr Firewall 放行新端口，暂时保留旧端口。
3. 修改有效 SSH 配置，使新端口可用；检查 `sshd_config.d` 和 cloud-init 生成的配置。
4. 执行 `sshd -t` 检查语法，按当前系统的服务方式应用，再用 `ss -lntp` 确认真实监听。
5. 从独立终端通过新端口完成密钥登录。
6. 确认有效配置关闭密码认证，复测密钥登录，最后才关闭旧端口。

目标认证配置片段：

```text
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
```

```bash
sshd -t
sshd -T | grep -E '^(port|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication|permitrootlogin) '
ss -lntp
```

```powershell
ssh -i "$HOME/.ssh/vps_example_ed25519" -o BatchMode=yes -p 17724 root@SERVER_IP 'echo NEW_PORT_KEY_LOGIN_OK'
```

Ubuntu 镜像的 `50-cloud-init.conf` 可能优先设置了密码认证，不能只添加一个更晚的文件就认定已关闭。某些系统使用 `ssh.socket`；`sshd -T` 显示的端口也不等于 systemd 当前已监听该端口。助手应检查实际 unit 和监听结果，不盲目执行一套重启命令。

临时密码如已交接给他人，完成密钥验证后应轮换并在私有位置记录救援方式。其他设备使用各自密钥，把各自公钥追加到同一服务器，不共享管理员私钥。

## 4. 安装基础组件与 Docker

在确认是新部署或不存在冲突后安装：

```bash
apt-get update
apt-get install -y ca-certificates curl git nginx fail2ban python3
```

按 [Docker 官方 Ubuntu 仓库安装说明](https://docs.docker.com/engine/install/ubuntu/)添加适配当前系统代号的官方 apt 源并安装 Engine / Compose 插件。不要为解决冲突直接删除已有容器和数据目录。

```bash
docker version
docker compose version
systemctl is-active docker
```

记录实际版本。Docker 发布容器端口可能绕过 UFW 的常规规则，不能只看到 UFW 拒绝端口就认定容器端口不公开；需结合 loopback 绑定及云防火墙验证。

## 5. 安装 XBoard，启动前限制面板地址

依据 [XBoard 官方安装入口](https://github.com/cedar2025/Xboard#-quick-start)检查当前 Compose 分支和初始化方式。下面是当前接口的示例，不是无需检查的固定安装器。

确认 `/opt/Xboard` 尚未使用；已有目录时停止克隆，先检查：

```bash
git clone -b compose --depth 1 https://github.com/cedar2025/Xboard.git /opt/Xboard
cd /opt/Xboard
git rev-parse HEAD
```

审阅实际 Compose 文件，在第一次启动前将面板端口改为：

```yaml
services:
  xboard:
    ports:
      - "127.0.0.1:7001:7001"
```

这是局部片段，不是完整 Compose 文件。保留现有 volumes、环境变量及服务定义。若所选版本提供 `RESOURCE_PROFILE=minimal`，可在 1 GB 实例上评估使用，但不移除计量、队列和调度组件。

另有[完整单服务 Compose 参考](../templates/xboard.compose.yaml)，以及[镜像选择、路径、持久卷与适用边界](05-xboard-solution.md#compose-参考的使用条件)。已有服务不能直接以参考文件覆盖；新部署也须先核对所选版本的初始化和目录布局。

第一次运行安装器前，按[关闭不用的 PHP 原生上传进度](04-operations.md#52-关闭不用的-php-原生上传进度)准备持久 INI 并合并单文件只读挂载。完整参考已包含该挂载，源文件缺失时会拒绝启动。它不替代 PHP 安全更新，不更改用户、规则或 Laravel 会话驱动；实际镜像扫描目录和功能需求仍须核对。

用户私下提供真实管理邮箱后初始化 SQLite 和 Redis：

```bash
docker compose run -it --rm \
  -e ENABLE_SQLITE=true \
  -e ENABLE_REDIS=true \
  -e ADMIN_ACCOUNT=ADMIN_EMAIL \
  xboard php artisan xboard:install
```

安装器输出的管理员密码、后台路径和 `.env` 只记录到仓库之外。不要保留 `ADMIN_EMAIL` 占位符作为真实管理员账号，也不要把初始化日志发布到网上。

```bash
docker compose up -d
docker compose ps
ss -lntp
```

检查面板仅绑定 `127.0.0.1:7001`，并从另一台设备确认公网 `SERVER_IP:7001` 不可达。记录实际镜像 digest；初始化失败时读取错误并修正，不重复运行安装器覆盖数据库。

## 6. 通过 SSH 隧道管理面板

在要使用浏览器的设备上运行：

```powershell
ssh -N -o ExitOnForwardFailure=yes -p 17724 -L 127.0.0.1:17001:127.0.0.1:7001 root@SERVER_IP
```

保持该进程运行，在同一设备访问 `http://127.0.0.1:17001/PANEL_PATH`。这里的路径使用安装后实际后台路径；隧道只负责端口转发，不替代面板登录。

Linux / macOS 同样可用 OpenSSH。手机需要支持 SSH 本地转发的客户端，在手机上建立隧道，再由手机浏览器访问自己的 `127.0.0.1`。笔记本上的 `127.0.0.1` 不是手机的地址，不能跨设备直接使用。

只需要订阅的普通用户不需要 SSH 权限。若面板仅内网管理，就由管理员私下分发订阅；不能同时承诺其他用户可直接打开公网面板登录。

## 7. 创建机器和转发节点

先在面板中创建新机器 / 节点记录，绑定生成的节点 ID 或机器 ID。不要复制任何现有用户的 UUID、节点密钥、机器 token。

示例节点参数：

| 字段 | 值或要求 |
| --- | --- |
| 名称 / code | `VPS-Node` / `vps-node`，或用户另定 |
| 协议 | VLESS |
| 传输 | TCP |
| 加密握手 | REALITY |
| flow | `xtls-rprx-vision` |
| 端口 | `443/tcp`，先检查无其他服务占用 |
| SNI / target | 兼容目标；可先测试 `www.cloudflare.com` / `www.cloudflare.com:443` |
| fingerprint | 客户端 `chrome`，需与内核兼容 |
| REALITY 密钥 / short ID | 为新节点独立生成，保留私钥在服务器私有配置中 |
| 节点额度 | 按服务商额度换算成字节，见下一节 |

选用某个大站域名并不自动保证 REALITY 稳定。应从目标 VPS 检查目标握手兼容性，再做客户端测试。出现 `timeout` / `EOF` 不能直接归因于某家网站或某种内核。改变 SNI 本身不要求重生成密钥，除非同时有独立的轮换需求。

## 8. 安装 xboard-node 并验证控制通道

本方案使用 Linux systemd 服务加 sing-box 内核。依据 [xboard-node 官方说明](https://github.com/cedar2025/Xboard-Node#install)选择当时维护的版本，先下载并审阅安装器、检查参数和发行包校验信息，再按生成的机器 / 节点配置安装。

不要在公开命令示例中填入真实 token，不把第三方脚本直接下载到管道后不经审阅地执行。不同版本的 token、machine ID、node ID 字段可能不同，使用该版本生成的完整配置，不凭本文局部示例猜认证字段。

关键配置不变量：

```yaml
instances:
  - panel:
      url: http://127.0.0.1:7001
    kernel:
      type: singbox
```

实际 `/etc/xboard-node/config.yml` 中还须保留认证、实例 ID 及其他必需字段。这段只是检查用，不可覆盖完整文件。

关闭公网面板后，节点的控制面 URL 也必须走本机 `127.0.0.1:7001`。指向公网 IP 的旧 URL 可能造成“节点仍能转发，但面板不计流量”。

```bash
systemctl is-active xboard-node
journalctl -u xboard-node -n 100 --no-pager
```

在适用版本中核对节点启动、有效用户数量、`ws connected`、`report pushed` 等日志；不同版本措辞可能变化。`healthz` 成功仅表示进程能响应，不替代真正的转发与计量测试。健康检查端口若监听通配地址，应关闭公网访问，不能只保护 7001。

## 9. 新建用户、套餐与流量额度

询问本次部署需要哪些用户、各自额度、到期时间、用户是否能自行登录、每月重置日和时区。用新的随机密码、UUID、订阅 token；若采用“名称加随机字符”，每位用户独立生成，不复用示例账号。

这些记录只进入私有文件。按所选 XBoard 版本配置用户所属组 / 套餐与节点权限，防止用户有额度却看不到可用节点。这里的 XBoard 用户与购买 Vultr 的账户不是同一体系；私用分发不需要顺便接入支付系统。

区分三个概念：

| 项目 | 含义 |
| --- | --- |
| 服务商流量 | 服务商账单中的实际扣费口径 |
| 节点总额度 | XBoard 对该节点累计流量的限制 |
| 每用户额度 | 用户独立的上传、下载可用量 |

```text
十进制 1000 GB = 1000000000000 bytes
二进制 1000 GiB = 1073741824000 bytes
```

部分面板将二进制单位显示成 `GB`，所以不能只比较界面上的数字。核对保存后的 `transfer_enable`，给系统更新、备用出站和其他非代理流量保留余量；不要保证面板统计与服务商账单完全一致。

若用户要求每月 1 日刷新，明确重置时区和服务商周期：

- 用户额度：检查当前 XBoard 调度器的用户重置设置。
- 节点总池：另查当前版本是否会重置节点计数，不假设用户重置等于节点重置。
- 需要独立定时任务时，必须按实际节点 code 精确定位，只清零节点计数，不改用户额度；重置前在线备份并记录结果。
- 首次启用定时任务时检查下一次触发时间，不用“手动执行重置”充当无副作用测试。`Persistent=true` 可能补跑错过的任务，也要纳入确认。

详细备份和计量检查见[维护文档](04-operations.md)。公开仓库不附带针对某个真实节点或客户名单的重置脚本。

## 10. 配置客户端分流，不混淆服务器路由

XBoard 的“订阅模板 / 客户端规则”决定客户端哪些请求进入代理；“节点自定义出站 / 路由”决定已经进入服务器的请求从哪个出口离开。这两层不能互相替代。

助手先读取当前版本生成的完整 Clash 配置及模板机制，再修改模板并检查所有引用：

1. 节点地址和订阅主机直连，防止代理拨号或订阅更新绕回自身。
2. 局域网和回环地址直连。
3. 用户明确指定的直连域名。
4. AI 域名代理规则优先于宽泛直连名单。
5. 国内 / 海外规则集及最终兜底策略。

节点地址应使用实际 IP 的 CIDR 规则，例如替换占位符后的 `IP-CIDR,SERVER_IP/32,DIRECT,no-resolve`，而不是 `DOMAIN,SERVER_IP,DIRECT`。若订阅使用域名，再加精确订阅域名直连规则。

下面只是规则段示例，`节点选择` 必须是实际生成的策略组名称；不让用户把这段当完整客户端配置导入：

```yaml
rules:
  - DOMAIN-SUFFIX,openai.com,节点选择
  - DOMAIN-SUFFIX,chatgpt.com,节点选择
  - DOMAIN-SUFFIX,oaistatic.com,节点选择
  - DOMAIN-SUFFIX,oaiusercontent.com,节点选择
  - DOMAIN-SUFFIX,anthropic.com,节点选择
  - DOMAIN-SUFFIX,claude.ai,节点选择
  - DOMAIN-SUFFIX,google.com,节点选择
  - DOMAIN-SUFFIX,googleapis.com,节点选择
  - DOMAIN-SUFFIX,gstatic.com,节点选择
  - DOMAIN-SUFFIX,googleusercontent.com,节点选择
  - DOMAIN-SUFFIX,grok.com,节点选择
  - DOMAIN-SUFFIX,x.ai,节点选择
  - DOMAIN-SUFFIX,githubcopilot.com,节点选择
```

Codex 和 ChatGPT 相关请求不能只靠一个 `codex` 关键词覆盖；应结合实际连接中的域名检查。登录、静态资源和新域名变化需要按问题补充，不承诺一个永不更新的固定清单。

可复用的[AI 规则片段](../templates/clash-ai-rules.yaml)还包含其他常用 AI 端点和微软 Copilot 的精确域名；它不是完整客户端配置，必须合并进实际订阅模板并验证组名及优先级。

策略组校验要点：节点列表只来自真实 `proxies`；不能把自身或循环引用的组塞入自动选择 / 故障转移。一个节点组成的“故障转移”不提供多服务器冗余。

DNS 设置按实际 Mihomo / 客户端版本验证。加密 DoH 并不自动等于经代理；节点域名的 bootstrap 解析也不能依赖尚未建立的节点本身。保持国内资源直连所需的解析路径，分开测试国内网页、AI 服务和 DNS 出口，不一口气替换全部解析器再归因于服务器。

### 通用 DNS 分流

对于 Mihomo，推荐将直连解析与代理解析分开。在已有 `dns` 中合并以下字段，并保留独立的节点启动解析：

```yaml
dns:
  respect-rules: true
  direct-nameserver:
    - https://223.5.5.5/dns-query
    - https://1.12.12.12/dns-query
  direct-nameserver-follow-policy: false
  proxy-server-nameserver:
    - https://223.5.5.5/dns-query
    - https://1.12.12.12/dns-query
```

这些是要合并的字段，不能替换完整 DNS 配置。`direct-nameserver` 服务于已匹配 DIRECT 的域名；其他初始解析仍需可用的 `nameserver` / `fallback` / policy。`respect-rules` 让 DNS 连接遵循路由规则，因此必须确认境外 DNS 的域名或 IP 实际匹配代理，而节点启动解析可直连；不要把“开启开关”等同于所有 DNS 自动出境。两个示例 DoH 需从实际客户端网络验证，见 [Mihomo 官方字段说明](https://wiki.metacubex.one/config/dns/)。

这可处理国内网站采用海外 CDN、被地理筛选送往不可达备用 DNS 的一类问题，优先于不断追加单站例外。修改 XBoard 持久订阅模板时使用该版本的模型/API并失效缓存；更新订阅后检查客户端有效配置及 DNS 覆写。用冷启动旧/新配置对照，分别测试国内 DIRECT 网站、海外代理站点和节点启动，不接受仅一次缓存恢复作为验收。

上述合并字段也提供为[DNS 规则片段](../templates/clash-dns-policy.yaml)，供部署助手读取；不要当作完整配置交给用户导入。

## 11. 发布完整订阅链接

无域名的基线地址形态：

```text
http://SERVER_IP/api/v1/client/subscribe?token=USER_TOKEN
```

这是演示地址，不能直接使用。助手应从面板取得每位用户的真实订阅链接，在私有回复中交付。先用目标客户端的 User-Agent 请求并确认返回完整配置，而不是只有节点、HTML 登录页或空 `proxies`。不要未经测试就添加 `flag=clash` 等参数。

使用[通用 nginx 模板](../templates/xboard-subscribe.nginx.conf)将公网订阅路径反代到 `127.0.0.1:7001`，其他路径返回 404。此模板包含自己的日志格式和限速 zone，不能再重复定义同名 zone。

部署助手上传模板后，应检查已有网站、默认站点和占用情况，再安装为新的站点配置，创建启用链接，最后检查并 reload：

```bash
nginx -t
systemctl reload nginx
```

不得覆盖另一应用的 `default_server`。模板的访问日志不记录路径或查询串，但错误日志仍可能包含敏感请求，应限制访问并定期轮转。

相关指令可对照 nginx 的[日志格式](https://nginx.org/en/docs/http/ngx_http_log_module.html#log_format)、[请求限速](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html#limit_req_zone)和[代理缓存](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_cache)文档。安装后的 `nginx -t` 必须通过，文本检查不代替目标服务器验证。

HTTP 会暴露传输中的订阅 token 和节点凭据，REALITY 的 443 不保护 80。正式多人使用优先规划带有效证书的 HTTPS 订阅入口。若同一 IP 的 443 已被节点占用，必须另行设计端口 / 反代方案并获得同意，不能让 nginx 抢占 443 导致节点离线。

如果另行采用 nginx stream 的透明源地址转发来共用入口，必须同时设计后端回包路由和内部端口保护，并按[透明反代维护与网络重启验收](04-operations.md#31-透明反代服务正常但外部全部-timeout)验证持久化。一次性添加策略规则或配置开机 oneshot，不能替代网络服务重启后的外部测试。该要求不适用于本文原有的节点直接监听方案。

## 12. 安全加固与交付验收

fail2ban 的 SSH 示例 jail：

```ini
[sshd]
enabled = true
port = 17724
backend = systemd
maxretry = 5
findtime = 10m
bantime = 1h
```

核对实际端口再启用，检查 `fail2ban-client status sshd`。限制管理端口暴露并控制日志增长，不能据此宣称能抵挡任何规模的 DDoS 或“永不被扫描”。

交付前逐项核实：

- 新 SSH 端口密钥登录成功，密码认证已关闭，救援方式仍可用。
- 面板仅 loopback，可通过 SSH 隧道登录；普通用户不需要 SSH 来用订阅。
- PHP 版本符合安全公告；不使用原生上传进度时，其功能已通过持久 INI 关闭、cleanup 保持开启，并核对新容器/worker 已加载，不只看宿主机 PHP。
- 公网订阅能在代理关闭时直接拉取，避免“先连接节点才能更新节点”。
- Clash Verge Rev / FlClash 能导入链接；有效配置无代理组循环、丢失节点或规则引用错误。
- 代理工作、国内直连资源正常；TUN 开启后再次检查节点连接是否形成环路。
- 从实际用户设备产生一小段已知流量，确认用户和节点计数都增加，不仅看日志在线状态。
- 所有非必要管理端口及 IPv6 暴露情况已检查；记录实际检查结果而非只写预期值。
- 每位用户得到自己的完整订阅 URL、配额和刷新说明；私下保存管理员入口及凭据。

客户端导入的是链接，后续通过订阅更新接收规则。仅修改服务器端 Scholar 出口通常不需更换 URL 或重新导入。修改节点参数时先刷新并检查有效配置，只有缓存确实未更新时才考虑重建订阅条目。

## 13. 最后再询问 Google Scholar

基础验收通过后，助手必须再问：

> 你是否需要使用 Google Scholar？如果需要，我会先测试原出口；若 Scholar 搜索受限且符合条件，是否同意按专项文档测试公共 NAT64 分流？它是第三方共享出口，不保证一直可用，也不适用于批量抓取。

不需要则结束基础部署，不擅自添加第三方出口。需要且原出口正常时，确认已有客户端规则即可；遇到符合条件的限制，再进入 [Scholar 专项配置](03-google-scholar.md)。
