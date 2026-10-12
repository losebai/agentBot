# agentBot 独立项目与 Python 迁移设计

日期：2026-10-12

状态：用户于 2026-10-12 确认采用方案 A；第一阶段实现和离线验证完成。Java AI 的 Python 迁移、生产数据迁移与真实平台联调尚未执行。

## 1. 目标与范围

在 `D:/items/web/agentBot` 建立专门用于 Agent 机器人的独立项目。长期目标是以 Python 实现机器人接入、AI 编排、工具调用和管理服务，使用 React + TypeScript 提供图形界面。浏览器管理界面是本方案的默认选择，桌面安装软件另行设计。

迁移来源是同级目录下的 `dispatch-sdk/dispatch-chat`、`dispatch-sdk/dispatch-ai` 及 `bai-web` 的 AI 页面。保留源项目及其本地修改，迁移过程中不调整源项目的配置、不连接真实 QQ、不修改生产数据库、不启动会产生外部消息的机器人。

第一阶段交付一个可以独立安装、构建和离线验证的机器人服务与管理界面。此阶段 AI 请求仍由显式配置的 Java AI 服务处理；不能将兼容转发宣称为 AI 的 Python 实现。后续阶段逐项替换 Java AI 能力。

## 2. 已检查的现有实现

源后端当前提交为 `da11607`，工作区包含未提交配置变更和新增文档、SQL；源前端也有未提交源码和构建产物变更。迁移基于当前工作区的明确源码清单，记录来源，不重置源目录。

| 能力 | 现有位置 | 迁移策略 |
| --- | --- | --- |
| Python 机器人启动 | `dispatch-chat/src/dispatch_chat/__main__.py`、`bootstrap.py` | 第一阶段保留启动配置语义，由新入口调用 |
| QQ 消息与生命周期 | `dispatch_chat/plugin/`、`integrations/onebot/` | 第一阶段迁入，保留 NoneBot 和 OneBot v11 |
| Chat 业务与接口边界 | `dispatch_chat/services/chat/` | 第一阶段迁入，复用 `ChatClient` 协议 |
| 配置持久化与热刷新 | `settings.py`、`runtime_reload.py`、`plugin/lifecycle.py` | 第一阶段迁入，管理 API 使用现有校验与 revision 乐观锁 |
| 持久队列、记忆和工作流 | `integrations/redis_queue.py`、`memory_store.py`、`integrations/workflow_store.py` 等 | 第一阶段保留，避免消息处理行为退化 |
| AI 兼容客户端 | `integrations/dispatch_ai.py` | 第一阶段保留；后续由 Python AI 实现替换边界内调用 |
| Chat 的 MCP 服务 | `integrations/mcp.py`、`services/tool_sessions.py` | 第一阶段保留已有会话鉴权、工具授权和生命周期 |
| Java AI 编排 | `dispatch-ai/src/main/java/com/items/dispatch/ai/` | 第二、三阶段按协议和行为迁移 |
| AI 数据实体与 Mapper | `dispatch-framework` 内的 `AiModel`、`AiProvider`、`AiAgent` 等 | AI 数据迁移时处理，不作为 Chat 代码附属物盲目复制 |
| AI 图形界面 | `bai-web/src/pages/ai/`、`src/services/ai/` | 选择性复用；新界面仅包含 agentBot 的功能 |

现有搜索结果包含 82 个 Chat Python 源文件、77 个 Python 测试文件和 131 个 AI Java 源文件。这只是范围指标，不代表已完成全部行为审计。

Chat 启动依赖 MySQL 的有效配置快照和外部 Redis。数据库配置是唯一权威来源；引导连接参数来自环境。不能用内存配置或默认机器人身份掩盖初始化失败。

实施计划检查补充：`dispatch_chat/model_guard.py` 直接读取同一数据库的 `ai_model(model_id, type, enabled)`。独立部署必须显式准备此只读兼容目录，不复制供应商密钥；Java AI 仍最终校验模型。旧仓库专属 CI/Helm 测试不属于机器人运行行为，新项目不承诺沿用原仓库发布流程。

## 3. 方案选择

### A. 完整迁入 Chat，逐步替换 Java AI（采用）

先独立已有 Python 机器人和管理入口，再按现有协议迁移 AI。可以沿用消息队列、记忆、媒体、工作流和工具会话的既有行为及测试；代价是第一阶段仍依赖 Java AI。

