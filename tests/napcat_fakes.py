"""生成不执行 Node、QQ 或联网的托管载荷夹具。"""

import hashlib
import json


def make_payload(root):
    """生成 Node 运行包的占位资源，不依赖本机 QQ 安装。"""
    root.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in (
        "node/node.exe", "node/index.js", "node/agentbot-entry.cjs",
        "node/wrapper.node", "node/QQNT.dll",
        "node/package.json", "node/config.json", "node/napcat/napcat.mjs",
        "node/avif_convert.dll", "node/broadcast_ipc.dll", "node/crypto.dll", "node/ssl.dll",
        "node/libglib-2.0-0.dll", "node/libgobject-2.0-0.dll", "node/libvips-42.dll",
        "node/ncnn.dll", "node/opencv.dll", "node/QBar.dll", "node/LightQuic.dll",
        "node/win64/SSOShareInfoHelper64.dll", "node/win64/parent-ipc-core-x64.dll",
    ):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'{}' if name in {"node/package.json", "node/config.json"}
                           else b"offline-placeholder-not-an-executable")
        files[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {"schema_version": 3, "platform": "windows-x64", "runtime": "node",
                "napcat_version": "v4.18.38", "node_version": "v22.11.0",
                "qq_source": "bundled-components", "entrypoint": "node/agentbot-entry.cjs",
                "files": files}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root
