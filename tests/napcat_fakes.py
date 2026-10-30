"""生成不含 QQ 可执行程序和凭据的载荷夹具。"""

import hashlib
import json

from agent_bot.napcat.payload import VERSION


def make_payload(root):
    """只生成当前支持版本 Shell 的占位字节，不捆绑或执行本机 QQ。"""
    root.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in ("shell/NapCatWinBootMain.exe", "shell/NapCatWinBootHook.dll",
                 "shell/napcat.mjs", "shell/loadNapCat.js", "shell/qqnt.json"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"offline-placeholder-not-an-executable")
        files[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {"schema_version": 2, "platform": "windows-x64",
                "napcat_version": VERSION, "qq_source": "installed",
                "entrypoint": "shell/NapCatWinBootMain.exe",
                "files": files}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root
