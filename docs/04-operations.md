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
| 所有用户同时 timeout | 同一时段端口、节点日志、OOM、出口、控制通道 | 一定是晚高峰或某一种内核有 bug |
| 只有一个设备出错 | 本机代理端口、覆盖配置、TUN、浏览器扩展、有效规则 | 一定要换服务器 |
| TUN 开启后失联 | 节点/订阅地址直连、路由回环、DNS bootstrap | 加更多 DoH 就一定不会泄漏 |
| DIRECT 测速红色，代理正常 | 测速 URL 是否直连可达，再测普通国内站点 | 直连网络整体不可用 |
| 可以上网但面板不计量 | 节点控制 URL、认证、推送、报表任务、数据库计数 | 转发正常就等于面板所有功能正常 |
| Scholar 首页能开、搜索报错 | 实际搜索状态、结果内容、目的地出口 | 首页 200 等于 Scholar 已修复 |

排障先保留时间点、设备、网络和错误，再一次只改一个变量。拿同一请求的客户端和服务端证据对照，不同时更换 SNI、密钥、DNS、内核和机房。

常用只读检查：

```bash
systemctl is-active xboard-node
journalctl -u xboard-node --since '10 minutes ago' --no-pager
ss -lntup
free -h
df -h /
```

日志可能包含 IP、域名、用户标识或 token。公开提问时只提供脱敏后的相关错误，不整段上传 `.env`、订阅 YAML、数据库或连接日志。

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
