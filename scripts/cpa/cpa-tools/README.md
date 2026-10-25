# CPA 自助运维 SOP

本页命令从工具包根目录执行：先在仓库根目录运行 `cd scripts/cpa`。实际配置和会话放在仓库外。

两个 Python 入口处理日常操作，无需由 GPT 启动。仅使用 Python 标准库；CPA 原生运行，不引入 Docker、网页管理服务或新的公网端口。

| 入口 | 功能 |
| --- | --- |
| `claude_auth.py` | 新增 Claude 账号；依次完成 setup-token、profile/quota 授权；校验、备份、绑定、启用；中断续接；查看凭据状态 |
| `api_keys.py` | 全屏 TUI；新增、删除、改名、换 Key；查询指定 Key 或全部 Key 用量；导出到本机私密文件 |

`common.py`、`remote_admin.py`、`key_tui.py`、`key_view.py`、`quota_view.py`、`tui_theme.py` 和 `pool_quota.py` 是内部模块。保留同目录结构，并把相邻的 `cpa-auth`、`cpa-observer` 源码一并保留。日常操作只运行上面两个入口。

## 1. 前置条件与一次性配置

- 本机：macOS/Linux、Python 3.9+、SSH；新增 Claude 账号还需安装官方 Claude Code CLI。Windows 请在 WSL 中运行，并在运行脚本的同一电脑浏览器完成授权。
- VPS：已部署 CPA v8、observer、SQLite、systemd；SSH 管理员可读 observer 配置、写 CPA 凭据并重启 observer。默认 CPA unit 为 `cliproxyapi.service`。本工具不安装服务或升级 CPA。
- 已核实 SSH 主机指纹，并配置密钥登录。工具拒绝自动接受新指纹。
- observer 配置保持原有 HMAC secret、数据库路径和标签。不要重建，否则历史标识会变化。

在项目根目录运行：

```bash
python3 cpa-tools/claude_auth.py init
```

按提示输入 VPS、SSH 端口/用户、私钥路径、HTTPS Base URL、observer 配置路径、验证模型及**首次部署生成的本机 env 路径**。信息缺失时询问操作者，不猜测生产环境地址或账号。配置写入 `cpa-tools/.deployment.private.json`，权限 `600`。`env_file` 指向既有 `.cpa-env`，日常新增和导出不再要求提供新的文件路径。

现有电脑已配置该文件时，跳过 `init`。也可使用 `CPA_TOOLS_CONFIG` 环境变量，或在**子命令前**传 `--config /PRIVATE/deployment.json`。两个入口共用配置，不需要在本机保存管理 Key：工具通过 SSH 在 VPS 内读取现有 observer 配置。

泛化模板（所有占位符必须替换）：

```json
{
  "ssh": ["ssh", "-i", "/PRIVATE/SSH_KEY", "-p", "SSH_PORT", "-o", "BatchMode=yes", "-o", "ConnectTimeout=12", "-o", "StrictHostKeyChecking=yes", "ADMIN@VPS_HOST"],
  "base_url": "https://CPA_HOST",
  "env_file": "/PRIVATE/EXISTING_DEPLOYMENT/.cpa-env",
  "observer_config": "/etc/cpa-observer/config.json",
  "observer_script": "/opt/cpa-observer/observer.py",
  "observer_unit": "cpa-observer.service",
  "model": "MODEL_ID_AVAILABLE_TO_THIS_ACCOUNT"
}
```

适配端口、服务名、路径前，先只读检查目标部署。SSH 数组最后一项必须为目标，不要加入远端 shell 命令。工具只将源码放入命令参数，配置和凭据通过 SSH 标准输入传送。

## 2. 新增 Claude 账号

交互菜单：

```bash
python3 cpa-tools/claude_auth.py
```

或直接开始：

```bash
python3 cpa-tools/claude_auth.py add
```

