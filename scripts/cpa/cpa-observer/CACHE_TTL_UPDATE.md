# 缓存时长采集与费用更新

本次更新包含查询客户端、observer 和 CPA 透传补丁。界面更新无需重启 VPS；要按实际缓存时长计价，需补齐后两项。

## 数据怎样流转

Claude 响应中的 `usage.cache_creation` 分别报告 `ephemeral_5m_input_tokens` 和 `ephemeral_1h_input_tokens`。同一次请求可以同时包含两种写入。只看客户端设置的 `ttl`，无法确定实际写入量。[官方字段说明](https://platform.claude.com/docs/en/build-with-claude/prompt-caching#1-hour-cache-duration)

本文基线 [CPA v8.0.42](https://github.com/router-for-me/CLIProxyAPI/tree/v8.0.42)，提交为 `bc0643405a1d0adf77dd0f109972ac3313d66397`。该版本的 usage Detail 和队列只保留写入总量。所附[补丁](patches/cpa-v8.0.42-cache-ttl.patch)补齐：

1. 从普通响应和流式响应提取上游的两项写入量。
2. 流式输出后续只提供输出用量时，保留已收到的缓存分项，不能重复相加。
3. 通过 `tokens.cache_creation` 送入现有用量队列。
4. observer 校验两项为非负整数，且合计等于 `token_breakdown.input.cache_write_tokens`，再存入 SQLite。

新增 `cache_ttl` 附表保存分项和当时计算的费用。原 `records` 的 19 列、Key 哈希、记录 ID 和历史金额均保留；旧 observer 仍可读写原表。只持久化数字，不保存请求、回复或图片。

## API 与显示

- `cache_write_5m_tokens`、`cache_write_1h_tokens`：实际采集的写入量。某期间含缺失记录时，该期间对应分项为 `null`，不能用已知部分冒充完整总量。
- `estimated_cost_usd`：一个美元估算值。普通输入、缓存读取、两种缓存写入、输出分别计价，避免重复累计。
- `cache_ttl_unknown_requests`：有缓存写入、但未收到有效时长分项的请求数。
- 原有 `estimated_cost_usd_low/high` 继续提供。收到有效时长时，两者都等于按实际时长计算的估算值。

旧记录没有缓存时长，不能补造。其单值估算沿用原有较高金额，按 1 小时写入费率计算，客户端标 `~`。缺少模型价格或 token 计量时仍显示未知；`*` 仍表示费用只包含部分请求。这是 API 标价估算，特殊服务档位、工具费用和实际订阅扣费仍以供应商账单为准。

## 维护 Agent Prompt

```text
按本仓库 scripts/cpa/cpa-observer/CACHE_TTL_UPDATE.md，更新已有 CPA 的缓存时长采集，不重新安装。

先只读检查实际 CPA 版本、构建来源、observer 的 ExecStart、Python 版本、数据库和当前采集状态。
保留 Key、OAuth、账号权限、HMAC secret、用量数据库、已有价格文件和 HTTPS/代理设置。
不要改 Claude Code 的缓存配置，不要新增队列消费者，也不要手动 pop 用量队列。

先备份正在使用的 CPA 二进制、observer.py 和配置；SQLite 使用 backup API 生成一致副本，不能只复制主文件而忽略 WAL。
记录原二进制和脚本的 SHA256、服务路径和回滚步骤。

在构建机检查 CPA 是否已透传 tokens.cache_creation 的两个 TTL token 字段。
若已支持，直接复用并验证，不重复打补丁。
若不支持，本文补丁仅针对 v8.0.42 / bc0643405a1d0adf77dd0f109972ac3313d66397。
先 git apply --check；版本不匹配就按实际源码移植并补测试，不强套、不模糊应用。
在构建机完成 gofmt、以下三个包的测试和二进制构建，不在低内存 VPS 上编译。
测试必须覆盖普通/流式响应、仅含输出的后续帧、混合 5m/1h、缺失字段、显式零和无效分项。
只有构建、测试和校验通过，且用户确认了维护窗口后，才替换 CPA 二进制并重启必要的 CPA 服务。
正在进行的流式调用可能中断，先检查活跃任务。

仅替换 observer.py，保留原价格与私密配置，重启必要的 observer 服务。
核对数据库历史数量、按 Key 隔离、四个期间和各模型总和；确认新增 cache_ttl 表而非重建 records。
通过现有真实请求观察两项 TTL 数值和 estimated_cost_usd，不额外发付费探测请求。
尚无真实请求时明确记为待验证，不能把合成测试当作生产验收。
缺失 TTL 的旧记录保留未知状态；不根据用户设置或推荐值推断历史时长。

查询客户端更新后验证：每 5 分钟后台刷新，R/回车可立即刷新，查询不重叠，刷新时可切换视图，Q 可退出。
所有用户 API 请求继续强制走指定代理，不能失败后改为直连。

回滚时恢复已备份的 CPA 二进制与 observer.py，只重启必要服务。
cache_ttl 附表可保留，无需恢复旧数据库或丢弃升级后新增用量；旧版 observer 仍写原 19 列。
最后记录版本、补丁摘要、测试结果、实际服务重启和仍未验证的内容。私密信息不得推送 GitHub。
```

构建机在已核对的 CPA 源码根目录执行，先替换补丁路径：

```bash
git apply --check /PATH/TO/cpa-v8.0.42-cache-ttl.patch
git apply /PATH/TO/cpa-v8.0.42-cache-ttl.patch
gofmt -w sdk/cliproxy/usage/manager.go internal/redisqueue/plugin.go internal/redisqueue/cache_ttl_usage_test.go internal/runtime/executor/helps/usage_helpers.go internal/runtime/executor/helps/plugin_executor_usage.go internal/runtime/executor/helps/cache_ttl_usage_test.go
go test ./sdk/cliproxy/usage ./internal/redisqueue ./internal/runtime/executor/helps
go build -trimpath -o cli-proxy-api ./cmd/server
```

补丁不包含二进制。队列测试通过 JSON 解码读取 `json.RawMessage`，并覆盖普通响应的混合 TTL 与字段缺失；不能对 RawMessage 直接断言为 map。

2026-10-31（Asia/Shanghai）：在 v8.0.42 / `bc0643405a1d0adf77dd0f109972ac3313d66397` 验证补丁干净应用及 gofmt，使用 macOS amd64 / Go 1.27.3 通过上述三个包的测试，并完成 `CGO_ENABLED=0 GOOS=linux GOARCH=amd64` 交叉构建。后续维护仍须在自己的构建机完成编译、测试和校验后才能部署；这些结果不代替生产 TTL 采集验收。
