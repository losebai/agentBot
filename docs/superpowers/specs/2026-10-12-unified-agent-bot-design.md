# 统一 agent_bot 包

## 目标与范围

将 src/dispatch_chat 的机器人配置、业务服务、集成、插件及资源直接迁入
src/agent_bot，使运行代码和 wheel 仅包含 agent_bot 一个顶层包。保持管理界面、
机器人行为、离线设置和工作流命令的现有功能。

## 布局与兼容

- 保留 agent_bot/__init__.py 的版本及 agent_bot/__main__.py 的管理服务入口。
- 原 dispatch_chat/__main__.py 改为 agent_bot/bot_cli.py，dispatch-chat 命令继续
  指向它，保持机器人独立启动行为；推荐使用 agent-bot 启动完整服务。
- 其余模块和 resources、services、integrations、plugin 直接迁入 agent_bot。
- 更新静态导入、动态导入、NoneBot 插件路径、测试补丁路径和资源加载路径。
- 原 Python 导入路径 dispatch_chat 不再提供；数据库表、配置字段、环境变量、
  Redis 键和外部协议标识保持兼容。历史 SQL、迁移清单和设计文档保留原始记录。
- 打包清单、覆盖率来源和 Ruff 忽略路径统一为 agent_bot。

## 验证

先运行离线测试基线，再以新命名空间的独立进程导入和工作流资源加载作为迁移验证。
运行完整离线测试、Ruff、前端构建、wheel 构建、包资源检查和隔离安装检查。
仓库没有 Git 元数据，因此在当前目录实施并保留迁移前文件备份。
