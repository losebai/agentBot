# 统一 agent_bot 包实施计划

> 使用 superpowers:executing-plans 在当前会话实施，并以独立代码审查验证迁移。

**Goal:** 将机器人和管理服务合并为 agent_bot 一个顶层 Python 包。

**Architecture:** 直接合并不冲突的模块，原机器人入口改名为 bot_cli。
保留 agent_bot 主入口及版本，更新所有可执行引用和包资源定位。

**Tech Stack:** Python 3.12、uv、NoneBot、pytest、Hatchling。

**Spec:** docs/superpowers/specs/2026-10-12-unified-agent-bot-design.md

## 全局约束

保持数据库、环境变量和外部协议兼容；不修改历史 SQL 和原始迁移记录。
不访问 QQ、生产数据库或付费模型。代码注释使用中文。

## 实施步骤

- [x] 保存迁移前文件备份，记录离线测试基线。
- [x] 增加新命名空间的独立进程导入与内置工作流资源测试，确认迁移前失败。
- [x] 迁移文件并更新 Python 导入、动态加载、测试和资源引用。
- [x] 更新 pyproject.toml、包检查工具、README.md 和 AGENTS.md。
- [x] 执行 Ruff、完整离线测试、前端构建、wheel 构建和资源检查。
- [x] 隔离安装 wheel，验证服务和兼容入口的离线命令，再进行独立代码审查。

## 审查重点

插件路径是否统一；工作流资源是否随 wheel 提供；导入是否产生启动副作用；
旧命令是否保持功能；配置和数据库标识是否被误替换；测试扫描是否误变为空目录。

## 执行记录

基线：1748 passed、1 failed、1 deselected，覆盖率 86.57%。已存在的失败为
test_runtime_config_storage_contract_covers_all_plugin_and_nonebot_fields。

当前工作区版本为 0.4.0，沿用已有媒体模块和发布资源，不回退到早期的 0.3.0。
字段完整性测试在已有媒体收尾更新中已修正为 140 个字段。

最终验证：

- 针对导入、入口、边界和工作流的测试：71 passed。
- `uv run ruff check .`：通过。
- 仓库规定的完整离线 pytest 命令：1790 passed、1 deselected，覆盖率 86.78%。
  首次执行出现临时 wheel 夹具的重复入口警告；修正为 bot_cli.py 后该测试独立重跑通过。
- `npm --prefix web run build`：通过；保留现有大文件分块提示。
- `uv build` 和 0.4.0 wheel 资源检查：通过。
- 临时目录安装 wheel：新包导入、内置工作流及两个入口的离线帮助通过；
  旧 dispatch_chat 命名空间不可导入，夹带旧包的 wheel 被检查工具拒绝。
- 独立只读审查：文件迁移完整，JSON 字节一致，配置与业务行为兼容，
  边界测试扫描真实目录，未发现需要修复的问题。

原始迁移工具及其测试继续验证历史导入操作；不会重写旧项目来源路径。
没有 Git 元数据，修改保留在当前工作区，不创建提交或执行发布。
