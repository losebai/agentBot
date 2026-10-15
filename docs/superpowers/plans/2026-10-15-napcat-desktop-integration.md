# NapCat 桌面托管集成实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Windows 桌面版托管一个 NapCat 实例，自动建立 OneBot 反向连接，提供原生登录入口和网页只读状态，并保留外部 NapCat 接入方式。

**Architecture:** 桌面父进程拥有后端进程树；仅桌面专用后端注入 NapCat supervisor。supervisor 消费本次有效配置并负责所属配置、锁和进程资源，网页通过现有认证观察状态。NapCat 与 QQ 程序载荷仅进入 Windows 分发包，不进入 wheel。

**Tech Stack:** Python 3.12、Pydantic、FastAPI/NoneBot、pywebview、Windows Job Object/ctypes、React/TypeScript、Vitest、pytest、PowerShell、Inno Setup。

**Spec:** [已确认设计](../specs/2026-10-15-napcat-desktop-integration-design.md)。用户于 2026-10-15 回复“开始”，批准从书面设计进入计划阶段。

**Execution status:** 用户已审阅计划并选择当前会话实施。任务 1 已完成源码核实，资料隔离的载荷条件尚未满足；证据见[兼容性记录](../reviews/2026-10-15-napcat-payload-compatibility.md)。任务 2–7 尚未开始，正在确认是否允许 QQ 沿用自身原生资料目录。

## Global Constraints

- Windows x64；第一版只托管一个 QQ 账号，资料根目录固定 `data/napcat/`，程序载荷固定 `runtime/napcat/`。
- `AGENT_BOT_NAPCAT_MODE` 仅允许 `external`、`managed`，缺失为 `external`；`AGENT_BOT_NAPCAT_WEBUI_PORT` 默认 6099，范围 1–65535，WebUI 固定回环。
- 不增加网页进程控制能力或 Token 原生菜单；新增 NapCat 静态字段本次在网页只读。
- 使用有效 `ONEBOT_V11_ACCESS_TOKEN`，不生成替代 Token、不写运行数据库、不回填密钥；修改后手动重启生效。
- 所属反向连接名为 `agentbot-managed`，消息格式 `array`，路径 `/onebot/v11/ws`；有正向地址或 HTTP API 根地址时明确报配置冲突。
- 所有新 Python 模块、类、函数及修改函数补中文文档字符串；前端函数和状态分支补中文注释。
- 不调用真实 QQ、付费模型或生产服务；不改 SQL、发布版本，不上传或发布安装包，保留既有 0.3.0 wheel。
- 已有工作区修改和暂存内容属于其他工作，只提交本次明确列出的文件；实施前按 using-git-worktrees 技能检查隔离方式，不丢失未提交源码。

## Review Focus

1. QQ 原生资料目录独立于 NapCat 工作目录；只设置 `NAPCAT_WORKDIR` 不能作为 QQ 隔离的证据。任务 1 负责证明，失败则停止载荷适配并修订设计。
2. Windows 启动器可能先退出、后代继续存活；任务 4 用真实的假后代进程证明关窗与父进程异常退出都会清理所属进程树，外部进程不受影响。
3. 网页已保存新 Token/端口但尚未重启时，原生登录入口仍须打开当前实例；任务 5 使用父子私有通道中的非秘密活动快照，不重新读取新端口冒充当前端口。
4. 新静态字段会被现有管理模型自动枚举；任务 2 明确排除其 HTTP 写权限，并断言手工 PATCH 同样被拒绝。
5. 外部 WebUI 恰好占据预期端口、或上游自动换端口时，任务 4 必须确认监听者属于所属 Job，不能仅凭 HTTP 200 认定就绪。

## 规划核实与修正

当前实际接口前缀是 `/api/v1`。已将设计中省略版本前缀的表述校正为 `/api/v1/bots`、`/api/v1/napcat/status`，不增加第二套别名。

对上游固定版本 `v4.18.38` 的只读核实确认：

