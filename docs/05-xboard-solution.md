# 已完成方案与复现入口

[返回目录](../README.md) · [从新 VPS 开始部署](02-xboard-deploy.md) · [维护与安全更新](04-operations.md)

这份文档把已经完成并在既有部署验收过的 XBoard 方案整理成可交接的公开参考包。它不是当前服务器的数据导出，不是 XBoard 源码 fork，也不是运行后自动接管服务器的一键脚本。每台新机必须生成自己的密钥、用户、订阅和镜像版本记录。

## 1. 最终交付的是什么

| 部分 | 方案及职责 | 新机验收要求 |
| --- | --- | --- |
| 面板 | XBoard 容器，SQLite、Redis、Octane/Swoole、Horizon；具体组件以选定版本为准 | 本机面板和后台任务正常，持久化目录正确 |
| 转发 | 独立的 xboard-node / sing-box，VLESS + TCP + REALITY + Vision | 真实外部 HTTPS 请求成功，不只看 TCP 端口或 healthz |
| 订阅 | 每用户一条链接，包含节点、DNS、策略组和规则 | 代理关闭时也能下载，Clash Verge Rev / FlClash 可使用 |
| 管理 | 面板绑定 `127.0.0.1:7001`，管理员通过 SSH 隧道访问 | 公网面板不可达，普通订阅用户不需要 SSH |
| 计量 | 本机控制通道推送在线状态及用量，用户额度和节点总池分开管理 | 真实请求后计数增长，月重置日/时区明确 |
| 客户端规则 | 国内及局域网直连，AI/海外代理，独立节点 bootstrap DNS | 国内网页、AI 静态端点、TUN 开关及冷启动分别测试 |
| 可选 Scholar | 独立服务器端 DNS64/NAT64 分流 | 用户同意且符合前提时才启用，验证真实搜索结果 |
| 维护 | 一致性备份、固定镜像身份、兼容性验收、延时回滚 | 故障可恢复且不覆盖正在增长的数据库用量 |

这些模块已有既有部署证据，但没有在一台全新 VPS 上重新执行本版参考包的全部流程。新文件的静态/Compose 校验不等于新实例已完成部署。

2026-10-21 参考包检查：三份 YAML 解析、Compose 配置展开、缺少必填镜像时拒绝运行、loopback 面板绑定、27 条 AI 规则无重复、文件链接和已知隐私模式扫描均通过。本次没有启动容器、拉取镜像、重启服务或修改生产 VPS；DNS/AI 片段仍须在新部署生成的完整订阅中做内核与网络验收。

## 2. 不混淆四条链路

```text
Subscription:
  client -> HTTPS subscription ingress -> XBoard 127.0.0.1:7001
         <- per-user nodes + DNS + proxy groups + routing rules

Proxy data:
  client -> public VLESS/REALITY listener -> xboard-node / sing-box -> Internet

Node control and accounting:
  xboard-node -> http://127.0.0.1:7001 -> configuration and usage reports

Administration:
  administrator -> SSH local forwarding -> localhost panel -> panel login
```

关闭公网面板之后，控制面仍应使用本机地址。订阅正常、转发正常、计量正常是三个不同结论；某条链路正常不能代替其余两条。

### HTTPS 与代理同时使用 443

按部署复杂度选一种，不同时套用：

