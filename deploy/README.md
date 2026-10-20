# Web 部署与 Windows 安装包 CI/CD

本仓库使用根目录 `.gitlab-ci.yml`。Web 服务部署到 Linux 服务器 `192.168.1.5`，
默认通过 `http://192.168.1.5:8080` 访问，管理前端与 API 由同一容器提供。

## 流程与触发

分支和合并请求运行 Python Ruff、完整离线 pytest（80% 分支覆盖率）、前端类型检查、
测试、构建和部署模板检查。排除 e2e 及仓库规定会清空真实数据的数据库测试。
检查通过后构建含前端的 wheel/sdist；任何检查失败都会阻断发布。

受保护默认分支或受保护 `vX.Y.Z` 标签构建并推送 GitLab Container Registry 镜像。
手动执行 `deploy-production` 发布 Web，使用不可变镜像摘要、就绪检查及原有失败回滚。

同一流水线的 `windows-installer` 为可选手动作业，使用 Windows Runner 构建便携包及
`AgentBot-<版本>-windows-x64-Setup.exe`，并验收真实打包后端的首次 SQLite 启动。
成功后下载该作业的 artifacts，其中有便携目录、安装器、`SHA256SUMS.txt`、
`latest.json` 和一键安装脚本 `install.ps1` / `install.bat`。
缺少 Windows Runner 时不要触发该作业；它不会阻塞 Linux 镜像或部署。

## 现有 Linux Runner

检查现有 Runner 是否采用 **Docker executor**；当前作业使用 Linux 容器镜像，
Shell executor 不能直接执行这套作业。镜像构建需要 privileged DinD 和共享 `/certs/client`，
Runner 的 `config.toml` Docker 段示例：

```toml
[runners.docker]
  privileged = true
  volumes = ["/certs/client", "/cache"]
```

在 GitLab Settings → CI/CD → Runners 中为能访问 `192.168.1.5:22` 的 Linux Runner
添加 `linux-lan` 标签；部署作业使用该标签，普通检查/镜像作业可继续使用能运行无标签作业的
Linux Runner。若现有 Runner 只运行有标签作业，需开启 Run untagged jobs，或为普通作业指定
现有 Linux 标签。可用 `DEPLOY_RUNNER_TAG` 覆盖部署标签。

该地址属于内网，执行部署的 **作业容器**必须能访问它，服务器也必须能访问 GitLab Registry。
privileged 镜像构建 Runner 应只承载可信项目；按现有团队策略保护默认分支和生产环境。

## GitLab 部署变量

在 Settings → CI/CD → Variables 配置部署变量，凭据设为 Protected，环境范围为 `production`。

| 变量 | 类型 | 内容 |
| --- | --- | --- |
| `DEPLOY_USER` | Variable | 可使用 Docker 且可写 `/opt/agentbot` 的 SSH 用户，必填 |
| `SSH_PRIVATE_KEY` | File | 部署专用私钥，保留末尾换行，必填 |
| `SSH_KNOWN_HOSTS` | File | 已核验服务器指纹的 known_hosts 内容，必填 |
| `DEPLOY_HOST` | Variable | 默认 `192.168.1.5` |
| `DEPLOY_PORT` | Variable | SSH 端口，默认 `22` |
| `DEPLOY_PATH` | Variable | 默认 `/opt/agentbot`，绝对路径，不含空格或点 |
| `AGENTBOT_BIND_ADDRESS` | Variable | 默认绑定 `DEPLOY_HOST`，使用反向代理时可设为 `127.0.0.1` |
| `AGENTBOT_HTTP_PORT` | Variable | 宿主机 Web 端口，默认 `8080`，容器内固定 `8080` |

Runner 标签变量 `DEPLOY_RUNNER_TAG` 和 `WINDOWS_RUNNER_TAG` 应使用全环境范围，
默认分别为 `linux-lan` 和 `windows`。更换 Web 地址时同步修改流水线的 environment URL。

脚本验证 SSH 主机密钥；不在 CI 自动信任 `ssh-keyscan` 的输出。服务器部署用户先执行
`docker login <GitLab Registry 地址>`，使用只有 `read_registry` 权限的 Deploy Token。
不要把短期 CI Job Token 作为服务器的长期登录凭据。

## Linux 服务器首次准备

