"""从已安装的发布包收集代码、前端、SQL 和动态导入，生成共享依赖的双入口。"""

from importlib.metadata import distribution
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


root = Path(SPECPATH).parent
package = Path(distribution("taobot").locate_file("agent_bot"))
# 遍历文件而非导入插件，避免在构建进程中初始化 NoneBot 和连接配置数据库。
hidden = []
for source in package.rglob("*.py"):
    parts = list(source.relative_to(package.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    hidden.append(".".join(parts))
for name in ("nonebot.drivers", "langchain_core", "langgraph", "uvicorn"):
    hidden.extend(collect_submodules(name))

data = collect_data_files("agent_bot")
data += copy_metadata("taobot", recursive=True)
data += copy_metadata("pywebview", recursive=True)

analysis = Analysis(
    [str(root / "tools" / "windows_entry.py")],
    datas=data,
    hiddenimports=hidden,
    excludes=["pytest", "tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6"],
    # 仅插件需要 SourceFileLoader；其他业务模块使用预编译字节码，减少启动编译和磁盘读取。
    module_collection_mode={"agent_bot.plugin": "py"},
)
pyz = PYZ(analysis.pure)
# 狐耳头像使用多尺寸 ICO，覆盖桌面快捷方式、窗口和任务栏的显示尺寸。
desktop_icon = str(root / "assets" / "desktop" / "agentbot-icon-v2.ico")
gui = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True, name="taobot",
    console=False, debug=False, upx=False, icon=desktop_icon,
)
# Windows 文件名不区分大小写，后台入口使用独立后缀以免覆盖桌面程序。
cli = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True, name="taobot-cli",
    console=True, debug=False, upx=False, icon=desktop_icon,
)
COLLECT(gui, cli, analysis.binaries, analysis.datas, name="taobot", upx=False)
