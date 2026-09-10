# Google Scholar 专用分流

[返回目录](../README.md) · 前置：[基础服务已验收](02-xboard-deploy.md)

## 1. 是否需要这一步

只在用户明确需要 Google Scholar 时进入。先保持已有节点开启，访问 Scholar 并手动搜索一次：

- 能正常返回论文：保留原出口，不因为本仓库附带模板就强行部署 NAT64。
- DNS、TLS 或连接超时：先定位网络问题，不把它当成 Google 的反自动查询限制。
- 出现 `We're sorry ... automated queries`：记录是哪个出口、哪个客户端和具体请求受限，再评估方案。

Google 可能对网络来源的异常流量进行限制；报错不能证明具体某位用户运行了爬虫。参见 [Google 的说明](https://support.google.com/websearch/answer/86640)。只打开首页、只清 Cookie、只换 DNS 或只测普通 Google 搜索，都不足以验证 Scholar 搜索问题已经解决。

## 2. 路径和适用条件

```text
用户浏览器
  -> 原有客户端规则与 VLESS 节点
  -> 服务器原生 nat64-scholar 出站
  -> VPS IPv6 网络
  -> 公共 NAT64 网关
  -> Google Scholar IPv4 服务

其他目的地 -> 原有服务器路由
```

需要同时满足：

1. VPS 有实际可用的原生 IPv6 出站，不只是分配了地址。
2. 能通过 IPv6 到达 DNS64 解析器和它返回的 NAT64 地址。
3. 原出口搜索确实有问题；缺少可用原生 AAAA 时可评估 DNS64，而不是假定打开客户端 IPv6 就能解决。
4. 所装 xboard-node / 内核支持下面的 schema，并且用户同意使用第三方共享服务。

公共 NAT64 会改变匹配请求的出口，但不解密 HTTPS，也不会让共享出口获得永久可信身份。网关可能记录连接信息，服务和 Google 接受状态都可能变化。仅用于正常低频人工检索，不批量抓取、不轮换出口规避频率限制。先阅读 [NAT64 服务](https://nat64.net/)及[服务条款](https://nat64.net/tos)。

无需为这条路径安装 WARP。WARP 也不是保证 Scholar 可用的服务；若已有 WARP，只把它作为需单独验证的原有路径，不假设一定可回滚到它。

## 3. 版本边界

模板对应的验证组合是 `xboard-node v1.13`、内嵌 `sing-box 1.13.2`，节点源码参考提交 `0a29338e1f102a462363ce3527417029f89bab28`。这用于解释配置兼容性，不是要求未来服务器强制降级。

该版本中，面板自定义出站白名单不能直接接受原生 `direct` 出站，因此使用 `kernel.custom_config` 加载原生 DNS 和出站，再由面板路由引用标签。

- 原生配置中的 `dns` 会替换生成配置的 DNS 部分。
- `outbounds` 追加，不能使用与已有出站重复的 tag。
- `route` 合并子字段；原生规则和面板规则有优先级关系，需检查最终生成结果。
- 已有 `custom_config` 时应合并到原文件，不覆盖其他功能，也不创建两个同名 YAML 字段。

依据：[配置合并实现](https://github.com/cedar2025/Xboard-Node/blob/0a29338e1f102a462363ce3527417029f89bab28/internal/kernel/singbox/config.go)、[配置定义](https://github.com/cedar2025/Xboard-Node/blob/0a29338e1f102a462363ce3527417029f89bab28/internal/config/config.go)、[解析器选项](https://sing-box.sagernet.org/configuration/shared/dial/)。不同内核或版本应重新验证，不能把 sing-box JSON 原样用于 Xray。

## 4. 备份与候选测试

修改前按[在线备份流程](04-operations.md#2-在线备份)保存数据库，同时保存节点 YAML、原生配置、目标节点的 `custom_routes` 和 `custom_outbounds`。这些文件不能进入公开仓库。

使用[原生模板](../templates/scholar-nat64.json)。其中 `2a00:1098:2b::1` 是公共 DNS64 服务地址，不是某个用户 VPS 的地址；应以服务运营者当时的可用信息重新确认。

从仓库目录上传候选文件，替换新 VPS 的地址、用户名和 SSH 端口：

```powershell
scp -P 17724 .\templates\scholar-nat64.json root@SERVER_IP:/root/scholar-nat64.candidate.json
```

在 VPS 上检查语法：

```bash
python3 -m json.tool /root/scholar-nat64.candidate.json > /dev/null
```

JSON 语法检查不等于 sing-box 配置校验。助手应从官方取得与内嵌版本匹配的发行包，核对可用的校验信息，在临时 loopback SOCKS 入站中组合测试配置：保留该模板 DNS 和出站，显式将测试请求路由到 `nat64-scholar`。不对公网开放测试端口。

先检查内核 schema，再通过这个临时入口做一次真实搜索，检查返回论文标题和链接。不能只用普通 `curl -6` 测试替代，因为 Scholar 可能没有原生 AAAA，需要 DNS64 合成地址。

候选失败就保留生产原配置，记录失败阶段。不要边试边覆盖已使用的原生文件。

## 5. 安装出站，再改面板路由

候选通过且确认目标文件不存在或已经备份后，在 root 运行的节点服务上安装：

```bash
install -o root -g root -m 0600 /root/scholar-nat64.candidate.json /etc/xboard-node/scholar-nat64.json
```

若服务以非 root 用户运行，应改为仅服务身份可读的属主和权限，不得为方便而开放给所有用户。

在目标实例已有的 `kernel` 中加入以下字段，其他配置保持不变：

```yaml
custom_config: /etc/xboard-node/scholar-nat64.json
```

安排可能短暂断流的维护窗口。已核对版本监视主 YAML；单改引用的 JSON 不保证触发重载。必要时受控重启服务，并检查出站已成功加载。进程 PID 不变也不代表内部服务没有重新启动。[主配置监视实现](https://github.com/cedar2025/Xboard-Node/blob/0a29338e1f102a462363ce3527417029f89bab28/cmd/xboard-node/main.go)

然后在目标节点 `custom_routes` 的原生规则数组中增加或替换下面的元素：

```json
{
  "domain": ["scholar.google.com"],
  "domain_suffix": ["scholar.googleusercontent.com"],
  "outbound": "nat64-scholar"
}
```

不要套一层 `custom_route_rules`；不要清空原数组。检查旧 Scholar 封禁或备用出口规则，以及优先级更高的结构化规则是否抢先匹配。其他现有规则保持不变。

通过面板支持的保存入口，或该版本 `App\Models\Server` 的 `save()` 应用，使观察者推送配置。直接更新 SQLite 不保证运行中的节点得到通知。助手需要读取实际面板版本后选择接口，不臆造通用 API 路径。

规则只覆盖上述 Scholar 域名，不把整个 `google.com`、登录和其他 AI 访问改为 NAT64。若客户端把 Scholar 设为直连，请求不会进入节点，服务端规则当然不起作用。

## 6. 从真实客户端验收

检查服务日志没有未知字段、出站不存在或加载失败，再通过用户实际使用的代理发起一次搜索。例如 Windows 本地代理监听 7890 时：

```powershell
curl.exe --proxy http://127.0.0.1:7890 --connect-timeout 10 --max-time 30 --output "$env:TEMP\scholar-check.html" --write-out "HTTP %{http_code}, time %{time_total}s\n" 'https://scholar.google.com/scholar?hl=en&q=visual+localization'
```

输出文件只是临时诊断页面，不是订阅文件，不要求用户导入。检查真实论文结果、链接及是否仍有自动查询提示；HTTP 200 也可能不是正常结果页。

最后由用户浏览器实际搜索确认，并测试一个普通网站仍走原有出口。普通 IP 查询网站的结果只代表那一次请求，不能推断 Scholar 或所有网站的路径。不要用高频自动 Scholar 查询做健康检查。

订阅链接、用户 UUID 和 REALITY 参数不因此改变；通常无需客户端重新导入或启用 IPv6。记录本次测试时间和结果到私有运维记录，并删除临时测试进程、候选文件和无用响应文件。

## 7. 回滚与后续维护

回滚顺序：

1. 将目标节点的 Scholar 路由恢复为本次备份中的原规则；原来没有特例就删除本次新增元素。
2. 通过面板模型保存并验证配置已推送，保留当前用户、额度和计数。
3. 确认不再引用 `nat64-scholar`，才移除本次原生配置。若共用一个 `custom_config`，只撤回此次增量。
4. 验证原有节点和订阅仍可用；恢复旧出口不等于旧出口一定能搜索 Scholar。

不要为了回滚一个标签恢复整份旧数据库，否则会覆盖备份后的用户和流量记录。公共 DNS64 地址、内核 schema 或 Google 接受状态变化时，重新验证，不承诺一劳永逸。