服务器需有 Docker Engine、Compose v2（支持 `up --wait`）、Bash、`flock`、SSH。
放行局域网客户端到 `192.168.1.5:8080`；该映射也支持 `/onebot/v11/ws` WebSocket。
使用现有 HTTPS 反向代理时，设置绑定地址为 `127.0.0.1`，代理保留 WebSocket 升级头。

1. 管理员创建 `/opt/agentbot` 并授予部署用户写权限，将本目录 `runtime.env.example`
   复制到服务器的 `/opt/agentbot/runtime.env`，权限设为 `600`。
2. 替换管理员密码、OneBot Token 和会话密钥。分别运行 `openssl rand -hex 48`
   为 `ONEBOT_V11_ACCESS_TOKEN`、`CHAT_SESSION_SECRET` 生成至少 32 字符的不同随机值，
   两者均写入服务器环境文件，修改后重启服务；容器通过环境变量注入 Token 时页面只读。
   密钥及配置仅保存在服务器，CI 不上传或覆盖它们。
3. 默认业务库与队列为 SQLite，无需外部 MySQL/Redis。只有明确选用它们时才设置对应后端
   与容器可达连接地址；不要复制模板覆盖已有服务器配置。
4. 全新数据库在成功构建镜像后由管理员显式初始化，使用以下命令。已有部署按版本说明
   手动执行增量迁移，日常发布不执行 SQL，不重建数据库。

```bash
# 在上传到服务器的 deploy 目录执行，替换为镜像作业产出的实际摘要。
export AGENTBOT_IMAGE='registry.example/group/agentbot@sha256:实际摘要'
export AGENTBOT_RUNTIME_ENV_FILE='/opt/agentbot/runtime.env'
docker compose --project-name agentbot -f compose.yaml run --rm --no-deps agentbot database init
docker compose --project-name agentbot -f compose.yaml run --rm --no-deps agentbot settings init
```

首次初始化后，数据库是运行配置的权威来源；供应商/MCP 的 URL 与密钥在管理页面保存。
OneBot Token 与会话密钥始终来自服务器环境文件，修改后重启服务；NapCat Token
须与 `ONEBOT_V11_ACCESS_TOKEN` 一致。管理 HTTP API 可修改页面提供且严格授权的静态字段，
保存到可写启动文件后需手动重启；环境变量覆盖的字段只读。本容器部署通过环境变量注入
启动配置，修改服务器环境文件后重启服务。动态运行字段仍写数据库并热刷新。
`HOST=0.0.0.0`、`PORT=8080` 等静态启动参数始终来自服务器环境文件。
固定 `agentbot-data` 卷保存 SQLite、队列、媒体和 TTS 数据，容器以 UID/GID 10001 运行。
会话密钥修改并重启后改变会话与身份命名空间，旧历史和记忆数据保留，供应商与 MCP
密钥不受影响；数据库连接配置修改也只在重启后切换，不迁移数据。


## 添加 Windows Runner

PyInstaller 的 Windows EXE 需要原生 Windows 构建，现有 Linux Runner 不能直接生成本项目
的 Windows/WebView2 桌面包。可以使用一台 Windows 10/11 x64 电脑或 Windows 虚拟机：

