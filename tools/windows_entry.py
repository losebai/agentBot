"""两个 Windows EXE 共用入口，GUI 与命令行共享发布资源和外部工作目录。"""

import multiprocessing
import sys
from pathlib import Path


def main() -> int:
    """先处理冻结进程引导，离线命令不导入或初始化 WebView。"""
    multiprocessing.freeze_support()
    from agent_bot.desktop import prepare_directory

    prepare_directory()
    if sys.argv[1:] == ["--desktop-child"]:
        from agent_bot.desktop import run_backend

        return run_backend()
    if Path(sys.executable).stem == "AgentBot" and not sys.argv[1:]:
        # 无控制台入口的错误用原生消息框报告，日志保存在可写目录。
        import ctypes

        try:
            from agent_bot.desktop import main as desktop_main

            return desktop_main()
        except Exception as exc:
            ctypes.windll.user32.MessageBoxW(
                None,
                f"桌面启动失败 ({type(exc).__name__})。\n"
                "请确认程序目录可写，并已安装 Microsoft Edge WebView2 Runtime。\n"
                "后端详情见 logs/desktop-backend.log；也可用 agent-bot.exe 排查。",
                "AgentBot", 0x10,
            )
            return 1
    from agent_bot.__main__ import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
