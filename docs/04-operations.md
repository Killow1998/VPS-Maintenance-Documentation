# 备份、排障与隐私

[返回目录](../README.md)

## 1. 每台机器的私有记录

在仓库之外建立访问受限目录，或使用密码管理器。以下只是字段列表，不要在公开文件中填写真实值：

```text
实例标识、机房、IP、OS、CPU/内存/磁盘规格
SSH 用户、端口、公钥指纹、救援入口
本地私钥所在路径，不复制私钥内容
XBoard 版本、镜像 digest、面板本地地址及随机后台路径
管理员账号和密码的私有存储位置
节点 code、ID、协议、SNI、内核版本
节点私钥、UUID、订阅 token 的私有存储位置
用户列表、额度、到期日和订阅链接
服务商计量口径、节点字节额度、重置日期及时区
服务器配置路径、备份时间和恢复方法
是否配置 Scholar、原出口和回滚规则、最近验收结果
```

不要把私有记录命名后放进公开仓库“等以后再忽略”。`.gitignore` 不会移除已经跟踪的内容。泄露后首先撤销 / 轮换凭据，再评估历史清理，仅删除当前文件不足以消除历史泄漏。

## 2. 在线备份

下例针对 SQLite 版本，数据库在 `/opt/Xboard/.docker/.data/database.sqlite`。先核对真实路径和数据库类型；MySQL 部署使用对应的一致性备份方法，不运行这个例子。

不要直接复制运行中的 SQLite 主文件作为唯一备份，最新提交可能仍在 WAL。使用在线 backup API：

```bash
umask 077
backup_dir="/var/backups/xboard/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$backup_dir"
python3 - "$backup_dir/database.sqlite" <<'PY'
import sqlite3
import sys

source = sqlite3.connect('file:/opt/Xboard/.docker/.data/database.sqlite?mode=ro', uri=True)
backup = sqlite3.connect(sys.argv[1])
try:
    source.backup(backup)
    result = backup.execute('PRAGMA quick_check').fetchall()
    if result != [('ok',)]:
        raise RuntimeError(result)
    print('SQLite backup verified')
finally:
    backup.close()
    source.close()
PY
```

只有看到成功结果才继续变更。另行备份 `.env`、Compose 配置、节点 YAML、原生 JSON、nginx 和相关 systemd 配置；需要恢复整套服务时，这些文件同样重要。备份敏感性等同生产数据库，限制权限，定期保留仓库外的安全副本。

路由变更另存目标节点原始字段，并优先局部回滚。整库恢复用于经过确认的灾难恢复，不是普通路由试错的回退按钮。

## 3. 快速定位故障

| 现象 | 先查什么 | 不应直接断言 |
| --- | --- | --- |
| 订阅更新失败 | 直连订阅端点、HTTP 状态、nginx、token、返回格式 | 一定是节点内核问题 |
| 更新后 `proxies: []` | 用户权限、额度、过滤参数、User-Agent、模板 | 随意增加格式参数一定有效 |
| 代理组循环报错 | 各组实际引用列表，组是否包含自身 | 重装客户端会修好错误 YAML |
| 所有用户同时 timeout | 同一时段端口、节点日志、OOM、出口、控制通道；透明反代还需查回包策略路由 | 一定是晚高峰或某一种内核有 bug |
| 只有一个设备出错 | 本机代理端口、覆盖配置、TUN、浏览器扩展、有效规则 | 一定要换服务器 |
| TUN 开启后失联 | 节点/订阅地址直连、路由回环、DNS bootstrap | 加更多 DoH 就一定不会泄漏 |
| DIRECT 测速红色，代理正常 | 测速 URL 是否直连可达，再测普通国内站点 | 直连网络整体不可用 |
| 国内网站关闭代理可访问，开启后间歇超时 | 有效 DIRECT 规则、直连专用 DNS、海外 CDN 是否触发不可达 fallback、DNS 连接实际出口 | 一定是网站封禁 VPS，或逐站加规则才可解决 |
| 可以上网但面板不计量 | 节点控制 URL、认证、推送、报表任务、数据库计数 | 转发正常就等于面板所有功能正常 |
| Scholar 首页能开、搜索报错 | 实际搜索状态、结果内容、目的地出口 | 首页 200 等于 Scholar 已修复 |

排障先保留时间点、设备、网络和错误，再一次只改一个变量。拿同一请求的客户端和服务端证据对照，不同时更换 SNI、密钥、DNS、内核和机房。

