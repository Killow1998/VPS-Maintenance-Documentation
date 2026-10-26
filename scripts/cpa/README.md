# CPA 原生工具包

从仓库根目录阅读 [VPS + CPA + Claude 部署指南](../../docs/09-cpa-claude.md)。本目录只提供源码和示例，不包含任何部署数据。

| 位置 | 用途 |
| --- | --- |
| `cpa-tools/claude_auth.py` | setup-token + profile/quota 双授权 |
| `cpa-tools/api_keys.py` | Key TUI、用量、剩余额度 |
| `quota.sh` / `quota.ps1` | 独立查询自己的用量、近期调用账号与匿名号池余量 |
| `cpa-auth/` | 受保护的授权进程 |
| `cpa-observer/` | VPS 原生采集器、SQLite、客户端接口 |
| `cpa_verify.py` | 显式运行的鉴权和真实生成验证 |

本机使用 Python 3.9+，macOS/Linux 或 Windows WSL。普通用户额度查询无需 SSH；管理员需现有 SSH 和对应权限。运行命令示例见 [自助 SOP](cpa-tools/README.md)。

临时查询只需运行一个文件：`quota.sh` 使用 Bash + Python 标准库，`quota.ps1` 使用原生 PowerShell 5.1+。必须指定本机 HTTP/mixed 代理，缺少代理或连接失败不回退直连。默认同屏显示当前 Key 的近 1h/7d/30d/总历史与号池余量，TUI 模式支持 R/Enter 刷新、Q 退出；每次仅两次只读请求。指定具体期间可查询匿名账号汇总、近期明细和计价。新总览需要新版 observer，历史仅限实际保留记录；号池百分比与 token 用量不可混算。固定版本下载、强制代理边界及选项见[部署指南第 9.3 节](../../docs/09-cpa-claude.md)。

离线验收：在仓库根目录运行 `python -B -m unittest discover -s scripts/cpa -p test_quota_clients.py -v` 以及 `python -B -m unittest discover -s scripts/cpa/cpa-observer -p test_observer.py -v`。仅使用合成凭据、临时 SQLite 和本机测试端口；安装了 PowerShell 时一并验证其客户端。

私密配置用 `--config /PRIVATE/deployment.json` 或 `CPA_TOOLS_CONFIG` 指定，放在克隆目录之外。默认配置路径是便利入口，不是公开模板。不要把私密 env、state、密钥、OAuth 文件、数据库和 VPS 备份复制到本目录。