- `packages/napcat-common/src/path.ts` 支持 `NAPCAT_WORKDIR`，分离配置、日志和缓存。
- `packages/napcat-core/helper/config-base.ts` 从默认配置创建新账号配置，可用于首次扫码后的 OneBot 配置继承。
- `packages/napcat-shell/napcat.ts` 支持 `NAPCAT_DISABLE_MULTI_PROCESS=1` 和 `NAPCAT_DISABLE_PIPE=1`，可避免上游主进程自动拉起工作进程和连接公共控制管道。
- `packages/napcat-shell/base.ts` 的 QQ 资料路径来自 `NodeQQNTWrapperUtil.getNTUserDataInfoConfig()`，不是 `NAPCAT_WORKDIR`。还没有证据证明普通 Windows Shell 载荷能独立设置该资料路径。

因此任务 1 是载荷兼容性前置条件，不得编造 `QQ_DATA_DIR`、`--user-data-dir` 或覆盖系统 QQ 配置绕过它。后续离线契约可以用假载荷测试；正式托管适配和捆绑必须等待该条件满足。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `src/agent_bot/napcat/contracts.py` | 不含秘密的状态、固定错误码和内部启动契约 |
| `src/agent_bot/napcat/payload.py` | 固定版本载荷清单、文件校验与安全路径 |
| `src/agent_bot/napcat/configuration.py` | 所属 OneBot/WebUI 配置和活动连接地址 |
| `src/agent_bot/napcat/windows_process.py` | Job、目录锁、所属进程启动和监听者归属 |
| `src/agent_bot/napcat/supervisor.py` | 可取消生命周期、允许事件日志和状态 |
| `src/agent_bot/napcat/desktop_ui.py` | QQ 原生菜单、固定模式选择及登录窗口 |
| `src/agent_bot/admin/napcat_routes.py` | 只读、受现有认证保护的状态路由 |
| `src/agent_bot/desktop.py`、`application.py` | 桌面专属注入与父子资源清理 |
| `src/agent_bot/config.py`、`runtime_fields.py`、管理字段授权 | 静态配置归属、校验和网页只读边界 |
| `tools/prepare_napcat_payload.py`、Windows 构建与安装器 | 显式载荷输入、白名单、干净发布 |
| `web/src/components/NapCatStatus.tsx`、运行概览/API/类型 | 状态展示与独立错误提示 |

公共类型放在 `contracts.py`：`NapCatMode = Literal['external', 'managed']`；`NapCatState = Literal['not_started', 'starting', 'running', 'stopped', 'failed', 'plugin_disabled']`。冻结数据类 `NapCatStatus(mode, state, webui_ready=False, error_code=None)` 只使用这些类型；错误码为固定字符串枚举，不存底层异常文本。内部冻结 `NapCatLaunch(command: tuple[str, ...], cwd: Path, environment: dict[str, str], webui_port: int)` 不进入响应或日志。

### Task 1: 固定运行载荷与资料隔离的兼容性证据

**Files:** Create `docs/superpowers/reviews/2026-10-15-napcat-payload-compatibility.md`；仅在兼容性证据成立后创建 `tests/fixtures/napcat/payload-layout.json` 和 `tests/fixtures/napcat/default-config.json`。

**Interfaces:** 消费官方 `v4.18.38` 的启动器、默认配置与资料路径实现；产出经核实的启动清单规则，供任务 3 的 `load_payload` 和 `build_launch` 使用。夹具只含目录结构、程序占位文件名和无秘密配置，不含第三方程序二进制。

