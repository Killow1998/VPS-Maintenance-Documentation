# Vultr、XBoard 与 CPA/Claude 部署维护手册

本仓库提供两条独立流程：Vultr + XBoard 的代理订阅，以及 VPS + CPA/Claude 的模型网关。既可由用户逐步操作，也可交给具有本地终端和 SSH 能力的部署助手执行；不要求同时安装两套服务。

## 两部分

| 部分 | 用途和交付 | 计量口径 | 主要入口 |
| --- | --- | --- | --- |
| XBoard 代理订阅 | Clash Verge Rev / FlClash 使用的远程订阅，包含节点、策略组、DNS 和分流规则；不需导入本地文件 | 上传/下载字节、用户配额、节点总池 | [部署方案](docs/05-xboard-solution.md)、[免安装运维工具](docs/08-terminal-tool.md) |
| CPA/Claude 模型网关 | Claude Code 使用的 HTTPS Base URL、独立客户端 Key、模型 ID；不是 Clash 订阅 | 本 Key 请求与 token、匿名调用账号、近期执行；共享号池限额单独显示 | [部署及使用指南](docs/09-cpa-claude.md)、[试用清单](docs/09-cpa-claude.md#95-使用者试用清单) |

两部分可以分别部署。VPS 的网络流量、模型 token 和号池额度不能相互替代，也不合并为同一账单。

## 阅读顺序

| 阶段 | 文档 | 完成标志 |
| --- | --- | --- |
| 选购 | [注册、登录与购买 Vultr](docs/01-vultr-purchase.md) | 用户确认地区与总价，实例就绪 |
| 部署 | [SSH、XBoard 与订阅交付](docs/02-xboard-deploy.md) | 密钥登录、节点连通、订阅和计量正常 |
| 可选扩展 | [Google Scholar 专用分流](docs/03-google-scholar.md) | 按用户需求启用，真实搜索通过 |
| 日常维护 | [备份、排障与隐私](docs/04-operations.md) | 能定位问题、恢复配置且不丢用户数据 |
| 方案交接 | [已完成方案与复现入口](docs/05-xboard-solution.md) | 架构、模板、版本边界和验收清单可供他人复用 |
| 镜像交付 | [干净构建的 XBoard 镜像](docs/06-clean-xboard-image.md) | 获取已通过安装/扫描的公开 Release，不在小 VPS 编译 |
| 已部署修复 | [旧部署 PHP 修复、加固与 Agent Prompt](docs/07-existing-vps-security-repair.md) | 按实际状态选择修复路径，保留现有用户、订阅和用量 |
| 运维工具 | [免安装 Python 终端](docs/08-terminal-tool.md) | 通过 SSH 隧道查询、管理用户和配额，不再临时编写脚本 |
| Claude 网关 | [VPS + CPA + Claude Code](docs/09-cpa-claude.md) | 原生 CPA、HTTPS、双授权、独立 Key、用量与剩余额度 |

已经购买 VPS、想直接复用完整方案时，从[方案交接](docs/05-xboard-solution.md)开始。仓库提供部署维护资料和无凭据参考模板，不是运行中的面板导出，也不分发生产容器镜像。

只需要 Claude 模型网关时，从 [CPA 十二节部署指南](docs/09-cpa-claude.md)开始，无需安装 XBoard 或 Docker。[CPA 工具包](scripts/cpa/README.md)提供双授权、黑底 TUI、Key 管理和剩余额度进度条。普通用户用 Base URL + Key 查询匿名号池；管理员通过 SSH 查看账号邮箱与对应剩余额度。

**已按旧版教程部署过的用户**：先看[已有 VPS 修复指南及可转发 Prompt](docs/07-existing-vps-security-repair.md#4-给维护-agent-的-prompt)，不要重新运行新机安装器或覆盖正在使用的 Compose/数据库。

## 工具脚本

| 场景 | 脚本入口 | 使用说明与条件 |
| --- | --- | --- |
| XBoard 全员流量、用户和配额管理 | [vpsctl.py](scripts/vpsctl.py) | [SSH 隧道终端手册](docs/08-terminal-tool.md)；Python 3.10+，Windows/Linux/macOS；不安装 VPS 常驻服务 |
| CPA Key、用量和剩余额度 | [api_keys.py](scripts/cpa/cpa-tools/api_keys.py) | [自助工具 SOP](scripts/cpa/cpa-tools/README.md)；管理员使用 SSH，普通用户仅查询自己的用量与匿名号池 |
| CPA 个人用量与号池额度查询 | [quota.ps1](scripts/cpa/quota.ps1) / [quota.sh](scripts/cpa/quota.sh) | [一条命令查询](docs/09-cpa-claude.md#93-使用者只用-base-url--key-查询)；Windows PowerShell 或 Bash，显示本 Key 用量、匿名账号、近期调用和共享余量；不调用模型 |
| Claude 双授权 | [claude_auth.py](scripts/cpa/cpa-tools/claude_auth.py) | [授权流程](scripts/cpa/cpa-auth/README.md)；由账号持有人完成浏览器授权 |
| CPA 聚合计量和匿名额度接口 | [observer.py](scripts/cpa/cpa-observer/observer.py) | [采集器部署说明](scripts/cpa/cpa-observer/README.md)；可选 VPS 原生服务，不属于 XBoard 免安装工具 |
| CPA 鉴权与真实生成验收 | [cpa_verify.py](scripts/cpa/cpa_verify.py) | [部署验收步骤](docs/09-cpa-claude.md#8-验证鉴权模型与真实生成)；仅显式运行，真实生成消耗上游用量 |

CPA 自助 TUI 使用 Python 3.9+ 和 macOS/Linux；Windows 通过 WSL 使用，独立 `quota.ps1` 无需 Python/WSL。私密配置与 env 放在仓库外。两套方案共用 VPS 时，先核对 80/443 和现有 SNI 分流，不能覆盖已有站点或节点。

## 参考方案

日常操作可直接运行[单文件 TUI](scripts/vpsctl.py)。只需本地 Python 和现有 SSH 免密登录；首页直接显示全部用户流量，选中用户即可操作，不再输入面板账号、密码或用户 ID。不安装 VPS 服务，不公开管理端口。见[启动、功能和测试边界](docs/08-terminal-tool.md)。

- 单台 VPS：Shared CPU / Cloud Compute，约 1 vCPU、1 GB RAM、25 GB 磁盘，公网 IPv4；可同时开通 IPv6。
- 选购参考型号：`vc2-1c-1gb` 或满足相同条件的当前最低价型号。历史同档预算约 5 美元/月，不是实时保价。
- Ubuntu LTS x64；本文默认以 24.04 的操作习惯为例，新版本需核对安装兼容性。
- XBoard 负责用户、配额和订阅；`xboard-node` 内嵌 sing-box 负责流量转发。
- VLESS + TCP + REALITY + Vision，示例节点名称 `VPS-Node`。
- 面板仅监听本机，管理员通过 SSH 隧道访问。

地区不固定为任何现有服务器所在城市。应根据实际使用地、运营商、当时库存和线路测试选择；地理最近不等于延迟最低，最低标价也不等于满足本方案的最低成本。

## 发给部署助手的起始请求

下面是供用户主动发送的请求模板，不是运行脚本。尚未购买时，不需要填写 VPS IP。

```text
请按照本仓库帮我部署 Vultr + XBoard。

先问我：
1. 主要使用者所在的国家/地区、城市、网络运营商；不需要街道地址。
2. 月预算、预计用户数和月流量、设备类型，是否已有 VPS。
3. 是否必须使用特定服务支持的地区；是否已有域名。

没有 VPS 时：核对 Vultr 当前价格和库存，比较 2-3 个候选地区。
优先推荐满足 1 核、1 GB、约 25 GB、公网 IPv4 的低价实例，
给出推荐地区、备选地区、总价和理由，购买前等我确认。

已有或新购 VPS 就绪后：问我服务器 IP、SSH 端口、登录用户名，
以及可供本地读取的临时凭据文件路径或已配置的 SSH 公钥。
不要索取 Vultr 登录密码、银行卡、验证码、恢复码或 SSH 私钥。

在我授权的这台服务器上，先检查和备份，再配置 SSH 密钥登录，
验证新端口可用后才关闭密码认证和旧端口。
安装 XBoard 与节点后，配置包含分流规则的远程订阅，
询问新用户及额度并验证计量。不要复制别人的 UUID、密钥或 token。

基础服务验收完成后，再问我是否需要 Google Scholar。
需要时先测试原出口；只有存在相应问题且我同意使用第三方出口时，
才按 Scholar 专项文档添加服务器端分流并验收。

请实际执行已授权步骤并报告验证结果；遇到购买确认、凭据缺失、
第三方服务同意或可能失联的变更时暂停询问，不要假装已经完成。
实际凭据、用户记录和订阅链接只留在仓库之外的私有位置。
```

## 隐私与操作边界

- 注册、付款、验证码由账户所有者完成。操作系统的临时 root 密码与 Vultr 网站密码不是同一件事。
- “免密登录”指不再使用服务器登录密码；私钥仍应受口令和 SSH agent 保护，私钥不交给第三方。
- 仓库不收录任何实际服务器地址、客户名单、订阅 token、私钥、数据库或面板凭据。`SERVER_IP`、`USER_TOKEN` 等只是占位符。
- 所有实际部署记录存入仓库之外的访问受限目录或密码管理器。不要在 Issue、PR、截图或公开聊天中粘贴它们。
- `.gitignore` 采用文件白名单，但不能防止强制添加或已跟踪文件里的泄漏。发布前仍需人工查看暂存内容及提交作者邮箱。
- 本手册没有执行购买或自动重装的程序。克隆仓库、阅读文档不会修改 VPS。
- 没有在新购 VPS 上重新走完本版教程；已保留可复用配置，并核对所引用的上游说明。每次部署仍必须独立验收。

## 版本和上游变化

文档核对日期：2026-09-10。价格、地区库存、操作界面及软件发布会变化。执行时记录实际 OS、XBoard commit / 镜像 digest、xboard-node 和内嵌内核版本，不要把移动的 `latest` 标签当作可复现版本。

2026-10-14 维护补充：[透明反代回包规则丢失、持久化与网络服务重启验收](docs/04-operations.md#31-透明反代服务正常但外部全部-timeout)。包含脱敏故障机制、networkd 配置示例、失联回滚和外部验收边界；这不是对选购价格或整份新机安装流程的重新验证。

Scholar 原生配置有明确版本边界，见专项文档。升级前先核对 schema 和生成配置，不能仅凭面板显示“在线”判断转发已经正常。

2026-10-17 维护补充：[直连与代理的 DNS 分流](docs/02-xboard-deploy.md#通用-dns-分流)，说明国内网站使用海外 CDN 时的 fallback 故障、通用解析配置、客户端覆写及冷启动验收。该方案在现有部署上完成独立 Mihomo 对照验证，未重新执行整份新 VPS 安装流程。

2026-10-21 维护补充：[容器内 PHP 安全更新](docs/04-operations.md#51-容器内-php-安全更新)，覆盖 PHP 安全公告、外部限额构建、隔离数据副本、单服务切换、真实订阅/转发/计量验收和保留当前数据库的镜像回滚。公开文档不包含实际机器、凭据或备份。

2026-10-21 方案整理：[XBoard 参考包](docs/05-xboard-solution.md)补齐四条链路、完整单服务 Compose、DNS/AI 规则片段、可选共用 443 的边界及 Agent 交接请求。模板经结构检查；它们不是已在新 VPS 上重跑完的自动安装器。

2026-10-21 镜像构建：[固定源码的干净构建与公开发布](docs/06-clean-xboard-image.md)，新增限额构建配方、新安装烟测、逐层秘密/数据检查和发布工作流。不使用生产快照；以实际 Release 资产及其 manifest 为完成证据，不把运行中的 CI 当作发布成功。

2026-10-21 镜像交付：干净的 PHP 8.2.35 / Linux amd64 镜像已通过 CI 与独立匿名下载、加载和拉取验证。[固定 GHCR digest、Release 下载及 Agent 使用要求](docs/06-clean-xboard-image.md#2-新-vps-使用已构建镜像)已补齐；现有生产服务器没有因此被替换。

2026-10-21 加固补充：[关闭不用的 PHP 原生上传进度](docs/04-operations.md#52-关闭不用的-php-原生上传进度)，补齐持久 INI、单文件只读 Compose 挂载、既有服务备份与面板重建、真实订阅/转发/计量验收和配置级回滚。新机流程及部署 Agent 要求已关联；不替代安全版本，也不更改已发布镜像 digest。

2026-10-25 运维工具：[单文件 Python TUI 与使用说明](docs/08-terminal-tool.md)，提供全员流量首页、SSH 密钥授权、列表选人、订阅链接和配额管理；复用单条连接、缓存只读快照并增量绘制，避免重复输入和每次按键重连。经 27 项离线契约测试、Windows PTY 和已有部署只读接口验收；生产用户未被用作写入测试，不增加 VPS 常驻组件。

2026-10-26（Asia/Shanghai）Claude 扩展：[原生 CPA 部署与自助工具包](docs/09-cpa-claude.md)，按十二节模板整理 HTTPS、setup-token 与资料双授权、按 Key 用量和剩余额度。主屏与额度页共用黑底，额度预取后直接显示，后台更新保留当前画面。已检查源码、帮助、链接、隐私，并完成离线界面预览和既有部署只读查询；新机部署、凭据换发及写入按指南逐项验收。

[PR #1](https://github.com/Killow1998/VPS-Maintenance-Documentation/pull/1) 合并验收：保留当前 XBoard 脚本、测试和工具手册，新增 CPA 的独立指南与源码，并统一上述入口。XBoard 27 项、CPA observer 14 项本地测试通过；15 个新增 Python 源文件、2 段内嵌远端 Python、相对文件链接及已知私密值扫描通过。独立查询脚本通过 Bash 语法、内嵌 Python 离线查询以及 PowerShell 5.1/7.6 内存入口离线查询；不显示 Key 或邮箱。本次未执行 CPA 新机安装、生产授权或写入，也未调用上游生成接口；没有 GitHub CI 检查，不将本地测试称为生产部署验收。

[PR #2](https://github.com/Killow1998/VPS-Maintenance-Documentation/pull/2) 合并整理：增加当前 Key 的匿名账号用量和近期执行记录，更新独立查询脚本；保留 XBoard 工具不变。17 项 observer 离线测试通过，真实使用按[试用清单](docs/09-cpa-claude.md#95-使用者试用清单)由使用者验收。本轮未修改现有 VPS、部署服务或调用上游模型；贡献者先前报告的生产验证与本轮本地检查分别记录。