1. 输入新增账号邮箱和唯一代号。工具拒绝覆盖已有凭据文件；同一账号的多个 token 不代表新增订阅额度。
2. 终端显示会话目录。保留终端运行。工具启动隔离的本机 `claude setup-token`，确认真实回调监听后显示授权链接。
3. **自行复制链接到该账号的隐私窗口**。完成邮箱验证和授权。工具不自动打开 Safari，也不要求复制 token 或回调 URL。
4. 工具私密捕获 token，通过 SSH 以禁用状态导入 VPS，再用仅含新凭据的临时 CPA 发起少量真实生成。此步骤会消耗少量 token。
5. 工具显示第二条 profile/quota 授权链接。继续选择同一个账号。该步骤临时转发本机 `127.0.0.1:54545` 到 VPS 回调端口；仅用于登录，日常 API 流量走 HTTPS。
6. 工具核对 profile 邮箱、账号 UUID 和实际 usage 窗口。按提示再次输入两次授权所用邮箱，确认 setup-token 与资料授权属于同账号；不会将用户确认冒充 token 自身披露的身份。
7. 自动备份配置、凭据和 SQLite 一致快照，写入 `quota_bindings`。模型 token 原值与空 refresh 字段保留。资料凭据保持禁用模型调度，只作为额度来源。
8. 启用模型凭据，仅重启 observer，检查回环模型接口和额度覆盖、目标账号状态、辅助凭据禁用状态。三项检查通过后显示完成。

**完成依据是工具的最终结果，不是浏览器成功页。** setup-token 与 CPA 普通 OAuth 使用不同的回调页面，不据此推断权限。标准 setup-token 当前只具备推理权限，profile/quota 需要第二种授权。两份凭据在已完成绑定的号池中只计一个逻辑账号。

可指定非敏感的代号；邮箱也可通过参数传入，但会进入 shell 历史。介意邮箱暴露时使用交互输入：

```bash
python3 cpa-tools/claude_auth.py add --account client_b
python3 cpa-tools/claude_auth.py status
```

### 中断、过期链接与续接

按 `Ctrl+C` 停止会关闭本次接收进程，旧链接失效。用终端最初显示的**同一会话目录**继续：

```bash
python3 cpa-tools/claude_auth.py resume --session /PRIVATE/SESSION_DIRECTORY
```

已导入的模型凭据不会覆盖；已有资料凭据也不会重复申请。模型会重新进行隔离生成检查，绑定前会重新核对资料和 usage。资料授权的接收进程约五分钟超时；过期必须由 `resume` 启动新接收端和新链接。不要刷新旧的 localhost 页面。

绑定失败会保留新账号为禁用，并恢复原 observer 配置。现有账号与下游 Key 保留。备份路径随错误或成功结果返回。发生网络超时或终端意外退出时，先 `status`，再续接同一会话，避免重新 `add` 造成重复账号。

本入口聚焦**新增账号及该会话续接**。已验收账号的 token 到期替换仍按主指南 5.1.2 执行，不支持用 `add` 覆盖。额度 helper 的日常 401 刷新由现有 observer 处理；403 缺少 scope 需要重新授权，429 应等待，不重复强刷。

## 3. API Key 管理

默认进入全屏 TUI，不再滚动输出 JSON：

```bash
python3 cpa-tools/api_keys.py
```

主屏用一行展示一个调用者：名称、鉴权状态、请求数、失败数、Tokens、估算 USD。列表默认仅显示有效 Key。后台读取 VPS 时界面保持可操作；用量读取失败时仍显示 Key 状态，并将用量标为未知。

| 按键 | 操作 |
| --- | --- |
| ↑ / ↓、Enter | 选择调用者、查看模型明细和完整非秘密 ID |
| `n` / `e` / `k` / `x` / `s` | 新增 / 改名 / 换 Key / 删除 / 导出原 env |
| `1` / `2` / `3` | 今日 / 本月 / 全部，北京时间 |
| `g` | 管理员号池额度，显示账号邮箱及对应的余量窗口 |
| `/` / `h` / `r` / `q` | 筛选 / 显示历史 / 刷新 / 退出 |

换 Key、删除需要输入选中调用者名称确认。新增、修改、删除成功后，结果面板**显示一次 `.cpa-env` 的完整绝对路径**；回车返回主屏。长路径完整换行显示。主屏不展示 Key 内容，也不逐行输出后台响应和备份元数据。

