# CPA 工具包

管理 Claude 账号与 API Key，查询模型用量和账户池额度。

- **查询自己的用量**：[一键启动](../../README.md#cpa-一键查询)。
- **管理账号和 Key**：[运维工具手册](cpa-tools/README.md)。
- **部署新服务**：[CPA/Claude 部署指南](../../docs/09-cpa-claude.md)。
- **更新已有采集服务**：[observer 更新说明](cpa-observer/README.md#repair-prompt)。

## 运行环境

Python 工具统一使用 **Python 3.10+**。管理员工具在 Linux、macOS 或 Windows WSL 中运行，并通过 SSH 连接 VPS；本机和 VPS 均需检查 Python 版本。

Bash 入口会查找本机已有的 Python 3.10+（包括版本化命令），不安装软件或修改系统 Python。

个人查询使用 `quota.sh`（Bash、Python）或 `quota.ps1`（PowerShell 5.1 或 7）。启动后输入本机代理端口、HTTPS Base URL 和 API Key。

## 工具列表

| 入口 | 用途 |
| --- | --- |
| [claude_auth.py](cpa-tools/claude_auth.py) | 新增 Claude 账号，完成模型调用与额度查询授权 |
| [api_keys.py](cpa-tools/api_keys.py) | 管理 Key，查询用量和账户池 |
| [quota.sh](quota.sh) / [quota.ps1](quota.ps1) | 查询当前 Key 的模型用量、估算费用、缓存命中率和共享余量 |
| [cpa-observer](cpa-observer/README.md) | 采集用量、保存 SQLite 记录、提供查询接口 |
| [cpa-admin](cpa-admin/README.md) | 可选的网页/TUI 管理与权限网关，限制每个 Key 可使用的 Claude 账号，保留官方 CPA |
| [cpa-auth](cpa-auth/README.md) | 授权流程的底层工具 |
| [cpa_verify.py](cpa_verify.py) | 验证鉴权和模型生成；生成测试会消耗用量 |

## 查询看板

看板展示近 1 小时、7 天、30 天和历史用量。输入 `1/2/3/4` 切换模型明细，`N/P` 翻页，`R` 或回车刷新，`Q` 退出。切换和翻页使用已取得的数据。

首次下载启动器使用当前网络；随后脚本下载及 API 查询使用指定代理，连接失败即停止。成功查询后询问是否保存到 `~/.cpa/query.json`；选择保存后，下次启动可直接载入或重新输入。未确认则不写入 Key。完整命令及选项见[使用指南](../../docs/09-cpa-claude.md#93-使用者只用-base-url--key-查询)，统计含义见[用量与费用说明](../../docs/09-cpa-claude.md#94-用量与费用口径)。

## 配置与测试

私密配置保存在仓库外，通过 `--config /PRIVATE/deployment.json` 或 `CPA_TOOLS_CONFIG` 指定。凭据、授权文件、数据库及服务器备份也应保存在私有目录。

在仓库根目录运行测试：

```bash
python -B -m unittest discover -s scripts/cpa -p 'test_quota_*.py' -v
python -B -m unittest discover -s scripts/cpa/cpa-observer -p test_observer.py -v
python -B -m unittest discover -s scripts/cpa/cpa-admin -p 'test_*.py' -v
python -B -m unittest discover -s scripts/cpa/cpa-tools -p 'test_*.py' -v
```

测试使用模拟凭据、临时数据库和本机接口。安装 PowerShell 后可同时验证 Windows 客户端。
