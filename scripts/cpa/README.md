# CPA 原生工具包

从仓库根目录阅读 [VPS + CPA + Claude 部署指南](../../docs/09-cpa-claude.md)。本目录只提供源码和示例，不包含任何部署数据。

| 位置 | 用途 |
| --- | --- |
| `cpa-tools/claude_auth.py` | setup-token + profile/quota 双授权 |
| `cpa-tools/api_keys.py` | Key TUI、用量、剩余额度 |
| `quota.sh` / `quota.ps1` | 独立临时查询匿名号池，无需下载 TUI |
| `cpa-auth/` | 受保护的授权进程 |
| `cpa-observer/` | VPS 原生采集器、SQLite、客户端接口 |
| `cpa_verify.py` | 显式运行的鉴权和真实生成验证 |

本机使用 Python 3.9+，macOS/Linux 或 Windows WSL。普通用户额度查询无需 SSH；管理员需现有 SSH 和对应权限。运行命令示例见 [自助 SOP](cpa-tools/README.md)。

临时额度查询只需运行一个文件：`quota.sh` 使用 Bash + Python 标准库，`quota.ps1` 使用原生 PowerShell 5.1+。两者只读取 Base URL 和自己的客户端 Key，显示匿名账号的剩余额度条、北京时间重置时间和短倒计时。可直接从 GitHub 固定提交读取执行，不在本机保存脚本；命令见[部署指南第 9.3 节](../../docs/09-cpa-claude.md)。

私密配置用 `--config /PRIVATE/deployment.json` 或 `CPA_TOOLS_CONFIG` 指定，放在克隆目录之外。默认配置路径是便利入口，不是公开模板。不要把私密 env、state、密钥、OAuth 文件、数据库和 VPS 备份复制到本目录。