TUI 使用 Python 标准库 [curses](https://docs.python.org/3/howto/curses.html)，要求至少 80 列 × 18 行，不增加 pip 依赖。`--demo` 提供离线界面预览，不读取生产配置、不连接 VPS、不执行写操作。非交互终端使用下面的子命令；默认紧凑表格，显式 `--json` 才输出完整结构：

```bash
python3 cpa-tools/api_keys.py --demo
python3 cpa-tools/api_keys.py list --json
python3 cpa-tools/api_keys.py usage client_a --period month --json
```

界面仅在输入、结果到达或窗口尺寸变化时重绘；方向键、筛选和历史切换不等待网络。日/月/全部用量有 15 秒内存缓存，`r` 强制刷新，写操作成功后清空缓存。TUI 会话通过 [OpenSSH ControlMaster](https://man.openbsd.org/OpenBSD-7.7/ssh_config) 复用管理连接，退出时关闭，异常时最多保留 60 秒空闲时间；不配置端口转发，不修改全局 SSH 配置。首次连接执行 SSH 握手；后续操作复用连接。

### 统一命名与保存格式

- 环境变量名统一为 `CPA_API_KEY_<大写调用者名称>`。
- env 赋值统一为 `export CPA_API_KEY_<名称>=...`，沿用首次部署的同一文件。
- 新增和主动换发的 Key 值统一为 `sk-cpa-` 加高熵随机串；调用者名称只作标签，不写入新 Key 值。
- 历史 Key 前缀保持原值。改变前缀就是更换有效凭据，会影响正在使用旧 Key 的程序；需要时通过 `k` 主动换发。

`export` 是 Shell 语法，`sk-cpa-` 是凭据值前缀，两者不是同一层。仅整理旧文件写法可运行下列命令；它不改 VPS，不改变 Key 值，也不执行 env 内容：

```bash
python3 cpa-tools/api_keys.py normalize-env
```

### 查看与新增

```bash
python3 cpa-tools/api_keys.py list
python3 cpa-tools/api_keys.py add client_a
source /PRIVATE/EXISTING_DEPLOYMENT/.cpa-env
```

名称只含英文字母、数字、下划线或连字符，并以字母开头。名称用于计量标签，不是 Key 内容。工具生成高熵随机 Key，VPS 配置成功后，原子更新原有 `env_file` 中的 `CPA_API_KEY_CLIENT_A`。文件权限为 `600`，不打印完整或部分 Key。其他 Key、管理 Key、现有 Base URL、注释及配置保留；缺失 Base URL 时才补充。不同名称不得映射到同一环境变量。

新增前自动保存一份权限受限的临时恢复记录。远端失败时不提前替换原 env 中正在工作的变量。若 SSH 超时或本机写入失败，工具保留恢复记录并显示路径；先 `list` 核实 VPS，再用 `export` 同步当前有效 Key。成功后删除本次临时记录。不需要用户选择新的 env；确有独立交付需求时才用可选的 `--out /PRIVATE/separate.env`。

`list` 的 `active: true` 表示 Key 仍可鉴权，不表示某个程序正在运行。新增其他调用者不会替换正在使用的 Key。已有终端和程序保留已加载的环境；需要在该终端使用新增 Key 时再 `source` 原文件。只在明确需要撤销旧 Key 时使用 `rotate` 或 `delete`。

### 修改、换 Key、删除

```bash
python3 cpa-tools/api_keys.py rename client_a client_b
python3 cpa-tools/api_keys.py rotate client_b
python3 cpa-tools/api_keys.py delete client_b
```

- 改名保留 Key、完整 id 和历史记录。本机已导出的变量名不会自动变更；可重新导出。
- 换 Key 会立即替换允许列表中的旧值，再更新原 env 中对应变量，调用端需重新加载。旧 Key 历史保留为 `名称-retired-id前缀`；新 Key 从新 id 开始计量。**历史不自动合并**，避免误把两把 Key 当作同一把。
- 删除只撤销调用权限，保留历史和标签。`list` 显示 `active: false`；用原名称或完整 id 仍可查询历史。最后一个 Key 不允许删除，避免清空配置改变鉴权行为。
- 换 Key、删除默认需要终端输入 `yes`。自动化可显式传 `--yes`，操作者应先核对对象。
- 显示名重复时，用 `list` 中的完整 id 选择。已退休标签也占用名称，避免混淆历史。

通过 v8 `config/access/api-keys` 更新客户端列表，CPA 热加载，不重启 CPA。observer 的标签同步后重启 observer；不改动 Nginx、上游凭据、数据库或 HMAC secret。工具在 VPS 使用同一把运维锁，并在修改前备份、检查并发变化。不要同时在其他管理面板修改同一列表。[CPA v8 官方配置接口](https://help.router-for.me/management/apiv8)

### 查询用量

```bash
python3 cpa-tools/api_keys.py usage client_a --period day
python3 cpa-tools/api_keys.py usage client_a --period month --date 2026-10
python3 cpa-tools/api_keys.py usage --period all
```

查询显示请求数、失败数、token 分项、模型明细和美元估算区间；日/月边界使用 **Asia/Shanghai**。管理员通过 SSH 读取已有 observer 的 SQLite 汇总，不消耗或弹出 CPA 用量队列。默认查询当前北京时间日。

费用为 API 标价估算，不是 Claude 订阅实际扣款或现金余额。缺失价格/计量时报告未知，不能当作零。采集开始前的历史不可恢复；既有内存队列在故障期间有事件丢失风险，不是严格财务账本。详细口径见 [observer README](../cpa-observer/README.md)。

### 在本机加载已有 Key

```bash
python3 cpa-tools/api_keys.py export client_a --name client_a
source /PRIVATE/EXISTING_DEPLOYMENT/.cpa-env
```

导出通过加密 SSH，默认只更新原 env 的目标变量；可选 `--out` 才写入其他文件。若仅供普通下游用户查询，应给用户其自己的 Key 和 Base URL，使用现有 `cpa-observer/query.py`；不要分发此管理员 SSH 配置。

### 号池和账号额度：用户匿名，管理员对应邮箱

**普通用户只需 HTTPS Base URL 和自己的 CPA API Key**，不需要 SSH 或管理 Key。接口是 `GET /v1/quota`，以 `Authorization: Bearer` 鉴权，返回匿名号池和每个匿名上游账号的五小时/七天等限额窗口：已用百分比、剩余百分比、重置时间、数据新鲜状态。

本机已加载原 env 时可运行：

```bash
python3 cpa-tools/api_keys.py quota --key-env CPA_API_KEY_CLIENT_A
```

如果没有本机部署配置，也可显式给 Base URL。Key 从指定环境变量读取，不把 Key 值写入命令参数：

```bash
python3 cpa-tools/api_keys.py quota --base-url https://CPA_HOST --key-env CPA_API_KEY_CLIENT_A
```

Base URL 默认取 `CPA_BASEURL`。不指定 `--key-env` 时，读取 `CPA_API_KEY` 或提示隐藏输入。可加 `--json` 获取完整匿名结构。这条客户端路径不会加载私密部署配置或发起 SSH；原有 `cpa-observer/query.py quota --client client_a` 也继续可用。

**管理员在 TUI 按 `g`**，或运行：

```bash
python3 cpa-tools/api_keys.py quota --admin
```

TUI 所有页面使用同一黑底主题。额度面板按账号分组，每个五小时/七天等窗口以**剩余额度进度条**展示，旁边保留精确百分比和北京时间重置时间。填充越多代表剩余越多：绿色 >50%、黄色 >20%、红色 ≤20%；旧值或禁用账号显示灰条，未知值显示“未知”，不伪装成零或满额。颜色不可用时仍有条形长度、数值及状态标识。窗口较多可用 ↑↓、PgUp/PgDn 滚动，Enter/Esc 返回。普通用户的 CLI 查询也显示匿名条形；`--json` 保持原始结构。

管理员路径通过 SSH 读取 observer 已保存的 SQLite 快照，再将匿名账号 ID 与 CPA 管理元数据匹配，显示“账号邮箱 → 各窗口余量”。仅在私有 SSH 返回中补充邮箱，不修改公网 `/v1/quota` 响应，不增加公网管理接口。身份匹配缺失时保留匿名 ID，不猜邮箱。管理员结果包含私人账号信息，不用于公共报告。TUI 初次加载预取快照，按 `g` 直接显示；面板 `r` 在后台读取新快照，保留当前条形。读取快照不触发上游查询、模型请求或 token 刷新。页面显示快照时间；上游数据由 observer 按既有周期采集。

当前下游 Key 共用号池，没有固定绑定某个上游账号。因此，用户可看每个匿名账号余量，但不能将其中一个声称为“这个 Key 的专属账号”。`/v1/usage` 则只返回当前 Key 的已采集调用用量；它和剩余额度是两个接口。多账号额度百分比不可相加，不换算成剩余 token 或现金；过时数据标为旧值，缺失值保持未知。

## 4. 故障处理与回滚

| 现象 | 处理 |
| --- | --- |
| SSH 失败或主机指纹未知 | 核对私密配置和普通 SSH；人工确认指纹，不关闭检查 |
| 54545 被占用 | 结束自己旧的登录会话，确认无其他登录任务后再 resume；不要随意杀进程 |
| 授权错账号 | 不启用；用同一会话重新授权。若错误凭据已落地并阻止续接，保留现场并按主指南核查，不删除其他账号 |
| 模型 403/429 或 model 不存在 | 模型凭据保持禁用；核对订阅、模型名与上游限制，稍后 resume，不借旧账号验证新 token |
| profile 403 / quota 未返回窗口 | 不报告完整成功；核对授权范围，必要时重新获得同账号资料授权 |
| 绑定后 quota 不完整 | 自动禁用新账号并恢复绑定前配置；查看结果中的备份路径及 VPS 服务状态 |
| Key 变更超时 | 先 list 和 status 核实状态；不要盲目重复新增或换 Key |
| 用量为零或未知 | 核对 collection_started_at、collector_status、last_collected_at 和价格覆盖 |

VPS 默认备份位于 `/var/backups/cpa-tools/`，目录 `700`，凭据和配置 `600`。Key 变更备份包含旧允许列表与 observer 配置；授权绑定还包含两份凭据和 SQLite backup API 快照。工具正常失败会尝试回滚；进程强杀、主机断电或并发外部写入需要管理员根据备份核实恢复，不把多服务操作承诺成原子事务。

不要直接复制活动 WAL 数据库主文件作为备份。回滚身份绑定时保持新账号禁用；回滚 Key 列表时确认不会恢复本来应撤销的 Key。不随意恢复旧 SQLite 覆盖新用量。保留最近可用备份；清理前由操作者确认保留范围。

## 5. 私密文件与交付范围

不要分享 `.deployment.private.json`、`state/`、`exports/`、env 文件、授权链接、原始 CLI 输出、VPS 备份。`.gitignore` 仅防止通常的 Git 误加入，**不会保护手工压缩包或云同步**。分享时只选择 `.py` 源码、README 和泛化指南。

会话持有邮箱、配置、状态和未知到期信息。活跃授权 URL 在接收进程退出后移除；凭据接收失败后的受保护恢复文件保留。未确认恢复价值前不要删除。终端链接本身也是短期敏感信息，不粘贴到公共日志。

## 6. 维护记录与验证范围

2026-10-26（Asia/Shanghai）：两个自助入口、按 Key 用量、用户匿名/管理员邮箱剩余额度视图已实现。新 Key 统一前缀，env 统一 export 写法并复用原文件，写操作结果集中显示一次绝对路径。

背景故障来自额度页单独调用 `use_default_colors()`，与主屏黑底冲突。现由 `tui_theme.py` 统一配色。查询先显示预取快照，更新在后台完成；不在管理员查询链路中再次验证下游 Key 或访问上游。

| 检查 | 结果 |
| --- | --- |
| Python 语法、帮助、文档链接 | 已检查 |
| 深色主题、进度条、旧值、未知窗口 | 已离线预览 |
| 现有 VPS 只读额度查询 | 已读取；输出仅保留耗时和账号数量 |
| 生产 Key 写入、双授权组合、故障回滚 | 本次未执行 |
| 新 VPS 完整部署 | 按主指南验收表独立执行 |

字符裁剪局部计时：典型表格行 1000 次由约 680 ms 降至 42 ms。该数字只计本机文本处理。SSH 首次握手和 VPS 执行耗时分别记录，不写成整体提速比例。