### B. 第一阶段同时重写所有 AI、Chat 和界面

一步获得纯 Python 后端，但需要同时重建 Solon Agent 行为、模型供应商协议、MCP、数据库与任务语义，难以隔离回归。本次不采用。

### C. 只新建界面，长期连接原有服务

改动最少，但 Python 服务仍留在原仓库，不能实现用户要求的独立 agentBot 迁移目标。本次不采用。

## 4. 项目边界与拟定目录

```text
agentBot/
  pyproject.toml
  uv.lock
  src/
    agent_bot/              新项目入口、管理 API、应用装配
    dispatch_chat/          首阶段迁入的机器人实现，暂保留导入名
  tests/                   迁入的行为测试及新管理 API 测试
  sql/                     经过审查的表结构与迁移脚本
  web/                     React + TypeScript 管理界面
  docs/
    superpowers/specs/
    migration/             来源清单、协议矩阵、迁移与回退说明
  .env.example             仅包含无效占位符
  README.md
```

公开项目名为 agentBot，Python 发布包名为 `agent-bot`，新增入口模块为 `agent_bot`。第一阶段保留内部 `dispatch_chat` 命名，避免把包重命名、接口修改和业务迁移混为一次变更。项目不能通过 `sys.path`、符号链接或相对路径导入同级旧仓库。

界面访问管理 API，管理 API 调用业务服务；QQ 通过 NapCat 的反向 WebSocket 连接机器人；Chat 通过 `ChatClient` 边界请求 AI。第一阶段管理路由挂载到现有 NoneBot FastAPI 应用，统一 HTTP 入口，并保留现有 MCP 挂载和启动、关闭逻辑。

机器人单实例运行，避免多个 Web worker 重复消费、调度或投递。图形界面关闭不结束机器人进程。

## 5. 第一阶段：独立 Chat 与图形管理入口

### 5.1 源码与测试迁入

迁入 Chat 的 `src`、`tests`、运行时资源和经过审查的 SQL，保留现有行为。迁入时记录源文件、内容摘要和迁入目的；检查文件变化，不批量复制工作区。

不复制 `.env`、虚拟环境、IDE 目录、缓存、构建产物、运行时数据、覆盖率文件和生产部署凭据。`.env.example` 必须经过检查，仅保留安全占位值。依赖锁文件根据新项目元数据生成或更新，不能将旧锁文件标记为已适用于新包。

源码中的现有 SQL 表名、Redis key、配置字段及密码学会话标识保持兼容。新部署使用独立数据库和 Redis key 前缀，通过显式配置与迁移操作导入数据；不自动修改源服务的数据。

### 5.2 服务与管理 API

新增 `agent-bot` 命令与 `python -m agent_bot` 入口，保留离线配置初始化和工作流管理能力。服务启动前必须完成数据库与 Redis 配置；初始化失败时明确退出。

管理 API 第一批包括：

- 服务存活、就绪状态与当前配置版本。
- 已连接机器人列表及运行状态。
- 配置字段描述、可编辑值、数据库 revision、运行时已应用 revision。
- 部分配置更新、校验结果和需要重启的字段。
- 通过现有 Java AI 兼容客户端进行文本对话测试。

管理 API 使用独立的管理员凭据，与 OneBot Token、模型 API Key、MCP 工具会话 Token 分离。凭据仅在运行环境配置。存活接口不暴露配置，其余管理接口均鉴权；浏览器凭据仅保存于内存，不写入 URL 或 localStorage。

配置读取不返回秘密原文，使用单独字段表示是否已设置。更新时只覆盖请求明确提供的字段，未提供的秘密保持原值，不能将遮罩文本写回数据库。提交包含 `expectedRevision`，沿用 `SettingsStore.save` 的并发校验；冲突返回 HTTP 409。

保存数据库与应用配置是两个可观测状态。界面必须显示保存版本、应用版本及需要重启的字段，不将“已保存”当作“已生效”。热刷新沿用运行时租约机制，不能让同一消息混用不同版本资源。

### 5.3 图形界面

建立独立的 React + TypeScript 界面，复用合适的现有 AI 服务类型与页面组件，去除博客、资源站、Book 等非机器人功能，以及旧项目的线上域名和开发代理。

第一批界面包含运行概览、机器人状态、Chat 配置和文本对话测试。模型、Agent、Skill、MCP 的完整管理随相应 Python 服务迁移加入；未实现能力不显示可操作的虚假入口。