1. **简单入口**：节点独占 443，HTTPS 订阅使用另一个已告知用户的端口或独立 IP，配置有效证书。不要为此直接公开完整后台。
2. **共用 443 的进阶入口**：nginx stream 按 TLS SNI 将订阅域名转到本机 HTTPS 服务，其余流量转给 REALITY 后端。若采用透明源地址转发，还需要配套回包策略路由和内部端口保护，见[故障机制及持久化验收](04-operations.md#31-透明反代服务正常但外部全部-timeout)。

同机不同子域名并不会自动解决端口冲突。不要开启未经验证的 PROXY protocol，也不要只检查监听就宣称共用入口成功。既有透明转发方案测试了 networkd 重启后的规则持久化；未据此证明所有系统或完整重启都适用。

域名示例一律使用 `sub.example.com`、`node.example.com`，实际值私下填写。需要 A/AAAA、证书、续期和客户端 bootstrap 的独立验证；不提供当前服务器的域名、IP 或证书私钥。

## 3. 可复用文件

| 文件 | 执行位置及用途 | 不能做什么 |
| --- | --- | --- |
| [xboard.compose.yaml](../templates/xboard.compose.yaml) | VPS 应用目录；完整的单服务 Compose 参考 | 未核对上游版本前覆盖已有 Compose 或数据 |
| [clash-dns-policy.yaml](../templates/clash-dns-policy.yaml) | XBoard 的完整 Clash/Mihomo 订阅模板中合并 DNS 字段 | 当完整客户端配置导入，或清空其他 DNS 配置 |
| [clash-ai-rules.yaml](../templates/clash-ai-rules.yaml) | 同一订阅模板；插入到宽泛直连/国内/兜底规则之前 | 替代用户节点、策略组或完整规则集 |
| [xboard-subscribe.nginx.conf](../templates/xboard-subscribe.nginx.conf) | nginx `http` 配置；仅订阅路径的基线反代 | 当作 HTTPS 或 stream 共用 443 配置直接使用 |
| [scholar-nat64.json](../templates/scholar-nat64.json) | 符合条件的节点原生配置；见 [Scholar 文档](03-google-scholar.md) | 未经同意替换全部出站或承诺绕过 Google 限制 |

最终仍交付订阅链接，而不是要求用户导入这几个文件。公开片段没有用户 UUID、REALITY 密钥或 token，必须由该部署的 XBoard 根据用户授权动态生成节点。

### Compose 参考的使用条件

先按[安装流程](02-xboard-deploy.md#5-安装-xboard启动前限制面板地址)选定版本并完成必要初始化，再对照本参考审阅上游 Compose：

- 相对路径必须解析到实际私有应用目录，不要在本仓库 `templates` 目录直接启动。
- `.env` 必须是有效的应用配置文件，数据库、日志、主题和插件目录应已正确创建并设置属主。模板禁止自动创建缺失的 bind 源，避免把缺失 `.env` 变成目录。
- `XBOARD_IMAGE` 必填。先取得并检查镜像，再设置已验证的 tag/digest；模板设置 `pull_policy: never`，不会因为维护时启动服务就自动换成未检查的新镜像。验证 image ID、PHP 和平台要求，不只看标签。
- 模板沿用单服务内置 Redis 布局、独立持久卷及本机面板绑定。镜像若改成外置数据库、改变目录、入口或用户，则不适用，应以该版本为准。
- `balanced` 是参考资源档，不保证适合每个小内存实例；先检查余量和该镜像支持的档位，保留计量、队列和调度。新增 Docker 日志轮转只覆盖容器 stdout/stderr，应用文件日志另行管理。
- 已有机器保留当前 Compose project name 和实际 Redis 卷身份。换项目名或从另一目录启动可能创建空卷，不能误认为数据库/Redis 自然迁移了。

在独立、私有的部署目录完成替换与审阅后，用实际文件名运行 `docker compose config`。输出可能含私密配置，不贴到公开 Issue。新参考包的检查不授权修改现有服务器。

### 合并 DNS 与 AI 规则

先读取现有完整模板，只合并示例字段；节点地址、订阅地址和局域网的直连规则仍在最前面，AI 规则必须早于宽泛的 `microsoft.com`、国内名单等直连项。组名 `节点选择` 必须在实际订阅中存在。

DNS bootstrap 不得依赖尚未建立的节点本身；`respect-rules` 也不保证所有 DoH 自动走代理。保持可直连的 `proxy-server-nameserver`，检查外国解析器的连接实际命中哪个出口。AI 域名列表是维护基线，不保证覆盖所有未来的新域名，也不保证决定第三方服务的模型权限或账号风控。

XBoard 不同版本的模板存储和缓存机制可能不同。通过该版本支持的模型/API保存并失效缓存，不能只修改临时渲染结果或直接写数据库后假设广播已完成。下载生成后的完整订阅，再用真实内核检查代理组引用、循环、DNS 和规则；订阅更新后确认客户端覆写没有盖回旧值。

## 4. PHP 修复与升级分开处理

既有部署已经完成 PHP 8.2.31 -> 8.2.35 的独立运行时修复，保留应用和用户/模板，并验证订阅、转发及计量；切换时发生过 Compose 标签继承冲突，已恢复，不能描述成零中断。通用方法及陷阱见[容器 PHP 安全更新](04-operations.md#51-容器内-php-安全更新)。

不要为了复刻旧版本而在新 VPS 上安装已知有漏洞的 PHP。先检查当时上游镜像是否已修复；[上游 Dockerfile](https://github.com/cedar2025/Xboard/blob/master/Dockerfile)和[官方 PHP 镜像构建定义](https://github.com/docker-library/php/tree/master/8.2)是检查入口，但源码声明不证明已发布镜像或运行容器的版本。已经修复就不必再次编译；尚未修复才考虑外部限额构建兼容派生镜像。

禁止公开发布从生产容器 commit 的镜像或归档，避免历史层泄漏环境、缓存或资料。另有已经授权的[干净源码构建与发布路线](06-clean-xboard-image.md)：固定公开输入、保留许可证，经新安装烟测及镜像层检查后才发布。该路线不读取生产服务器；新 VPS 优先取得已构建的明确版本 Release，而不是重复编译。源码、构建脚本和 CI 存在不等于镜像已经通过并发布，以 Release 的实际资产和 manifest 为准。

## 5. 交接给另一个 Agent 的要求

把以下请求与本仓库链接一起交给具备终端和 SSH 权限的 Agent。尖括号字段私下提供，不写回 GitHub：

```text
请按本仓库 docs/05-xboard-solution.md 维护或复现 XBoard 方案。
新 VPS 优先按 docs/06-clean-xboard-image.md 获取已构建的干净镜像。
目标 SSH 入口：<已有别名或私下提供的地址、用户名和端口>。
本地私有维护目录：<目录>。本次操作范围：<新部署或指定维护项目>。

先读项目指令和私有运行记录，核对实际版本、端口、数据库及持久卷。
新部署按 docs/01、02 执行；已有服务先备份，不重跑安装器覆盖数据。
每台机器独立生成用户、密钥、token，不复制参考部署的真实账户。
普通用户最终拿到一条带节点和规则的订阅链接，不需要 SSH 或文件导入。
PHP 更新按 docs/04 执行；小 VPS 不编译，兼容候选先隔离验收。
可能失联的网络变更先获授权、保留救援入口并启用延时回滚。
分别验证订阅、外部转发和计量；不修改无关用户、规则或应用。
询问是否需要 Google Scholar，不需要则不增加第三方出口。
完成后更新私有运行记录和脱敏维护文档；推送前需有明确授权。
如实记录失败、恢复和未测试项；无授权不扩展服务、不启动子 Agent。
```

## 6. 完成标准与发布边界

- 凭据：新端口密钥登录验证，救援入口可用；私钥和面板密码不公开。
- 入口：面板仅 loopback；HTTPS 订阅证书有效且可直连，非订阅后台路径被拒绝。
- 客户端：下载完整配置、无代理组循环；节点/bootstrap、国内直连和 AI 代理冷启动通过。
- 计量：实际流量进入用户和节点计数；额度单位、时区、下一次重置时间明确。
- 维护：备份可验证，恢复镜像/配置明确；普通回滚保留实时数据库，不覆盖新用量。
- 证据：版本/镜像身份、检查时间、实际结果与未验证项留在私有记录；对外只发脱敏结论。

公开仓库不包含实际服务器 IP/域名、用户清单、订阅 URL、UUID、REALITY 密钥、节点认证、`.env`、数据库、生产镜像、私有备份或原始日志。复制模板到私有目录后也不要强制添加回仓库。上游 XBoard 源码和许可证仍以[原项目](https://github.com/cedar2025/Xboard)为准。