1. 安装 Git、Python 3.12、Node.js 22、uv 和 [Inno Setup 6](https://jrsoftware.org/isdl.php)。
   确保 Runner 服务账号的 PATH 中可找到 `git`、`uv` 和 `npm.cmd`；缺少标准路径的编译器时，
   设置 CI 变量 `ISCC_PATH` 为 `ISCC.exe` 的绝对路径，环境范围为全环境。
2. 在 GitLab 项目的 Runners 页面创建项目 Runner，添加 `windows` 标签，关闭
   Run untagged jobs，并根据作业规则设为 Protected。下载官方 Windows GitLab Runner。
3. 在该 Windows 机器上注册 Runner，选择 `shell` executor 和 `powershell` shell；
   然后安装服务并启动（示例使用项目页面提供的认证 token，不写入仓库）：

```powershell
New-Item -ItemType Directory -Force C:\GitLab-Runner
# 从 GitLab 官方下载页保存 gitlab-runner.exe 到该目录后，以管理员 PowerShell 执行。
Set-Location C:\GitLab-Runner
.\gitlab-runner.exe register --url https://你的GitLab地址 --token 项目Runner认证token --executor shell --shell powershell
.\gitlab-runner.exe install
.\gitlab-runner.exe start
```

Shell Runner 会在宿主机执行构建脚本，应专用于可信项目。Windows 作业不需要交互桌面，
不连接 QQ、真实 Redis 或付费模型。验收使用临时目录和 SQLite；GUI 截图是本机可选验收。
Windows 检查失败不会上传可分发的 artifacts；该可选作业失败不影响 Web 部署状态。

没有 Windows Runner 时，也可在 Windows 本机手动构建：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/build_windows.ps1 -BuildInstaller
```

安装器默认安装到当前用户 `%LOCALAPPDATA%\Programs\AgentBot`，无需管理员权限。
提供开始菜单和可选桌面快捷方式，升级保留配置与数据，卸载也保留用户运行数据。
安装器不包含 WebView2 Runtime，目标电脑需提前安装；程序与安装器均未签名。

## 发布和回滚

### Windows 一键安装下载站

`https://public.loserbai.cn/agentbot/` 使用服务器 `192.168.1.5` 的
`/www/public/agentbot/` 静态目录，沿用现有下载站。该目录发布安装器、校验文件、
版本清单、安装脚本、版本化 SQL 包、升级说明及同版 wheel，不上传配置、数据库、日志或媒体。
公网命令见 [Windows 桌面版说明](../WINDOWS_README.md)。

每次 Windows 安装器构建都会在 `dist/windows/installer/` 生成发布所需文件。
版本采用 `主版本.次版本.修订号`，发布新程序时同步更新 `pyproject.toml` 和
`src/agent_bot/__init__.py` 中的版本再构建；安装器的 `AppVersion`、文件名和
`latest.json` 使用同一发布版本。当前已发布的 Windows 程序为 `0.5.4`。
同时提供 `AgentBot-0.5.4-sql.zip`、`UPGRADE-0.5.4.md` 和
`agent_bot-0.5.4-py3-none-any.whl`，供新库初始化与已有库的显式迁移使用。
安装脚本读取当前用户的安装登记，只有服务器版本更高时升级；相同版本跳过下载，
本机版本更高时保留现有安装。升级成功后由安装器更新登记版本，脚本不提前改写。

先上传版本化的安装器和校验文件，再上传脚本，最后替换 `latest.json`，
确保新清单引用的安装器已完整可用。示例在 Windows PowerShell 执行：

```powershell
ssh root@192.168.1.5 'mkdir -p /www/public/agentbot'
scp dist/windows/installer/*-Setup.exe dist/windows/installer/SHA256SUMS.txt root@192.168.1.5:/www/public/agentbot/
scp dist/windows/installer/install.ps1 dist/windows/installer/install.bat root@192.168.1.5:/www/public/agentbot/
scp dist/windows/installer/latest.json root@192.168.1.5:/www/public/agentbot/latest.json.upload
ssh root@192.168.1.5 'chmod 644 /www/public/agentbot/latest.json.upload && mv /www/public/agentbot/latest.json.upload /www/public/agentbot/latest.json'
```

保留旧版本安装器用于手动回退；切换到旧清单只影响首次安装或更旧的客户端，
已有更高版本不会自动降级。回退安装程序时保留客户端已有数据。
发布后通过公网检查两种脚本、`latest.json` 和安装器均可下载，核对安装器 SHA256。
下载脚本的离线测试：`uv run pytest tests/unit/test_windows_install.py --no-cov`。

### Web 服务

手动执行 `deploy-production`；发布文件保存在 `/opt/agentbot/releases/<流水线编号>/`。
先拉取镜像再更新固定 Compose 项目，等待 `/health/ready`。成功后更新 `current`；
拉取失败保留原服务，就绪失败尝试恢复上一成功发布并返回失败。
绑定地址和端口随发布清单保存，因此显式回滚也使用该版本的网络设置。

```bash
bash /opt/agentbot/current/deploy.sh /opt/agentbot 目标流水线编号
```

回滚只切换程序，保留数据卷，不回退数据库结构；跨数据库版本回滚前确认兼容性。
发布会有短暂服务切换。GitLab `resource_group` 与服务器文件锁避免并发部署交叉。

参考：[GitLab Windows Runner](https://docs.gitlab.com/runner/install/windows/)、
[Shell executor](https://docs.gitlab.com/runner/executors/shell/)、
[Runner 标签](https://docs.gitlab.com/ci/runners/configure_runners/)、
[Inno Setup 当前用户安装](https://jrsoftware.org/ishelp/topic_setup_privilegesrequired.htm)。