- [ ] **Step 1:** 阅读该 tag 的 Windows 启动入口、QQ 原生资料路径来源、WebUI 监听及日志配置；记录源文件链接、固定 commit 和支持的 QQ 版本。下载只用于检查的文件放隔离临时目录，不执行 QQ 或安装器。
- [ ] **Step 2:** 在兼容性记录中写出确切入口、参数、环境、QQ 资料目录机制和首登默认配置文件名。必须能说明 QQ 数据如何落到 `data/napcat/` 且不读写系统 QQ 资料或公共注册表/配置。
- [ ] **Step 3:** 若上游未提供满足条件的机制，将该记录标为“该载荷不符合已确认设计”，停止任务 3–7 的产品适配，向用户提交有证据的设计调整选项；不得把假启动器测试当成真实兼容性证明。
- [ ] **Step 4:** 证据成立时固定载荷清单字段：`schema_version=1`、`platform='windows-x64'`、`napcat_version`、`qq_version`、`entrypoint`、`qq_executable`、`files`（相对路径到 SHA256 的映射），并记录独立资料机制。只支持该已验证版本组合，不接受任意 shell 模板。
- [ ] **Step 5:** 核对夹具来源和无密钥内容；仅提交记录与无秘密夹具，不提交上游二进制。该任务可单独否决具体载荷，其他离线代码不得据此宣称可交付。

### Task 2: 静态参数、首次初始化与 HTTP 权限边界

**Files:** Modify `src/agent_bot/config.py`、`runtime_fields.py`、`admin/models.py`、`admin/settings.py`、`application.py`、`.env.example`；Test `tests/unit/test_configuration_layers.py`、`test_startup_settings.py`、`tests/admin/test_settings.py`。

**Interfaces:** 产出 RuntimeConfig 的 `agent_bot_napcat_mode: NapCatMode` 和 `agent_bot_napcat_webui_port: int`。在 `admin/models.py` 定义 `NATIVE_ONLY_STARTUP_SETTING_NAMES` 为这两个字段；`STARTUP_SETTING_NAMES` 仍包含全部静态字段供读取和候选校验，HTTP 注入的 `StartupSettingsStore.allowed_names` 使用其差集。原生模式保存使用只含 `agent_bot_napcat_mode` 的 store。

- [ ] **Step 1:** 写失败测试，断言缺省模式 external、端口 6099，非法模式/0/65536 拒绝；这两个字段不进入数据库；新增字段网页 `read_only=True`，手工 PATCH 拒绝且不改文件。
- [ ] **Step 2:** 定向运行新增测试确认失败；只允许模型不存在、字段缺失或权限断言失败，夹具本身失败须先修正。
- [ ] **Step 3:** 在 RuntimeConfig 增加上述字段与中文说明，加入 `RESTART_REQUIRED_FIELDS`，复用静态字段分类。首次初始化依赖任务 3 的校验函数，放到任务 7 集成，避免未实现模块形成任务依赖环。
- [ ] **Step 4:** 加入已有 `.env`/`.env.prod` 未编辑字段不被修改、环境覆盖不可原生保存、陈旧 revision 拒绝的断言；新代码读写均使用 StartupSettingsStore，不修改原有其他静态字段的授权。
- [ ] **Step 5:** 运行本任务相关定向测试（`--no-cov --allow-hosts=127.0.0.1,::1`）和所改文件 Ruff；通过后只提交本任务代码与测试。

### Task 3: 载荷校验与所属连接配置

**Files:** Create `src/agent_bot/napcat/__init__.py`、`contracts.py`、`payload.py`、`configuration.py`；Test `tests/unit/test_napcat_payload.py`、`test_napcat_configuration.py`。

**Interfaces:** `load_payload(root: Path) -> NapCatPayload` 校验任务 1 的固定清单；`configure_instance(directory: Path, payload: NapCatPayload, config: RuntimeConfig, backend_url: str) -> NapCatLaunch` 生成所属配置；`reverse_ws_url(backend_url: str) -> str` 生成协议地址。内部 `NapCatPayload` 为冻结数据类，持有 root、版本和校验后的入口路径；不接受 HTTP 参数构造。

