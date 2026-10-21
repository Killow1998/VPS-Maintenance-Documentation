# 干净构建的 XBoard 镜像

[返回目录](../README.md) · [完整方案](05-xboard-solution.md) · [首次安装](02-xboard-deploy.md)

## 1. 交付对象和状态

本目录的[构建配方](../build/xboard/Dockerfile)只使用固定提交的公开 XBoard 源码、前端子模块、官方 PHP/Composer 镜像及校验过的扩展源码。不使用生产容器 commit、数据库、环境文件、订阅、用户或私钥。镜像未初始化，没有默认管理员密码；每台新机必须独立安装并生成凭据。

固定输入及镜像 tag 以 [inputs.json](../build/xboard/inputs.json) 为准。当前目标为 **Linux/amd64、PHP 8.2.35**；不宣称支持 ARM64，也不保证此版本永远最新。基础镜像用 digest，主源码及前端用 commit，Composer 使用公开锁文件。Alpine 软件仓库等仍可能变化，因此这是输入可追溯构建，不是逐字节可重现性保证。

本次新增的是干净源码构建，不是当前服务器旧应用版本的无差异 PHP 替换。现有服务器升级前必须审阅应用差异、数据库迁移和回滚，不能因为 PHP 相同就直接覆盖。

发布入口：[GitHub Releases](https://github.com/Killow1998/VPS-Maintenance-Documentation/releases)。只有出现该 tag 且校验、烟测报告均通过后才算已发布。构建中的 Actions 或仓库里有 Dockerfile，不等于已经有可用镜像。

## 2. 新 VPS 使用已构建镜像

优先使用 Release 的公开归档，不要求 VPS 自行编译。选择与 `inputs.json` 对应的明确版本，不盲目使用 `/latest`。

下载该 Release 中的：

- `xboard-clean-linux-amd64.tar.gz`：可供 Docker load 的未初始化镜像。
- `SHA256SUMS`：归档及其余公开资产的 SHA-256。
- `manifest.json`：实际 PHP、镜像身份、安装/HTTP 测试及扫描结果。
- `provenance.json`：构建输入与对应源码文件摘要。
- `xboard-upstream-source.tar.gz`：对应公开源码和前端，保留上游许可证。

在私有部署目录取得文件后验证校验和，再加载；文件名须与 checksum 文件一致：

```bash
sha256sum --check --ignore-missing SHA256SUMS
docker load --input xboard-clean-linux-amd64.tar.gz
```

确认实际镜像的 image ID 与该 Release 的 `manifest.json` 一致，并检查 PHP，而不是只相信文件名。校验和不能替代可信发布来源或签名；本流程没有宣称完成签名/第三方认证。

Release 中的镜像保留其具体 GHCR tag，加载后该 tag 可在本机使用，无需注册表登录。将它设为 `XBOARD_IMAGE`，按 [Compose 参考条件](05-xboard-solution.md#compose-参考的使用条件)和首次安装流程初始化新的 SQLite、Redis 与管理员。不要使用别人初始化后的 `.env` 或数据库。

GHCR 推送状态见 Release 的 `registry-status.json`。推送成功不等于允许匿名拉取；GitHub 新包可能默认私有，见[官方说明](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)。匿名访问验证通过前，不把 `docker pull` 当作唯一安装途径。公开 Release 归档不依赖包的可见性。

## 3. 构建、测试和发布机制

[工作流](../.github/workflows/clean-xboard.yml)使用仓库的短期 `GITHUB_TOKEN` 发布，不需要把个人 PAT、服务器密码或生产 secrets 填到仓库。修改构建目录后推送 main 会触发；同一 Release tag 已存在时拒绝覆盖，需要明确更换版本标识。

执行顺序：

1. 从固定公开 commit 拉取源码和前端；只复制 Git 跟踪文件，不带 `.git`、真实 `.env` 或数据库。清空公开 `.env.example` 中的 APP_KEY。
2. 使用独立 BuildKit，限制 **2 CPUs / 4 GiB RAM / 5 GiB 合计 RAM+swap**、并行构建阶段为 1，扩展编译任务为 2。结束时只删除本次 builder/cache，不全局 prune。
3. 验证实际 PHP、扩展和 Composer 平台要求。镜像内保留 XBoard 及依赖许可证，不对上游软件重新归属版权；应用 MIT 标签不替代其余系统包和依赖的各自许可证。
4. 在独立临时目录完成全新 SQLite 安装、面板 HTTP、管理员登录、空节点订阅 YAML 和 Horizon 检查。所有管理员/密钥仅存在于临时数据，测试容器从不 commit，结束删除测试数据。
5. 导出原始镜像，对镜像配置及可读取的每一层检查环境文件、数据库、日志和高置信度秘密模式。扫描每个普通文件的前 2 MiB；不是完整漏洞、恶意代码或秘密审计，不代表绝对安全。
6. 只在测试/扫描通过后生成归档、对应源码、证据和校验和，再发布 Release；GHCR 是额外渠道，状态单独记录。

烟测只针对面板镜像，不包含真实节点转发、生产用户计量、HTTPS 公网证书或新 VPS 整套网络部署验收。后续仍按主教程分别验证这些链路。

初次检查发现上游扩展安装产物含公开测试/示例私钥，以及依赖 README 中的示例密钥块。配方在生成相应镜像层之前删除不参与运行的测试目录、示例目录及该 README，保留依赖 LICENSE；构建生成的应用日志也在同一层提交前清除。不能只在后续层删除，然后把历史层里的内容当作已消失。二进制库中的密钥格式识别字符串不是密钥，扫描要求匹配完整密钥块，而不是只出现 BEGIN 标记。

## 4. 手动构建

需要相同架构、已有 Docker/Buildx/Python 3 的 Linux 构建机；预留至少 12 GiB 磁盘和足够内存，不在 1 GB 生产 VPS 编译。先审阅源码与脚本，再从仓库根目录执行：

```bash
work=$(mktemp -d)
python3 -u build/xboard/build.py "$work"
```

具体镜像名和 image ID 见 `$work/build-result.json`。使用该文件中的实际镜像名继续运行，不把占位符原样执行：

```bash
python3 build/xboard/smoke.py IMAGE_REFERENCE "$work/smoke.json"
python3 build/xboard/scan.py IMAGE_REFERENCE "$work"
python3 build/xboard/package.py "$work"
```

`$work/build.log` 为本次构建日志，发布内容只来自 `$work/release` 的白名单资产。没有通过测试就不上传；排障时保留证据但不公开临时安装日志或环境文件。完成发布并确认可下载后，再清理本次独立临时目录，不删除其他镜像或服务。

## 5. 给部署 Agent 的更新要求

```text
先读取本仓库 docs/06-clean-xboard-image.md 和 inputs.json。
新 VPS 优先下载明确版本 Release 中已构建的 Linux/amd64 镜像，
验证 SHA256SUMS 与 manifest 的 image ID，再 Docker load；不要重新编译。
只有镜像过时、不兼容或明确要求重建时，才在资源充足机器限额构建。
不要假设 GHCR 可匿名拉取，失败时使用公开 Release，不索取用户 PAT。
镜像没有管理员、节点或用户；按部署教程独立初始化和生成私有凭据。
订阅规则由 XBoard 发布，客户端得到链接，不导入公开片段文件。
已有服务器不得直接套用新应用镜像；先备份并检查迁移及兼容性。
```
