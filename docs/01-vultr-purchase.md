# 注册、登录与购买 Vultr

[返回目录](../README.md)

## 1. 先问使用地，不问家庭住址

部署助手先向用户收集以下信息。信息仅用于本次选型，不写进公开仓库。

| 问题 | 用途 |
| --- | --- |
| 主要使用地的国家/地区、城市、运营商 | 筛选机房和测试线路，不需要精确地址 |
| 多人使用时，其余使用者的大致区域 | 避免只对管理员本人线路好 |
| 月预算、人数、月流量、是否并发下载 | 区分能启动服务与足够长期使用 |
| Windows / Android / Linux 等设备情况 | 后续订阅和客户端验收 |
| 是否已购买 VPS，在哪个地区 | 已有服务器先检查，不重复购买 |
| 是否已有域名、是否有必须使用的服务地区 | 决定订阅 HTTPS 和区域约束 |

“用户所在城市”“服务器所在机房”“服务器公网 IP”是三个不同的信息。未购买时没有服务器 IP，不要提前索取不存在的地址。

## 2. 注册并登录

1. 从 [Vultr 官网](https://www.vultr.com/)进入注册页面，核对域名，避免推广页面仿冒。
2. 用户自己输入邮箱和独立强密码，阅读并接受服务条款。
3. 到邮箱完成验证，再登录控制台。
4. 在 Billing 添加本人可用的付款方式，核对是否要求充值及金额。
5. 用户自己完成支付、身份或设备验证，不把支付资料、验证码交给部署助手。
6. 为 Vultr 账号启用双重认证，并把恢复码保存在私有位置。

流程以[官方注册说明](https://docs.vultr.com/platform/create-an-account)为准；[付款方式](https://docs.vultr.com/support/platform/billing/what-payment-methods-do-you-accept)会因账户和地区而异。优惠余额可能有期限和使用条件，不能写成“永久免费”。账号双重认证见[官方说明](https://docs.vultr.com/support/platform/authentication/what-is-two-factor-authentication)。

已有账号直接登录，不需要重新注册。若使用 GitHub / Google 登录，应继续使用对应登录方式，并在身份提供方保护账号；参见 [OAuth 登录说明](https://docs.vultr.com/create-a-new-vultr-account-with-github-or-google)。

## 3. 如何推荐最近且便宜的实例

### 先满足最低条件，再比较价格

本方案的低成本参考：

| 项目 | 参考选择 |
| --- | --- |
| 类型 | Shared CPU |
| 类别 | Cloud Compute / Regular Performance，按当前界面名称 |
| 计算 | 1 vCPU |
| 内存 | 1 GB 起，不把 512 MB 视为等价替代 |
| 磁盘 | 约 25 GB，具体 SSD / NVMe 以所选方案为准 |
| 网络 | 必须有公网 IPv4；同时开通 IPv6 便于后续扩展 |
| 流量 | 约 1 TB/月档位；以账单页的实际额度和计量口径为准 |
| 参考型号 | `vc2-1c-1gb`，库存不足时比较同档而不是盲目升级 |
| 参考预算 | 历史同档约 5 美元/月；下单前重新核价 |

官方使用该型号作为 [1 核、1 GB、25 GB 的部署示例](https://docs.vultr.com/how-to-provision-cloud-infrastructure-on-vultr-using-terraform)。参考预算不是实时价格承诺；[官方说明](https://docs.vultr.com/support/platform/billing/is-pricing-the-same-in-all-data-center-locations)指出不同地区可能有价差，应以控制台所选地区总价为准。

1 GB 是低成本起点，不是任意并发量的性能保证。大用户量、额外应用、日志积压或 OOM 都可能需要更多内存。升级到 2 GB 前先说明额外费用并征得用户同意。

### 机房选择顺序

1. 排除服务不可用或不符合用户区域要求的地区。
2. 按实际使用地挑选 2-3 个相对接近的可售机房；可用地区以控制台为准。
3. 使用 Vultr 官方 Looking Glass / 测试地址做延迟、丢包和小规模吞吐测试，优先从用户自己的网络测。
4. 在满足上述配置且线路可接受的候选中，选总价最低的一个；同价时比较丢包、延迟及晚间表现。
5. 无法测试时，把“按距离初选”写成待验证建议，不伪造测速或宣称某地是全国最优。

例如，东亚用户可比较东亚机房与美国西海岸候选，北美用户先比较附近机房；这只是候选生成方法，不是固定推荐。海缆路径和运营商互联可能使远机房反而更快。Vultr 在[迁移最佳实践](https://docs.vultr.com/best-practices-when-migrating-to-vultr)中也建议用 Looking Glass 测试线路。

助手给用户的购买确认应包含：

```text
推荐：REGION_A / PLAN_A
配置：CPU、内存、磁盘、公网 IPv4、IPv6、流量额度
当前价格：每小时、每月上限、附加项与税费说明
依据：从哪里测的、测了什么、哪些还没测
备选：REGION_B / PLAN_B，差价与线路差异
是否确认购买这一台？
```

没有预算内方案就如实说明，不擅自提高预算，也不一次开多台收费实例。

## 4. 控制台如何选择

在 Products / Compute 进入 Deploy。界面可能变化，按[官方实例创建说明](https://docs.vultr.com/products/compute/instances/cloud-compute/provisioning)核对。

| 页面项 | 操作 |
| --- | --- |
| Choose Type | Shared CPU，不选 GPU / Bare Metal |
| Location | 上一步经用户确认的机房 |
| Plan | 经确认的低价档位，核对每月总价 |
| Image | Operating System，不选预装面板的 Marketplace App |
| OS | Ubuntu LTS x64；本教程以 24.04 为例 |
| SSH Keys | 已添加公钥则从下拉框选择；没有可暂时不选 |
| Startup Script / Cloud-Init | 没有审阅过的脚本不要添加 |
| Firewall Group | 使用已检查的规则，首次 SSH 端口必须可达 |
| Hostname / Label | 自定义中性名称，例如 `vps-node` |
| Connectivity | Instance with Public IP，保留 Public IPv4 |
| IPv6 | 若该方案支持，建议同时启用；不能仅为省钱改成 IPv6-only |
| VPC / NAT Gateway | 单机方案不需要 |
| Backups / DDoS 等附加项 | 逐项核价，预算优先时可不选；不等于无备份或保证抗攻击 |
| Quantity | 1 |

24.04 和 26.04 均列在当前 Docker 的 [Ubuntu 支持列表](https://docs.docker.com/engine/install/ubuntu/)中，但这不能替代对 XBoard、节点发行包及系统服务行为的验证。不必为了“版本最新”偏离已验证的软件组合。

### SSH Keys 下拉框不能粘贴怎么办

这是正常的：先在 Vultr 的 Account / SSH Keys / Add SSH Key 添加本地公钥，再回到部署页选择。[官方添加说明](https://docs.vultr.com/products/orchestration/ssh-keys/add-ssh-keys)

只上传 `.pub` 公钥，不上传私钥。若已经跳过此项完成部署，之后通过第一次 SSH 登录追加公钥；不要为了补密钥而重装已经有数据的服务器。[运行中实例的密钥操作风险](https://docs.vultr.com/how-to-add-and-delete-ssh-keys)

## 5. 部署、收费与交接

用户确认最终 Summary 后点击 Deploy。等待实例显示就绪，再打开详情页检查 IPv4、机房、系统、默认登录用户名和临时密码。

Vultr 按小时累计费用并有相应月度计费上限；停止实例通常仍收费，销毁才停止该实例后续计费。快照等独立资源可能继续计费，已发生费用不会因销毁自动退回。参见[官方实例计费说明](https://docs.vultr.com/support/platform/billing/how-am-i-billed-for-my-servers)。不要让用户为了测试随意销毁仍在使用的机器。

通过私有方式向部署助手提供：

```text
服务器公网 IPv4：SERVER_IP
服务器所在机房：REGION
操作系统：OS_VERSION
SSH 用户：root 或详情页显示的实际用户名
SSH 端口：通常新机为 22，以实际为准
认证：已安装公钥 / 临时密码位于仓库外的私有文件
私有文件路径：由用户在本机指定，不提交到仓库
允许操作的目标：仅此新实例
```

IP 和初始服务器密码可由用户通过受信任的私有渠道交接；更推荐让助手读取本机仓库外的受限文件，或用户在 SSH 提示符输入临时密码。不要把密码放在命令参数、GitHub Issue、公开截图或提交记录中。

不需要提供 Vultr 网站密码、API key、付款资料或 SSH 私钥。交接完成进入[部署步骤](02-xboard-deploy.md)。
