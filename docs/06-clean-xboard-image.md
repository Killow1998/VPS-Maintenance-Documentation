# 干净构建的 XBoard 镜像

[返回目录](../README.md) · [完整方案](05-xboard-solution.md) · [首次安装](02-xboard-deploy.md)

## 1. 交付对象和状态

本目录的[构建配方](../build/xboard/Dockerfile)只使用固定提交的公开 XBoard 源码、前端子模块、官方 PHP/Composer 镜像及校验过的扩展源码。不使用生产容器 commit、数据库、环境文件、订阅、用户或私钥。镜像未初始化，没有默认管理员密码；每台新机必须独立安装并生成凭据。

固定输入及镜像 tag 以 [inputs.json](../build/xboard/inputs.json) 为准。当前目标为 **Linux/amd64、PHP 8.2.35**；不宣称支持 ARM64，也不保证此版本永远最新。基础镜像用 digest，主源码及前端用 commit，Composer 使用公开锁文件。Alpine 软件仓库等仍可能变化，因此这是输入可追溯构建，不是逐字节可重现性保证。

本次新增的是干净源码构建，不是当前服务器旧应用版本的无差异 PHP 替换。现有服务器升级前必须审阅应用差异、数据库迁移和回滚，不能因为 PHP 相同就直接覆盖。

已按早期教程搭建且已有用户时，直接使用[既有 VPS 修复指南及 Agent Prompt](07-existing-vps-security-repair.md)，先判断需要更新 PHP 还是只补外置 INI；不要重新安装或替换用户数据。

发布入口：[GitHub Releases](https://github.com/Killow1998/VPS-Maintenance-Documentation/releases)。只有出现该 tag 且校验、烟测报告均通过后才算已发布。构建中的 Actions 或仓库里有 Dockerfile，不等于已经有可用镜像。

**2026-10-21 已发布并完成匿名取回验证**：[此版本 Release](https://github.com/Killow1998/VPS-Maintenance-Documentation/releases/tag/xboard-clean-20261021-81dd561-php8.2.35-amd64)，[成功的构建与门禁](https://github.com/Killow1998/VPS-Maintenance-Documentation/actions/runs/39653120625)。全新安装、管理员页面与三个静态资源、登录、空节点订阅、Horizon 和 SQLite 完整性检查均通过；逐层检查 23 层、13847 个文件前缀，无剩余门禁命中。新安装启动在 CI 上约 5.8 秒，不是生产负载或所有主机的启动保证。

## 2. 新 VPS 使用已构建镜像

此版本已用空 Docker 认证目录验证实际匿名拉取，优先使用下列固定 digest，不要求 VPS 自行编译或提供 GitHub PAT：

```bash
docker pull ghcr.io/killow1998/xboard-clean@sha256:b0e5ecbaeb1cf15a95ed9aa348b66794308287a09ed9ff7bde5e1f20e9fa03fd
export XBOARD_IMAGE='ghcr.io/killow1998/xboard-clean@sha256:b0e5ecbaeb1cf15a95ed9aa348b66794308287a09ed9ff7bde5e1f20e9fa03fd'
```

可读 tag 为 `ghcr.io/killow1998/xboard-clean:20261021-81dd561-php8.2.35-amd64`；部署固定 digest 更明确。镜像未初始化，拉取并不创建用户、节点或开放端口，之后仍须按主教程配置私有数据和本机面板。

该 Release/digest 没有因后续上传进度加固而重建。拉取后、首次初始化前，按[维护手册 5.2 节](04-operations.md#52-关闭不用的-php-原生上传进度)准备宿主机 INI，并使用含只读挂载的 Compose；单独拉取镜像不能自动得到外置加固配置。已有服务器也不会因仓库模板更新而自动生效，必须另外备份、重建面板并验收。

注册表不可达时，使用该 Release 的公开归档作为备用。选择与 `inputs.json` 对应的明确版本，不盲目使用 `/latest`。

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

继续核对镜像身份与实际 PHP，而不是只相信文件名。Docker 的不同镜像存储实现可能分别以 config digest 或 OCI manifest digest 显示 image ID，不能只比较一个字符串，也不能因此跳过检查。可使用仓库的[跨存储验证脚本](../scripts/verify-clean-image.py)，传入已校验的镜像归档和 `manifest.json`：它会加载镜像，检查归档中的 config/manifest 摘要、RootFS diff IDs、入口/环境等运行配置，并离线启动 PHP 确认版本及未初始化状态。路径按本机仓库位置填写：

```bash
python3 /PATH/TO/REPOSITORY/scripts/verify-clean-image.py \
  xboard-clean-linux-amd64.tar.gz manifest.json
```

校验和不能替代可信发布来源或签名；本流程没有宣称完成签名/第三方认证。

Release 中的镜像保留其具体 GHCR tag，加载后该 tag 可在本机使用，无需注册表登录。将它设为 `XBOARD_IMAGE`，按 [Compose 参考条件](05-xboard-solution.md#compose-参考的使用条件)和首次安装流程初始化新的 SQLite、Redis 与管理员。不要使用别人初始化后的 `.env` 或数据库。

GHCR 推送状态见 Release 的 `registry-status.json`。推送成功不等于允许匿名拉取；GitHub 新包可能默认私有，见[官方说明](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)。匿名访问验证通过前，不把 `docker pull` 当作唯一安装途径。公开 Release 归档不依赖包的可见性。

本次独立取回的镜像归档为 **145069685 bytes**，SHA-256 为 `14bfec1a8e0caa8b1b6f7ca23cfe2f609d3e9f7a2fdd8cb9a86167b93e1a1b44`。CI config digest 为 `sha256:f991acc62d6b8200d0b1fe9539710c86ddefefd43d6dc9fe355c7dfba8cbbb5c`；Docker 29 加载归档后显示的 OCI manifest digest 为 `sha256:2064b827996874e25c03442f97cb8f7567ef7f5bc228e9c1f4ca01911e6de61f`。两者已通过归档关联、RootFS 和运行配置验证，注册表的压缩 manifest digest 则为上面的 `b0e5...`；三种摘要不能混为同一个字段。独立运行确认 PHP 8.2.35、Swoole 6.2.3，镜像中没有真实 `.env` 或应用数据库。

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
新 VPS 优先匿名拉取本页给出的固定 GHCR digest（Linux/amd64），不要重新编译。
注册表不可达时下载对应 Release，验证 SHA256SUMS，使用跨存储验证脚本
核对归档/加载身份和运行版本，再继续初始化；不要只比较 image ID 字符串。
只有镜像过时、不兼容或明确要求重建时，才在资源充足机器限额构建。
此版本的匿名 Docker 拉取已实测；其他版本重新验证，失败时用公开 Release，
不要索取用户 PAT。
镜像没有管理员、节点或用户；按部署教程独立初始化和生成私有凭据。
首次初始化前按 docs/04 的 5.2 节安装外置上传进度加固 INI 并核对挂载；
不改已发布镜像 digest，不把只拉取镜像当作配置已生效。
订阅规则由 XBoard 发布，客户端得到链接，不导入公开片段文件。
已有服务器不得直接套用新应用镜像；先备份并检查迁移及兼容性。
```
