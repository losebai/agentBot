# NapCat Windows 托管载荷兼容性核实

日期：2026-10-15。对应计划：[NapCat 桌面托管集成](../plans/2026-10-15-napcat-desktop-integration.md)。

## 结论

固定检查版本为 NapCat `v4.18.38`，GitHub commit 为 `bbd0d2aca3d470c20b33b1d4f95123335633bf26`。已确认工作目录分离、首登默认配置继承以及禁用公共控制管道和主从重启的机制。

尚未找到该 Windows Shell 版本支持“QQ 登录资料全部独立保存在项目 `data/napcat/` 下，且不读写系统 QQ 资料或公共配置”的可验证启动机制。因此该版本目前不能按原设计声明为符合要求的运行载荷。这个结论是交付证据不足，不是证明所有 Windows 载荷均无法实现资料隔离。

按计划任务 1 的停止条件，先确认资料目录要求是否调整，再做后续产品适配。本次没有运行 QQ、安装器或上游程序，没有读取本机 QQ 配置和登录资料。

## 可验证的源代码事实

| 检查项 | 证据与含义 |
| --- | --- |
| NapCat 可写目录 | `packages/napcat-common/src/path.ts:18` 检查 `NAPCAT_WORKDIR`；config、logs、plugins、cache 等由此派生。可以分离 NapCat 自身配置与程序资源。 |
| QQ 资料路径 | `packages/napcat-shell/base.ts:57` 的 `getDataPaths()` 在 Windows 调用 `NodeQQNTWrapperUtil.getNTUserDataInfoConfig()`，随后派生 `nt_qq/global`；没有在此使用 `NAPCAT_WORKDIR`。 |
| 登录与会话路径 | 同文件的 engine/login 初始化使用前项派生路径；修改 Python 子进程工作目录不能证明改变原生 QQ 资料路径。 |
| 原生修改目录 API | `packages/napcat-core/wrapper.ts:152` 声明 `setUserDataSaveDirectory`，但声明本身不证明其修改仅限本进程。不能据此调用它来改变用户的系统 QQ 资料设置。 |
| 首次账号配置 | `packages/napcat-core/helper/config-base.ts:33` 起先定位账号配置，缺失时从默认配置加载再保存。OneBot 的 loader 使用 `onebot11` 前缀，可准备默认模板。 |
| WebUI 配置 | `packages/napcat-webui-backend/src/helper/config.ts:91` 起使用工作目录 config 下的 `webui.json`，与 QQ 原生资料路径属于不同机制。 |
| 主从进程 | `packages/napcat-shell/napcat.ts` 支持 `NAPCAT_DISABLE_MULTI_PROCESS=1` / `NAPCAT_DISABLE_MULTIPROCESSING=1`；独立模式可避免 master 自动拉起 worker。 |
| 公共控制管道 | 同文件及 `base.ts` 支持 `NAPCAT_DISABLE_PIPE=1`，可避免托管实例参与上游公共控制管道。 |
| Windows 原生启动 | `packages/napcat-shell-loader/launcher-user.bat` 设置固定载荷路径，再调用 `NapCatWinBootMain.exe`，参数包含 QQ 程序和 hook DLL；默认脚本还读取系统 QQ 安装登记，不宜直接当作项目自带独立运行环境。 |

## 发布资产校验记录

GitHub 在该 tag 返回以下文件摘要。仅查询资产元数据，没有下载或执行二进制，也没有把摘要匹配等同于可运行或许可已获授权。

| 发布资产 | SHA256 |
| --- | --- |
| `NapCat.Shell.zip` | `735f2e633247df3ffd6e8ee8b84d7039c315b2655f5807e5f0029f93d2c30016` |
| `NapCat.Shell.Windows.Node.zip` | `b88007a373a30deefb387e7cf17911752ff900bce22c534b3da6b644f5c6d0ae` |
| `NapCat.Shell.Windows.OneKey.zip` | `46c3ec155aa61f12e89bbf97013a21243d00b0019b5565992a88852d7627f39c` |

未选定满足资料隔离要求的 QQ 运行组合，因而没有生成“已验证载荷”夹具、QQ 版本断言或可用于发布的运行清单。官方一键包是安装准备入口，不能作为已完成 QQ 载荷兼容性验收的证据。

## 具体调整选项

推荐允许 QQ 沿用自身原生资料目录：NapCat 工作目录仍隔离到 `data/napcat/`，程序载荷仍独立；AgentBot 不修改 QQ 原生资料位置、不迁移已有资料，也不删除外部 QQ 数据。托管生命周期和 OneBot 配置继续由 AgentBot 负责。使用专用机器人 QQ 账号，首次登录由用户在 WebUI 明确完成；是否支持指定账号和既有登录恢复仍需按所选载荷验证。

另一选项是保留完全隔离要求，继续寻找或准备有明确独立资料机制的运行载荷。不得通过猜测参数、全局改目录、覆盖系统 QQ 配置或未经验证的启动脚本满足表面测试。

## 固定源码链接

- [工作目录](https://github.com/NapNeko/NapCatQQ/blob/bbd0d2aca3d470c20b33b1d4f95123335633bf26/packages/napcat-common/src/path.ts#L18)
- [QQ 路径来源与启动](https://github.com/NapNeko/NapCatQQ/blob/bbd0d2aca3d470c20b33b1d4f95123335633bf26/packages/napcat-shell/base.ts#L57)
- [原生路径接口](https://github.com/NapNeko/NapCatQQ/blob/bbd0d2aca3d470c20b33b1d4f95123335633bf26/packages/napcat-core/wrapper.ts#L152)
- [默认账号配置继承](https://github.com/NapNeko/NapCatQQ/blob/bbd0d2aca3d470c20b33b1d4f95123335633bf26/packages/napcat-core/helper/config-base.ts#L33)
- [WebUI 配置](https://github.com/NapNeko/NapCatQQ/blob/bbd0d2aca3d470c20b33b1d4f95123335633bf26/packages/napcat-webui-backend/src/helper/config.ts#L91)
- [主从与管道开关](https://github.com/NapNeko/NapCatQQ/blob/bbd0d2aca3d470c20b33b1d4f95123335633bf26/packages/napcat-shell/napcat.ts)
- [Windows 启动器](https://github.com/NapNeko/NapCatQQ/blob/bbd0d2aca3d470c20b33b1d4f95123335633bf26/packages/napcat-shell-loader/launcher-user.bat)
- [固定版本发布资产](https://github.com/NapNeko/NapCatQQ/releases/tag/v4.18.38)
