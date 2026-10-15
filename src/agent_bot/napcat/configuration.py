"""仅修改所属 NapCat 配置；QQ 原生资料位置保持不变。"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from agent_bot.napcat.contracts import NapCatLaunch
from agent_bot.napcat.payload import NapCatPayload, is_link

if TYPE_CHECKING:
    from agent_bot.config import RuntimeConfig


class ConfigurationError(ValueError):
    """配置无法解析或持久化，公开错误不包含密钥和文件原文。"""


def atomic_write(path: Path, content: bytes) -> None:
    """同目录替换避免上游读到半份文件，失败时释放暂存资源。"""
    descriptor, name = tempfile.mkstemp(prefix=".agentbot-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def reverse_ws_url(backend_url: str) -> str:
    """只接受本后端就绪地址，兼容通配监听和 IPv6。"""
    parsed = urlsplit(backend_url)
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(parsed.hostname, parsed.hostname)
    if parsed.scheme != "http" or not host or parsed.port is None or parsed.username:
        raise ConfigurationError("后端连接地址无效。")
    address = f"[{host}]" if ":" in host else host
    return f"ws://{address}:{parsed.port}/onebot/v11/ws"


def read_object(path: Path) -> dict:
    """缺失文件表示首次创建，已有损坏配置则明确失败。"""
    value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(value, dict):
        raise ConfigurationError("NapCat 配置必须是对象。")
    return value


def configure_instance(
    directory: Path, payload: NapCatPayload, config: RuntimeConfig, backend_url: str,
    *, qq_executable: Path,
) -> NapCatLaunch:
    """使用已定位的本机 QQ 与活动配置；写入失败回滚，不修改 QQ 程序目录。"""
    if config.onebot_v11_ws_urls or config.onebot_v11_api_roots:
        raise ConfigurationError("托管模式不能同时配置正向 OneBot 连接。")
    folder = directory / "data/napcat/config"
    changed: list[Path] = []
    originals: dict[Path, bytes | None] = {}
    try:
        for parent in (directory / "data", folder.parent, folder):
            if is_link(parent):
                raise ConfigurationError("托管配置目录不能是链接。")
        folder.mkdir(parents=True, exist_ok=True)
        updates = {}
        for path in [folder / "onebot11.json", *sorted(folder.glob("onebot11_*.json"))]:
            data = read_object(path)
            network = data.setdefault("network", {})
            for kind in ("httpServers", "httpClients", "websocketServers", "websocketClients"):
                entries = network.setdefault(kind, [])
                if (not isinstance(entries, list)
                        or any(not isinstance(item, dict) for item in entries)):
                    raise ConfigurationError("NapCat 连接配置无效。")
                if any(item.get("enable") and item.get("name") != "agentbot-managed"
                       for item in entries):
                    raise ConfigurationError("托管目录含有其他已启用连接，请先停用。")
            clients = [item for item in network["websocketClients"]
                       if item.get("name") != "agentbot-managed"]
            clients.append({"name": "agentbot-managed", "enable": True,
                            "url": reverse_ws_url(backend_url), "messagePostFormat": "array",
                            "token": config.onebot_v11_access_token.get_secret_value(),
                            "reportSelfMessage": False, "debug": False,
                            "heartInterval": 30000, "reconnectInterval": 5000})
            network["websocketClients"] = clients
            updates[path] = data
        webui = read_object(folder / "webui.json")
        webui.update(host="127.0.0.1", port=config.agent_bot_napcat_webui_port,
                     disableWebUI=False, enableXForwardedFor=False)
        webui.setdefault("token", secrets.token_urlsafe(32))
        updates[folder / "webui.json"] = webui
        for path in [folder / "napcat.json", *sorted(folder.glob("napcat_*.json"))]:
            logging = read_object(path)
            logging.update(fileLog=False, consoleLog=False, fileLogLevel="error",
                           consoleLogLevel="error")
            updates[path] = logging
        for path, data in updates.items():
            if is_link(path):
                raise ConfigurationError("托管配置文件不能是链接。")
            originals[path] = path.read_bytes() if path.exists() else None
            atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
            changed.append(path)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        for path in reversed(changed):
            original = originals[path]
            if original is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, original)
        raise ConfigurationError("NapCat 配置无法保存，请检查格式、连接冲突和写入权限。") from exc
    shell = payload.root / "shell"
    # 仅把程序需要的系统环境交给第三方进程，不传数据库、模型或会话秘密。
    environment = {name: value for name, value in os.environ.items()
                   if name.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
                                       "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
                                       "PROGRAMFILES", "PROGRAMFILES(X86)", "COMSPEC"}}
    environment.update({"NAPCAT_WORKDIR": str(folder.parent.resolve()),
                        "NAPCAT_DISABLE_MULTI_PROCESS": "1", "NAPCAT_DISABLE_PIPE": "1",
                        "NAPCAT_PATCH_PACKAGE": str(shell / "qqnt.json"),
                        "NAPCAT_LOAD_PATH": str(shell / "loadNapCat.js"),
                        "NAPCAT_MAIN_PATH": str(shell / "napcat.mjs"),
                        "NAPCAT_INJECT_PATH": str(shell / "NapCatWinBootHook.dll")})
    return NapCatLaunch((str(shell / "NapCatWinBootMain.exe"), str(qq_executable),
                         str(shell / "NapCatWinBootHook.dll")), shell, environment,
                        config.agent_bot_napcat_webui_port)