界面通过同源 API 访问服务。生产构建由服务提供静态文件；开发模式明确配置本地代理。密钥与数据库连接始终由后台处理。

## 6. Java AI 兼容契约与后续迁移

第一阶段使用显式配置的 Java AI 地址与鉴权凭据，不隐式寻找原项目，也不在 Python Provider 失败时自动切换 Java。

| 现有 Chat 使用的接口 | 用途 | Python 迁移阶段 |
| --- | --- | --- |
| `POST /dispatch-ai/chat/chatOne` | 普通同步对话 | 第二阶段 |
| `POST /dispatch-ai/chat/chatAgentOne` | 带 Chat 授权工具的 Agent | 第三阶段 |
| `POST /dispatch-ai/chat/agentChat` | 流式 Agent 事件 | 第三阶段 |
| `POST /dispatch-ai/chat/mediaOne` | 图片或音频生成 | 第四阶段 |
| `POST /dispatch-ai/chat/autoSpeakDecision` | Laya 自由发言决策 | 第四阶段 |
| `GET /dispatch-ai/job/results` | 查询异步任务投递结果 | 第四阶段 |
| `POST /dispatch-ai/job/{jobId}/ack` | 确认任务结果 | 第四阶段 |

上述为客户端已检查的接口，不承诺覆盖旧 AI 全部 API。实现前逐项记录请求、响应、错误和超时协议，包括 `requestsId`、`sessionId`、多模态 `input` 与 `messages` 的优先级、工具会话令牌和 SSE 分块语义。

第二阶段建立 Python 模型供应商适配、模型与供应商配置、普通同步与流式对话、会话记录；保留现有模型选择和错误分类。

第三阶段迁移 Agent 与 Skill、MCP 客户端、工具授权和循环执行。工具名称、参数校验、执行上限、请求总预算及取消语义必须通过契约测试。不能把简单的一次模型调用当作完整 Agent。

第四阶段迁移媒体、后台任务与 Laya 等外部能力。`soloncode` 和动态 Java/Lua 执行存在独立运行环境依赖，单独评估 Python 替代或保留外部执行服务；不能承诺全部能力仅凭 Python 进程运行。

每项能力迁移后以显式配置选择实现，校验通过再切换。旧服务继续保留以支持回退，数据库升级必须提供兼容性与回退说明。

## 7. 错误处理与验证

依赖不可用与未初始化必须可区分。API 错误返回类型化结构、稳定错误分类和请求标识；详细异常仅进入脱敏日志。对有副作用的聊天工具、任务和消息投递不自动重试。

验证分为：

1. 迁入现有离线测试，确认数据库 Fake、Redis Fake、HTTP Mock 和 NoneBot 集成测试仍覆盖原行为。
2. 新管理 API 测试覆盖鉴权、秘密脱敏、部分更新、revision 冲突、热刷新及重启提示。
3. 对话接口测试使用 HTTP Mock，不访问真实模型、QQ 或外部 MCP。
4. 前端完成类型检查与生产构建，并验证状态与配置更新的真实 API 交互。
5. Python 运行 Ruff 和完整离线测试，沿用现有 80% 分支覆盖率门槛；不通过降低门槛解决迁移失败。
6. 构建包并检查运行时资源，确认新项目无需访问旧目录即可导入、安装和运行。

真实 QQ、模型、数据库迁移与部署测试需要具备运行配置后单独执行。本次设计阶段不宣称已经测试运行。

## 8. 第一阶段验收条件

- agentBot 有自己的依赖声明、可安装入口、启动说明与迁移来源清单。
- 已有 Chat 的核心行为测试在新项目通过，新代码不引用旧源码目录。
- 管理界面能够鉴权查看状态，修改配置并显示保存与应用结果。
- 能够通过明确配置的 Java AI 兼容接口完成文本对话；文档准确声明该依赖。
- Python 检查、离线测试、前端检查与构建、包资源检查均有实际结果。
- 源项目、本地秘密和真实机器人状态保持完整；用户本地变更未被覆盖。

## 9. 审阅与实现边界

本文件是架构设计，不是迁移完成报告。用户审阅通过后，再形成第一阶段的具体实施计划，列明改动清单、验证命令和执行顺序，然后进入代码迁移。默认在当前聊天执行，不另建聊天、不委派子代理、不提交或推送代码。
