# VPS 部署与维护

在 Vultr 上部署 XBoard 代理订阅或 CPA/Claude 模型网关，并通过终端工具管理用户、查看用量。

## 两部分

| 方案 | 用途 | 文档 |
| --- | --- | --- |
| XBoard | 提供 VPN 订阅；管理用户、流量配额和分流规则 | [部署指南](docs/02-xboard-deploy.md) · [运维工具](docs/08-terminal-tool.md) |
| CPA/Claude | 为 Claude Code 提供 API；管理 API Key，查看模型用量和账户池剩余额度 | [部署指南](docs/09-cpa-claude.md) · [运维工具](scripts/cpa/cpa-tools/README.md) |

两套服务可独立部署。已有服务器需要修复 PHP 时，使用[现有 VPS 修复指南](docs/07-existing-vps-security-repair.md)。

## CPA 一键查询

先打开本机 VPN，再运行对应命令。启动后依次输入 **代理端口、HTTPS Base URL、API Key**。API Key 输入时隐藏。首次成功查询后，可选择把连接配置保存到 `~/.cpa/query.json`；下次启动选择 `1 载入已有配置` 或 `2 重新输入`。

**Linux / macOS**：Bash、curl、Python 3.10+。

```bash
c=$(curl -qfsS --max-time 60 https://raw.githubusercontent.com/Killow1998/VPS-Maintenance-Documentation/main/cpa.sh) && bash -c "$c"
```

**Windows**：PowerShell 5.1 或 7。

```powershell
irm https://raw.githubusercontent.com/Killow1998/VPS-Maintenance-Documentation/main/cpa.ps1 -TimeoutSec 60 -ErrorAction Stop | iex
```

看板显示当前 Key 的近 1 小时、7 天、30 天和历史用量，并按模型列出请求数、token、缓存命中率和美元估算费用。账户池区域显示共享额度的余量与重置时间。

| 操作 | 按键 |
| --- | --- |
| 切换模型明细的时间范围 | `1` / `2` / `3` / `4` |
| 翻页 | `N` / `P` |
| 刷新 | `R` 或回车 |
| 退出 | `Q` |

缓存命中率表示输入中有多少内容复用了缓存。例如，100 个输入 token 中有 80 个来自缓存，命中率就是 80%。美元金额按 API 公开价格估算；实际支出以服务商账单为准。详细含义见[用量与费用说明](docs/09-cpa-claude.md#94-用量与费用口径)。

首次下载启动器使用当前网络；输入端口后，后续下载和 API 查询均通过指定代理，代理失败即停止。请在 VPN 中确认 CPA 域名使用了代理节点。需要下载全程使用代理时，见[完整启动命令](docs/09-cpa-claude.md#93-使用者只用-base-url--key-查询)。

遇到“查询参数不受支持”或缺少模型明细时，请管理员按[observer 更新说明](scripts/cpa/cpa-observer/README.md#repair-prompt)检查服务器版本。

## 工具脚本

Python 工具统一使用 **Python 3.10+**。

| 工具 | 功能 | 运行环境 |
| --- | --- | --- |
| [vpsctl.py](scripts/vpsctl.py) | XBoard 全员流量、用户、订阅、配额和服务管理 | Windows / Linux / macOS，Python、SSH |
| [api_keys.py](scripts/cpa/cpa-tools/api_keys.py) | CPA Key 管理、用量和账户池查询 | Linux / macOS / Windows WSL，Python、SSH |
| [claude_auth.py](scripts/cpa/cpa-tools/claude_auth.py) | 新增 Claude 账号并完成授权 | Linux / macOS / Windows WSL，Python、SSH、Claude Code |
| [quota.sh](scripts/cpa/quota.sh) / [quota.ps1](scripts/cpa/quota.ps1) | CPA 个人用量与共享额度查询 | 上方一键命令 |
| [observer.py](scripts/cpa/cpa-observer/observer.py) | 在 VPS 上采集用量、提供查询接口 | Python、systemd |
| [cpa_verify.py](scripts/cpa/cpa_verify.py) | 验证鉴权和模型生成 | Python；生成测试会消耗用量 |

XBoard 工具通过 SSH 密钥登录，首页显示全部用户，选中用户即可操作。启动与管理方法见[终端工具手册](docs/08-terminal-tool.md)。

## 阅读顺序

| 任务 | 文档 |
| --- | --- |
| 购买服务器 | [注册与选购 Vultr](docs/01-vultr-purchase.md) |
| 部署 XBoard | [SSH、节点、订阅与计量](docs/02-xboard-deploy.md) |
| 配置 Google Scholar | [专用分流](docs/03-google-scholar.md) |
| 备份和排障 | [日常维护](docs/04-operations.md) |
| 复用完整方案 | [XBoard 配置与交付清单](docs/05-xboard-solution.md) |
| 使用预构建镜像 | [镜像下载与版本](docs/06-clean-xboard-image.md) |
| 修复已有服务器 | [PHP 更新与加固](docs/07-existing-vps-security-repair.md) |
| 管理 XBoard | [终端工具](docs/08-terminal-tool.md) |
| 部署 CPA/Claude | [网关部署与使用](docs/09-cpa-claude.md) |

## 参考方案

XBoard 的入门配置：Vultr Shared CPU、1 vCPU、1 GB RAM、约 25 GB 磁盘、公网 IPv4、Ubuntu LTS。地区按使用地、运营商和线路测试选择，价格以购买页面为准。

XBoard 管理用户和订阅，`xboard-node` 使用 sing-box 转发 VLESS + TCP + REALITY + Vision 流量。面板通过 SSH 隧道管理。两套服务共用 VPS 时，先按[端口配置说明](docs/09-cpa-claude.md#54-已有-443--sni-分流)安排 80/443。

## 发给部署助手的起始请求

<details>
<summary>展开部署请求模板</summary>

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

</details>

## 安全与维护

- 注册、付款和浏览器授权由账户所有者完成；SSH 私钥留在自己的设备上。
- 凭据、订阅链接、数据库和服务器备份存入仓库外的私有目录。
- 修改已有服务前备份并准备回滚，保留用户、配额、订阅和已有用量。

版本与验证记录见[更新记录](CHANGELOG.md)，各次部署按对应指南验收。