- [ ] **Step 1:** 写失败测试：缺清单、文件摘要不符、绝对路径、`../`、目录链接越界、未列入清单的可执行文件、实际配置/日志混入载荷均报固定错误；测试夹具文件用临时字节生成。
- [ ] **Step 2:** 写配置行为测试：`http://127.0.0.1:8080/` 转成 `ws://127.0.0.1:8080/onebot/v11/ws`；IPv6 和通配监听使用对应回环；首登模板和所属账号配置包含唯一 `agentbot-managed`、`array` 和当前 Token；不修改外部配置，保留其他字段。
- [ ] **Step 3:** 定向运行上述测试确认失败，然后实现清单校验和原子配置写入。通过已验证的启动方式设置 `NAPCAT_WORKDIR`、禁用上游公共管道与主从自动拉起；QQ 独立资料方式严格使用任务 1 的证据，禁止猜测参数。
- [ ] **Step 4:** WebUI 固定回环；首次生成独立随机 WebUI 密钥，已有配置保留上游改密后的值；关闭上游文件日志及 debug，stdout/stderr 不落原文。正向连接冲突、无法解析配置或配置写入失败返回固定错误，不切后端、不覆盖旧文件。
- [ ] **Step 5:** 运行本任务测试和 Ruff；同时断言异常、对象 repr 和日志不含 OneBot/WebUI/会话密钥。通过后提交本任务文件。

### Task 4: Windows 所属进程树与 supervisor

**Files:** Create `src/agent_bot/napcat/windows_process.py`、`supervisor.py`；Test `tests/unit/test_napcat_windows_process.py`、`test_napcat_supervisor.py`。

**Interfaces:** `WindowsJob.spawn(launch: NapCatLaunch) -> OwnedProcess`、`WindowsJob.assign(pid: int) -> None`、`WindowsJob.owns_listener(host: str, port: int) -> bool`、`WindowsJob.close() -> None`；`OwnedProcess` 提供 `pid: int`、`poll() -> int | None`、`wait(timeout: float) -> int`、`close() -> None`，封装原生句柄。`InstanceLease(directory: Path)` 为非阻塞 OS 锁上下文。`NapCatSupervisor(directory: Path, config: RuntimeConfig)` 提供 `start(backend_url: str) -> None`、`stop() -> None`、`snapshot() -> NapCatStatus`；start 创建工作线程后立即返回，内部失败只发布状态。

- [ ] **Step 1:** 写真实假进程测试：启动器派生子进程后退出，Job 关闭使后代结束，另一个未加入 Job 的进程存活。假进程只等待事件/管道，不运行 QQ；平台限定 Windows。
- [ ] **Step 2:** 写 supervisor 测试：external/QQ 禁用无 spawn，托管缺载荷为 failed，目录重复锁为 failed，启动期间 stop 可取消，重复 stop 安全，退出不自动重试，WebUI 异常不改变后端就绪状态。
- [ ] **Step 3:** 先运行确认失败，然后通过 ctypes 创建配置 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Job。NapCat 以挂起状态创建，加入 Job 后再恢复线程，避免子进程在归属确定前派生后代；所有句柄在部分失败路径释放。
- [ ] **Step 4:** 使用 Windows 监听表及 `IsProcessInJob` 判断 WebUI 端口归属，HTTP 探测只在归属成立后进行。测试“预期端口为外部服务”“上游换到另一个端口”都不返回 webui_ready。抢占失败不更改端口。
- [ ] **Step 5:** stdout/stderr 送 DEVNULL；日志仅记录允许的固定生命周期事件与错误码。通过 supervisor 工作线程存活和句柄释放断言后，运行本任务测试及 Ruff，提交文件。

### Task 5: 桌面装配、原生入口与当前实例快照

**Files:** Modify `src/agent_bot/desktop.py`、`desktop_settings.py`、`application.py`；Create `src/agent_bot/napcat/desktop_ui.py`；Test `tests/unit/test_desktop.py`、`test_desktop_settings.py`、`test_napcat_desktop.py`。

**Interfaces:** `create_application(stored, admin, *, napcat: NapCatSupervisor | None = None) -> FastAPI` 只注入观察能力，不从普通应用生命周期自动启动；`serve(app, host, port, *, napcat: NapCatSupervisor | None = None) -> None` 的 DesktopServer 在监听成功后启动 supervisor，在 `shutdown()` 调用 `super().shutdown()` 前停止 supervisor。父进程保存无秘密 `NapCatDesktopState(mode, state, webui_ready, webui_port, error_code)`；mode/state 与公共状态同类型，webui_port 为 int，error_code 为固定错误码或 None。

