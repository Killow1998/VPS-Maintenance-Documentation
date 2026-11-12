# Linux TCP 安全更新

[返回首页](../README.md) · [备份与维护](04-operations.md)

## 1. 先分清内核修复和应急缓解

**CVE-2026-98432 是 Linux 内核漏洞，不是 XBoard 或 PHP 漏洞。** 容器共用宿主机内核。升级面板镜像、关闭 MPTCP 或更新 Xray，均不能代替安装并运行发行版已确认修复的内核。

截至 2026-11-11，[Ubuntu 官方状态](https://ubuntu.com/security/CVE-2026-98432)确认 Ubuntu 24.04 的 `linux` 在 `6.8.0-149.149` 修复了该漏洞，并已进入常规软件仓库。其他发行版和内核系列的状态可能不同；执行时仍需核对实际系统、官方状态及可用包。2026-10-31 该版本当时仍为 work in progress，不能将当时的状态当作当前结论。

[Go 安全公告](https://groups.google.com/g/golang-announce/c/PCoQcdxoScw/m/aD-MgXEvAAAJ)说明：Go 1.24 起 Linux TCP 监听默认启用 MPTCP；已演示的利用依赖 MPTCP，但底层漏洞不限于 MPTCP。无 MPTCP 监听不代表内核已安全。

本仓库的转发参考实现是 xboard-node / sing-box，面板镜像还可能运行 Caddy。先查实际进程，不因为消息标题提到 Xray 就安装另一套转发内核。确实运行 Xray 时，另外核对[上游安全发行版](https://github.com/XTLS/Xray-core/releases/tag/v26.10.31)；内核修复仍需独立完成。

## 2. 只读检查

**执行位置：目标 VPS。** 下列容器名和服务名是占位符，先替换为实际名称。不要公开完整配置、环境变量或连接列表。

```bash
uname -r
cat /etc/os-release
sysctl net.mptcp.enabled net.ipv4.tcp_migrate_req
ss -H -lnp -A mptcp
systemctl show NODE_SERVICE -p MainPID -p Environment
docker inspect XBOARD_CONTAINER --format '{{.State.Pid}} {{.Image}} {{.HostConfig.NetworkMode}}'
```

MPTCP 开关按[网络命名空间](https://www.kernel.org/doc/html/latest/networking/mptcp-sysctl.html)分别生效。检查每个相关容器，不能只看宿主机：

```bash
pid=$(docker inspect XBOARD_CONTAINER --format '{{.State.Pid}}')
test "$pid" -gt 0
nsenter -t "$pid" -n sysctl net.mptcp.enabled net.ipv4.tcp_migrate_req
nsenter -t "$pid" -n ss -H -lnp -A mptcp
```

Ubuntu 另查已安装和候选的内核包，以及官方安全状态。已安装较新内核但 `uname -r` 仍是旧版，说明还没有运行新内核。没有确认可用的修复包时，先缓解，不把普通更新或换内核系列称为本漏洞已修复。

以下命令在 Ubuntu VPS 上只读检查；`linux-generic` 未安装时，先识别云厂商或 HWE 对应的内核包，不要强行安装 generic：

```bash
cat /etc/os-release
uname -r
dpkg-query -W -f='${Status}\t${Version}\n' "linux-image-$(uname -r)"
dpkg -l 'linux-image-[0-9]*' | awk '/^ii/ {print $2, $3}'
apt-cache policy linux-generic linux-image-generic linux-headers-generic
```

`uname -r` 是当前运行版本；`dpkg` 是已安装状态；`apt-cache` 是本机缓存的候选状态，索引过旧时不能代表仓库实时状态。将实际内核包及版本与 [Ubuntu 官方漏洞状态](https://ubuntu.com/security/CVE-2026-98432)对应的发行版和内核系列比较。

## 3. 维护顺序

本节适用于修复包尚不可用，或需要在维护窗口前临时降低风险的情况。修复包已发布时，优先按第 4.1 节检查和更新；不必为安装修复内核而重复部署缓解措施。已启用的缓解措施应保留到修复内核运行并完成验证后，再评估是否移除。

1. 按[在线备份](04-operations.md#2-在线备份)保存数据库，另存 Compose、节点/systemd/防火墙配置、原 sysctl、网络命名空间、镜像 ID 和相关 PID。记录用户身份/配额、节点参数和规则摘要，以及正常的外部订阅、真实转发和计量基线。
2. 准备配置级回滚并审阅。恢复本次文件、sysctl、专用 nft 表和必要服务；普通回滚继续使用当前数据库和持久卷，不恢复旧用量。原来不存在的文件只删除本次新建项。校验部署哈希，拒绝覆盖后续独立修改。
3. 保留服务商救援入口；启用独立于当前 SSH 会话的延时回滚。确认 timer 正在等待后再应用配置。获得必要的短暂中断授权，不承诺现有连接无损。
4. 先预检、应用专用过滤表，再关闭相关命名空间的 MPTCP，替换旧监听。只重启已确认需要替换监听的服务，不整机重启，不重启 Docker 或网络服务。
5. 分别验证外部订阅、真实转发、正常计量和持久化；通过后才取消回滚，确认回滚没有执行。

### 3.1 过滤 IPv4 TCP 的 IP options

[Red Hat 官方缓解](https://access.redhat.com/security/vulnerabilities/RHSB-2026-016)使用 IPv4 头长度过滤。下面在其 INPUT 示例之外添加 FORWARD 链，覆盖桥接容器的转发路径；这是对容器拓扑的扩展，其他网络模式需单独检查。

先评估业务是否需要 IPv4 IP options。在隔离网络命名空间检查语法及正常 TCP，再部署到宿主机。以下内容保存为独立文件，例如 `/etc/nftables.d/cve-2026-98432.nft`：

```nft
table inet cve_2026_98432 {
    chain input {
        type filter hook input priority -310; policy accept;
        ip protocol tcp ip hdrlength > 5 counter drop
    }
    chain forward {
        type filter hook forward priority -310; policy accept;
        ip protocol tcp ip hdrlength > 5 counter drop
    }
}
```

普通 TCP options 不在 IPv4 头内，不会因这条条件被丢弃。过滤仍只是缓解，不保证阻断所有利用路径。

```bash
nft --check --file /etc/nftables.d/cve-2026-98432.nft
nft list table inet cve_2026_98432
```

确认该表原先不存在，或已审阅其现有内容后，首次执行 `nft --file /etc/nftables.d/cve-2026-98432.nft`。后续启动脚本应先检查表是否存在，避免重复添加规则；存在但内容不符时停止并检查，不直接覆盖。

开机持久化可使用单独的 systemd oneshot：在 `local-fs.target` 后、`network-pre.target` 前调用该幂等脚本，并在 `multi-user.target` 启用。预检 unit，再检查实际规则。**不执行 `flush ruleset`，不为加载这一个表而启用可能清空原规则的全局 nftables 配置。** 保留 UFW、Docker 和透明反代的现有规则。移除缓解时只删除本节专用表和其启动文件。

### 3.2 关闭 MPTCP 并替换旧监听

先确认相关 sysctl 存在且当前业务不依赖 MPTCP。在宿主机专用 `/etc/sysctl.d/90-disable-mptcp.conf` 中保存：

```ini
net.mptcp.enabled = 0
net.ipv4.tcp_migrate_req = 0
```

运行 `sysctl -p /etc/sysctl.d/90-disable-mptcp.conf` 只加载这份文件。不要为局部更新加载所有无关 sysctl。MPTCP 开关限制新建 socket，已有监听还需替换。

Go 节点可增加 systemd drop-in，明确运行时默认值；若已有 `GODEBUG`，合并其他项，不直接覆盖：

```ini
[Service]
Environment="GODEBUG=multipathtcp=0"
```

执行 `systemctl daemon-reload` 后，在授权窗口重启实际节点服务。应用显式启用 MPTCP 会覆盖 GODEBUG，因此还要检查 sysctl 和实际 socket，不能只验环境变量。

对于本仓库使用的 **Linux 桥接网络容器**，保留原 Compose，仅合并：

```yaml
services:
  xboard:
    sysctls:
      net.mptcp.enabled: "0"
      net.ipv4.tcp_migrate_req: "0"
```

[完整 Compose 模板](../templates/xboard.compose.yaml)已包含这两项。内核不支持这些 sysctl，或服务使用 host 网络时，不能直接套用；改用对应宿主机/命名空间方案。

比较规范化 Compose，差异只应是上述 sysctls。核对准确 project/service、持久卷和原镜像，预检后运行 `docker compose --project-directory APP_DIR -f COMPOSE_FILE up -d --no-deps --pull never xboard`。新增容器配置需要重建，普通 `restart` 不会应用它。不要 `down`、删除数据卷或更新镜像。面板和内置 Caddy 会短暂停顿，后台进程也会重建；不能称为“仅重新载入 Caddy”。独立节点是否重启按其自己的监听检查决定。

仅用 `nsenter ... sysctl -w` 修改当前容器，不能保证下一次容器重建仍保留设置。

## 4. 完成标准与后续内核更新

验收必须包含：

- 宿主机和每个相关网络命名空间的实际 sysctl、监听检查；Docker inspect 中的持久 sysctls 与预期一致。
- 专用 nft INPUT/FORWARD 规则各一条，重复运行启动脚本不增加规则；现有 SSH、防火墙、入口、透明回包路由保持可用。
- 真实 HTTPS 订阅可下载、可解析，原订阅 URL、节点和分流规则未变；用户身份、配额和历史用量保留。
- 外部独立客户端明确经目标节点完成小型 HTTPS 请求和已知大小下载，核对实际出口。等待正常报告周期后确认账号计数增长；同时有其他活动时，不把全部增长归到测试流量。
- 面板及节点健康，原 PHP 加固仍加载；临时测试核心已退出，含凭据的临时文件已移除；延时回滚已取消且没有执行。

2026-10-31 的既有部署案例通过上述在线验收：宿主机和面板容器均无 MPTCP 监听；隔离命名空间中的普通 TCP 正常，单个无害 IP-options SYN 命中过滤。原镜像未变，仅重建面板及重启独立节点，nginx、Docker 和网络服务未重启；订阅与计量继续工作。重建及健康恢复操作约 36.5 秒，不是所有用户的精确断线时长。**没有执行整机重启、漏洞利用 PoC 或长时间负载测试。** 实际主机、备份和账户数据仅记录在私有文档。

修复包可用时，先核对官方公告、已安装包与 `uname -r`。修复内核可能已经由系统更新安装，但尚未运行：这种情况先完成备份和启动项检查，取得整机重启窗口后重启，不必重复安装。若尚未安装，先核对软件源、预览升级范围并备份，再安装对应发行版的修复包。

重启前确认修复版启动文件和旧内核入口存在，并准备服务商救援入口。启动后检查 `uname -r`、官方修复状态、过滤/容器设置、SSH、订阅、真实转发和计量。确认已运行修复内核后，再评估是否撤掉临时过滤；不在安装后、重启前提前撤除缓解。现有重启提示不等于已获得重启授权。旧内核仅是可用性回退选项，重新启动受影响的旧内核会重新暴露漏洞。

### 4.1 已有 Ubuntu 24.04 通用内核

1. 在 VPS 上执行第 2 节的只读检查。核对 `linux-generic`、`linux-image-generic` 和 `linux-headers-generic` 已安装，且适用 Ubuntu 24.04 的 `linux` 状态；检查空闲磁盘、软件包管理状态、现有备份和服务商控制台。用户和用量数据只保存在私有位置。
2. 如果官方修复版**已经安装但尚未运行**，不需要再次安装。检查 `/boot` 中该版本的 `vmlinuz`、`initrd.img`、默认启动项和旧内核入口，取得用户对重启及短暂断连的授权后，进入第 4 步。
3. 如果修复版**尚未安装**，在获准的维护窗口内刷新索引并预演升级。以下仅适用于上述三个 generic 元包均已安装的情况。审阅软件源和拟安装、升级或移除的全部软件包；出现不相关的移除或无法解释的变更时停止。

   ```bash
   sudo apt-get update
   apt-cache policy linux-generic linux-image-generic linux-headers-generic
   sudo apt-get -s install --only-upgrade linux-generic linux-image-generic linux-headers-generic
   ```

   只有候选版本得到官方确认、预演范围可接受、备份和救援入口可用，才执行：

   ```bash
   sudo apt-get install --only-upgrade linux-generic linux-image-generic linux-headers-generic
   ```

   不启用 `-proposed`，也不把固定版本号直接复制到不同发行版。其他内核系列使用其发行版支持的对应元包。
4. 核对新内核与 initramfs 已生成、默认启动项指向预期版本，且旧内核入口保留。用户已授权短暂中断后，执行 `sudo reboot`；不要在没有带外入口时假定旧 SSH 会话能自行恢复。
5. 重新连接后再次执行第 2 节，确认**实际运行内核对应已安装的官方修复包**。检查关键服务和缓解是否按预期持久化，再用独立客户端验证 HTTPS 订阅、真实代理转发和正常计量。保留当前数据库和持续增长的用量，不用旧备份覆盖生产数据。

一次 Ubuntu 24.04 既有服务器维护中，修复包已自动安装但旧内核仍在运行；备份、检查默认启动项和 initramfs 后，只需按授权重启。重启后确认运行修复版、面板和节点恢复、外部订阅与转发成功，测试账户的上传/下载计数继续增长。并发流量可能参与计数，不能将全部增长归因于测试。该案例不代表其他内核系列或每台 VPS 都可以跳过安装与预检。

## 5. 发给维护 Agent

```text
请先阅读本仓库 docs/10-linux-tcp-security.md，检查已部署 VPS 的
CVE-2026-98432 风险，不重装 XBoard，不更换用户或节点。
SSH 入口由我私下提供。

先确认运行中的内核、发行版官方修复状态、已安装/候选内核包，
以及实际节点内核、Go 运行时、Caddy 和所有相关网络命名空间的
MPTCP 开关及监听。不要仅因上游发布 Xray 更新就替换 sing-box。

官方修复包已发布时，先确认是否已经安装：已安装但未运行则
预检启动文件和默认启动项，得到重启授权后重启；尚未安装才
核对软件源、预演升级范围并安装对应内核包。不得擅自安装全部更新。

先在线备份数据库和配置，记录用户、配额、订阅、规则及计量基线；
审阅配置级回滚，准备独立延时回滚和服务商救援入口。
需要断开现有连接或重建服务时，先说明影响并取得维护窗口授权。

没有可用修复内核时，按文档在隔离命名空间预检 IP-options 过滤，
保留现有防火墙及透明回包路由，持久关闭相关命名空间的 MPTCP，
只重建/重启替换旧监听所需的服务。保留原镜像、数据卷和用量。
不得清空 nftables、重启 Docker/网络服务或擅自整机重启。

分别验收 HTTPS 订阅、外部真实转发、计量增长和配置持久化。
通过后取消回滚，记录实际影响、备份位置、手动回滚和未测项。
若仅做了缓解，明确指出内核仍待正式更新，另约重启窗口。
私有配置和用户数据不得进入公开仓库。
```