国内网站的 DIRECT 规则与 DNS 选路需要一起验证，通用配置见[订阅 DNS 分流](02-xboard-deploy.md#通用-dns-分流)。测试旧/新配置时使用独立冷缓存和实际订阅内容，记录 HTTP 结果与命中路由。静态 AI 端点返回 200 只证明该网络请求成功，不能代替登录后的对话验收。共享模板上线后，客户端需刷新原订阅，且检查 DNS 覆写没有盖回旧值；回滚仅恢复相关模板并清缓存，不覆盖正在计量的整库。

常用只读检查：

```bash
systemctl is-active xboard-node
journalctl -u xboard-node --since '10 minutes ago' --no-pager
ss -lntup
free -h
df -h /
```

日志可能包含 IP、域名、用户标识或 token。公开提问时只提供脱敏后的相关错误，不整段上传 `.env`、订阅 YAML、数据库或连接日志。

### 3.1 透明反代：服务正常但外部全部 Timeout

**适用范围**：已有 nginx stream 入口使用 `proxy_bind $remote_addr transparent`，将外部连接转发给本机回环后端，并通过策略路由接回后端响应。本节不是新的 443 共用安装教程；节点直接监听公网 443、普通 HTTP 反代或非 networkd 管理的系统，不要照搬。

典型故障表现是 SSH 可达、443 能建立 TCP 连接、本机节点健康检查成功，但真实客户端仍超时。nginx 透明源地址绑定需要配套内核回包路由，监听正常不等于完整转发路径正常，参见 [nginx 官方 proxy_bind 说明](https://nginx.org/en/docs/stream/ngx_stream_proxy_module.html#proxy_bind)。

一次维护案例中，两条回包规则缺失，而专用表中的 local 路由仍存在；仅恢复原规则后，两个外部代理探测恢复。故障前发生过自动更新及网络服务重启，但没有历史规则删除审计，不能把具体删除进程说成已直接取证。未发现资源或配额耗尽，也不能据此将其他 Timeout 一律归入此原因。

先核对以下只读信息；`MAIN_INTERFACE` 是实际网卡名，表号 `1443` 仅对应下方示例，其他部署需替换：

```bash
systemd --version
systemctl is-active systemd-networkd
networkctl status MAIN_INTERFACE --no-pager
ip -4 rule show
ip -4 route show table 1443
systemctl show ROUTE_SETUP_UNIT -p ActiveState -p SubState -p ActiveEnterTimestamp
journalctl -u systemd-networkd --since '30 minutes ago' --no-pager
```

`ROUTE_SETUP_UNIT` 是该机器已有的路由初始化 unit。查看其定义以及 nginx 的依赖，并与 apt 历史、故障时间对照。若初始化是 `Type=oneshot`、`RemainAfterExit=yes`，其 active/exited 只说明以前执行过；内核规则后来丢失时，nginx 的 `Requires=` / `After=` 不会自动重新执行已完成的初始化。

systemd 255 的 `ManageForeignRoutingPolicyRules=yes` 是默认值，会清理未声明于 `.network` 配置的非 kernel 协议规则。因此仅用一次 `ip rule add` 加规则，再保证开机先运行，不足以覆盖运行期间的网络服务重启。版本与行为以目标机为准，参见 [systemd v255 官方 networkd.conf 文档源](https://github.com/systemd/systemd/blob/v255/man/networkd.conf.xml)。

### 3.2 将必要规则纳入网络管理器配置

先恢复访问，再单独验收持久化。恢复时优先使用该机器已审阅、可重复执行的路由恢复脚本，只补缺失项，不清空规则或路由表，不同时更换 DNS、SNI、密钥和内核。

对于上述 networkd 场景，先从 `networkctl status` 的 `Network File` 确定实际生效文件，例如 `/run/systemd/network/10-example.network`。在 `/etc/systemd/network/10-example.network.d/90-transparent-return.conf` 添加 drop-in；`10-example.network` 必须换成实际文件的同名 basename。不要直接修改 `/run` 下的生成文件，也不要用新的宽泛匹配配置接管所有接口。

下面是**示例拓扑**的完整规则段：后端源地址为 `127.0.0.1`，两个 TCP 源端口分别为 `1443` / `9443`，专用表号 `1443`、优先级 `1441` 已确认无冲突。部署时逐项核对，不把示例数字当作所有服务器的固定值：

```ini
[RoutingPolicyRule]
From=127.0.0.1/32
IPProtocol=tcp
SourcePort=1443
Table=1443
Priority=1441

[RoutingPolicyRule]
From=127.0.0.1/32
IPProtocol=tcp
SourcePort=9443
Table=1443
Priority=1441
```

drop-in 合并规则和字段含义见 [systemd v255 官方 systemd.network 文档源](https://github.com/systemd/systemd/blob/v255/man/systemd.network.xml)。这段仅声明 policy rules，不会自动创建表中的 `local 0.0.0.0/0 dev lo` 路由，也不负责防火墙。保留并独立验证已有的开机路由初始化和内部端口保护；若本机路由也会被网络管理器清理，需为其另行设计持久化，不能只验收两条规则。

不要为了局部规则丢失而直接全局关闭 foreign rule/route 清理或关闭自动安全更新。也不要随意改动 DHCP、公网地址、默认网关或原本未受管理的 loopback。此例仅验证 IPv4；不能据此新增 AAAA 记录或宣称 IPv6 已可用。

### 3.3 备份、失联回滚和真实验收

网络服务重启可能导致当前 SSH / 代理连接中断，必须先取得授权，并准备服务商控制台等独立救援入口。

1. 在仓库外、权限为 0700 的私有目录备份已有 drop-in、主网络文件、恢复脚本和规则/路由/接口地址快照；不存在的旧文件要明确记录为“不存在”。记录新配置哈希，不覆盖其他应用配置。
2. 准备并审阅本机回滚脚本：只恢复本次修改的文件，原来不存在则只移除本次新增文件；校验哈希，防止覆盖变更后他人又做的修改。随后有界等待 networkd reload，即使 reload 失败也应尝试已知可用的临时规则恢复。
3. 安装配置前启动独立于 SSH 会话的本机延时回滚任务，并确认 timer 正在等待。五分钟是示例窗口，部署前按预计验收时长设置；不得为了省事在测试前取消。回滚不能保证救回所有网络故障，控制台入口仍需保留。
4. 安装 drop-in 后运行 `networkctl reload`，检查日志是否有未知字段或配置错误；核对每条规则的源地址、协议、源端口、表号和优先级，确保没有重复或遗漏。
5. 在授权窗口执行一次真实 `systemctl restart systemd-networkd`，重新连入后检查规则、local 路由、网络服务及 SSH。对比公网地址和默认网关未变，不把只 reload 或语法通过当作 restart 验收。
6. 从外部设备明确使用目标节点访问两个独立的小型 HTTPS 测试端点；避免测试不知当前选中哪个节点的策略组。再检查 HTTPS 订阅入口。根路径返回预设 404 可证明相应 TLS/入口可达，但不是带 token 的订阅下载成功；真实订阅需在私有环境另测返回格式、节点和规则。
7. 所有验收通过后才停止本次 rollback timer，确认它没有执行，保留私有备份与手动回滚方法。若失败，按预案回滚并重新测外部请求；不要顺手修改用户配额或重置数据库。

下面仅展示延时回滚的命令形态，**不是现成回滚脚本**。先实现并审阅脚本，再将大写占位符替换为本次私有备份路径和唯一 unit 名；实际凭据与路径不回填到本仓库：

```bash
systemd-run --unit=ROUTE_ROLLBACK_UNIT --on-active=5m \
  /bin/sh /PRIVATE_BACKUP_DIR/rollback.sh /PRIVATE_BACKUP_DIR
systemctl list-timers --all ROUTE_ROLLBACK_UNIT.timer
# Only after external acceptance:
systemctl stop ROUTE_ROLLBACK_UNIT.timer
systemctl show ROUTE_ROLLBACK_UNIT.service -p ActiveState -p ExecMainStartTimestamp
```

**已验证的边界**：2026-10-14 维护案例在 Ubuntu 24.04 / systemd 255 上实际重启 networkd 后，两条规则仍在，外部两个节点探测成功；接口地址、默认网关不变，未重启节点或 nginx。该案例没有执行整机重启、全新 VPS 重装或长时间可靠性测试，不能将这些状态写为“已通过”。

## 4. 计量与每月重置

先验证用户通过节点产生的一小段已知流量确实进入计数。长期为零时检查节点到 `http://127.0.0.1:7001` 的控制面连接，而不是先修改计费额度。

用户计数与节点总池是两个对象。每月重置方案应逐项记录：

- 用户重置由哪个 XBoard 任务执行，何日、何时区。
- 节点计数是否由当前版本自动重置。
- 若需额外 timer，它的目标 node code、下一次运行时间、漏跑补偿行为。
- 重置前的一致性备份、执行日志与事后计数核对。

人工核查不能触发一次真实重置。更换节点名或 code 后，复核相关任务的目标，禁止继续沿用其他部署的硬编码标识。额度换算始终区分 GB 与 GiB，并核对服务商是否存在账户共享额度、超额费用或不同重置周期。

## 5. 更新与切换

更新前记录 OS、XBoard commit / 镜像 digest、节点与内核版本，阅读升级说明并完成备份。面板升级、内核升级、出站切换是不同操作，不捆在一起做。

如果用户当前正通过待维护节点与助手通信，提前说明中断窗口，准备独立 SSH / 控制台访问和回滚方法。不要保证“PID 不变就不会断线”；内部配置重载也可能重建连接。

更换实例时先让新节点通过订阅、转发和计量测试，再迁移用户并验证，最后由用户明确确认销毁旧机。停止机器不等于停止服务商收费。

## 6. 发布前检查

公开内容仅包括通用步骤、占位符、标准服务地址和无凭据模板。检查时不要把“公钥可以公开”理解成可以公开一套可用的订阅配置。

必须排除：真实 VPS 地址、可访问后台路径、客户名称 / 邮箱、密码、订阅 URL、UUID、REALITY 密钥、SSH 私钥、API token、数据库、备份、截图中的登录资料、本机绝对路径和未经脱敏的日志。

```bash
git status --short
git diff --cached --name-only
git diff --cached --check
git diff --cached
```

确认暂存文件只来自仓库白名单。再核对提交作者信息，避免个人真实邮箱被不经意公开；需要时使用账户的 GitHub noreply 邮箱。本仓库不存任何具体账户的私有部署记录。