- [ ] **Step 1:** 写失败测试：普通 CLI 不实例化 supervisor；桌面后端使用同一已加载 RuntimeConfig；后端监听失败无 NapCat；NapCat 失败仍打开管理页；禁用 QQ 不启动。
- [ ] **Step 2:** 为 `BackendProcess.__init__(command, directory, log_path, *, desktop_handshake: bool = False)` 增加握手选项，桌面生产入口显式启用。实际 `--desktop-child` 在装配前等待一个私有启动字节；父进程启动后先通过 `WindowsJob.assign(pid)` 加入所属 Job，再发送启动字节。泛用假后端测试保持默认不握手，避免启动字节被当作退出信号；父进程异常结束由 Job 清理全部所属后代。
- [ ] **Step 3:** 在已有私有 stdout 增加 `AGENT_BOT_NAPCAT_STATE ` 固定 JSON 通知，只含 NapCatDesktopState 的字段；先按严格模型校验再保存，未知或畸形消息不得修改窗口地址。密钥不进入通知或后端日志。
- [ ] **Step 4:** 原生 QQ 菜单固定提供“使用托管 NapCat”“使用外部 NapCat”“打开 NapCat 登录页”“查看托管状态”。模式保存后提示手动重启；登录窗口只使用当前活动状态的端口和固定 `/webui/` 路径，必要密钥从固定本地 WebUI 配置读取，以原生提示显示。没有 js_api、不把密钥放 URL、不接受网页消息控制。
- [ ] **Step 5:** 测试“保存新端口/Token 未重启仍打开旧实例”“原生端口验证失败保持进程”“关窗与重启竞争无遗留”“父进程异常退出清理”；相关测试通过后只提交本任务改动。

### Task 6: 认证只读接口与运行概览

**Files:** Create `src/agent_bot/admin/napcat_routes.py`、`web/src/components/NapCatStatus.tsx`；Modify `admin/routes.py`、`admin/models.py`、`web/src/api.ts`、`types.ts`、`pages/Overview.tsx`；Test `tests/admin/test_napcat_routes.py`、`web/src/components/NapCatStatus.test.tsx`、`pages/Overview.test.tsx`。

**Interfaces:** `install_napcat_routes(router: APIRouter, provider: Callable[[], NapCatStatus]) -> None` 注册 `GET /napcat/status`，挂到已认证 `/api/v1` router；API 模型 `NapCatStatusResponse` 与公共状态同字段。`ApiClient.napcatStatus(signal?: AbortSignal): Promise<NapCatStatus>`；组件 `NapCatStatus({ status, botCount, error }: { status?: NapCatStatus; botCount: number; error?: string })` 只负责展示。

- [ ] **Step 1:** 写失败测试：有密码时匿名 401；授权 GET 只返回 mode/state/webui_ready/error_code；无上下文为 external；POST/DELETE 等控制动作不存在；响应和 OpenAPI 不含 secret/command/path。
- [ ] **Step 2:** 实现固定响应和外部状态 provider，不让请求触发外部 NapCat 探测。已有免登录语义保持不变。
- [ ] **Step 3:** 前端测试精确断言 running + 0 bots 显示“等待登录或连接”，bots > 0 才显示 QQ 在线，plugin_disabled 与失败固定文案；状态接口失败时原用量和机器人连接仍可刷新。
- [ ] **Step 4:** 概览各请求分别处理错误；新增读取带 AbortSignal，卸载和后续刷新取消旧读取，禁止旧响应覆盖新状态。更新已有 URL 夹具，避免把新增状态请求误当用量响应。
- [ ] **Step 5:** 执行 `uv run pytest tests/admin/test_napcat_routes.py --no-cov --allow-hosts=127.0.0.1,::1`、相关前端测试及 typecheck；通过后提交本任务代码。静态资源留到任务 7 一次构建。

