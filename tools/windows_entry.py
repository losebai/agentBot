"""两个 Windows EXE 共用入口，GUI 与命令行共享发布资源和外部工作目录。"""

import multiprocessing
import sys
import traceback
from pathlib import Path


def main() -> int:
    """先处理冻结进程引导，离线命令不导入或初始化 WebView。"""
    multiprocessing.freeze_support()
    from agent_bot.desktop import prepare_directory

    prepare_directory()
    if sys.argv[1:] == ["--desktop-child"]:
        from agent_bot.desktop import run_backend

        return run_backend()
    if Path(sys.executable).stem == "taobot" and not sys.argv[1:]:
        # 无控制台入口的错误用原生消息框报告，日志保存在可写目录。
        import ctypes

        try:
            from agent_bot.desktop import main as desktop_main

            return desktop_main()
        except Exception as exc:
            # 无控制台启动也保留故障位置；不记录异常正文、局部变量或配置秘密。
            try:
                log = Path("logs/desktop-launcher.log")
                log.parent.mkdir(parents=True, exist_ok=True)
                frames = traceback.extract_tb(exc.__traceback__)
                log.write_text(type(exc).__name__ + "\n" + "\n".join(
                    f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                    for frame in frames
                ), encoding="utf-8")
            except OSError:
                # 目录不可写时仍须弹出原生错误，不能让诊断写入遮蔽启动失败。
                pass
            ctypes.windll.user32.MessageBoxW(
                None,
                f"桌面启动失败 ({type(exc).__name__})。\n"
                "请确认程序目录可写，并已安装 Microsoft Edge WebView2 Runtime。\n"
                "启动详情见 logs/desktop-launcher.log；后端详情见 logs/desktop-backend.log。",
                "taobot", 0x10,
            )
            return 1
    from agent_bot.__main__ import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