### Task 7: 可选载荷分发、说明与集中验证

**Files:** Create `tools/prepare_napcat_payload.py`；Modify `src/agent_bot/first_run.py`、`desktop.py`、`tools/build_windows.ps1`、`build_installer.ps1`、`windows-installer.iss`、`check_windows.py`、`check_package.py`、`pyproject.toml`（仅排除规则）、`.gitignore`、`README.md`、`WINDOWS_README.md`、`deploy/README.md`；Test `tests/unit/test_napcat_packaging.py`、`test_first_run.py`。

**Interfaces:** `prepare_payload(source: Path, destination: Path) -> None` 调用任务 3 校验后按清单复制；构建参数 `-NapCatPayloadDirectory` 仅接收本地已准备且已校验载荷，不自动下载。安装器仅在载荷参数存在时纳入 `runtime/napcat/`。

- [ ] **Step 1:** 写失败测试：无载荷可正常构建标准外部包；给定载荷完整校验后复制；摘要错误、未列出文件、实际 config/logs/QQ 资料、目录链接均拒绝且不清空旧目录；已有运行数据的构建目录继续拒绝覆盖。
- [ ] **Step 2:** 实现复制前完整校验和白名单；更新构建保护识别干净 runtime，删除或重建前验证绝对路径和链接属性。升级/卸载只处理明确程序资源，保留 `data/napcat/`。sdist/wheel 明确排除 runtime 载荷与验收数据。扩展 `prepare_first_run(directory: Path, *, enable_managed_napcat: bool = False) -> None`；仅桌面入口携带已通过 load_payload 验证的显式开关，新的启动文件才写 managed。已有配置/数据库保持现有保护，普通 CLI 不自动托管。
- [ ] **Step 3:** 文档说明两种模式、首次安装、原生登录、权限/端口问题、手动重启、保留资料、固定版本和许可来源。标准包不声称已经包含运行环境；实际运行载荷的授权未确认前不捆绑、不发布。
- [ ] **Step 4:** 合并代码后一次执行相关 Python 测试、Ruff、相关 Vitest、`npm --prefix web run typecheck` 和 `npm --prefix web run build`，记录每条命令结果；只有失败或后续改动影响时重跑。
- [ ] **Step 5:** 使用全新独立输出目录构建 Windows 包；tools/check_windows.py 的离线启动副本不复制真实 runtime，带载荷场景只使用假的已验证占位程序，真实载荷仅做文件完整性检查，避免验收意外启动 QQ。若构建 wheel，只使用新输出目录并按实际版本运行 check_package，保留旧 0.3.0 包。安装器仅在编译器可用且载荷满足任务 1 时检查，不上传。
- [ ] **Step 6:** 手工真实扫码/QQ 联调记录为未执行，等待隔离测试账号与明确验收环境；不得用离线假进程通过代替它。最后仅提交本次改动并报告通过项、未执行项及具体限制。

## 验证纪律与交接

本计划自身仅做文档、路径和契约检查，不跑产品测试。任务中的红/绿验证属于有行为风险的配置、资源和权限测试，不增加逐语句复述实现的测试。

用户已选择由当前会话逐项实施，以减少共享接口和 Windows 生命周期处理在多位实施者之间的交接。任务 1 的载荷条件未满足时须明确修订设计，不能用兼容分支静默绕过。

## 上游证据链接

- [v4.18.38 工作目录](https://github.com/NapNeko/NapCatQQ/blob/v4.18.38/packages/napcat-common/src/path.ts)
- [v4.18.38 首次账号配置继承](https://github.com/NapNeko/NapCatQQ/blob/v4.18.38/packages/napcat-core/helper/config-base.ts)
- [v4.18.38 QQ 资料路径来源](https://github.com/NapNeko/NapCatQQ/blob/v4.18.38/packages/napcat-shell/base.ts)
- [v4.18.38 主从进程及控制管道开关](https://github.com/NapNeko/NapCatQQ/blob/v4.18.38/packages/napcat-shell/napcat.ts)
