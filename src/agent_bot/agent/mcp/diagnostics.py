"""将不可信 MCP 报错映射为有限诊断，不保存 URL、路径、命令或凭据原文。"""

import json
import re

from mcp import types

_HTTP_STATUS = re.compile(
    r"\b(?:HTTP(?:/\d(?:\.\d)?)?\s+(?:error\s*[:=]?\s*)?|"
    r"status(?:[\s_]+code)?\s*[:=]?\s*|response\s+code\s*[:=]?\s*)([45]\d{2})\b"
    r"|\b([45]\d{2})\s+(?:Client|Server)\s+Error\b", re.IGNORECASE,
)
_CATEGORIES = (
    (r"\b(?:TimeoutError|ReadTimeout|ConnectTimeout|timed?\s*out|timeout)\b", "timeout"),
    (r"\b(?:ConnectionError|ConnectError|ConnectionRefusedError|NameResolutionError|"
     r"connection\s+(?:refused|reset)|failed\s+to\s+connect)\b", "connection_error"),
    (r"\b(?:ValidationError|invalid\s+arguments?|validation\s+errors?)\b", "invalid_arguments"),
)
_MESSAGES = {
    "http_error": "远端工具报告目标 HTTP 请求失败；检查目标访问限制和状态码。",
    "timeout": "远端工具报告操作超时；无法据此确认操作是否完成，不要盲目重放。",
    "connection_error": "远端工具报告目标连接失败；检查目标服务与网络连通性。",
    "invalid_arguments": "远端工具拒绝参数；按工具定义检查参数后再调用。",
    "remote_error": "远端工具报告失败，但未提供可识别的错误类别，需检查服务端日志。",
}


def remote_error_content(result: types.CallToolResult) -> str:
    """仅提取状态码和固定类别；错误描述只用于分类，永不作为模型指令或日志输出。"""
    structured = result.structured_content or {}
    details = structured.get("error", structured)
    if not isinstance(details, dict):
        details = {}
    status = details.get("status_code")
    if type(status) is not int or not 400 <= status <= 599:
        status = None
    # 分类扫描有界；不展开任意对象，也不把凭据键当作诊断字段。
    snippets = [details.get(key) for key in ("message", "type", "code")]
    snippets.extend(item.text for item in result.content if isinstance(item, types.TextContent))
    text = "\n".join(item[:4096] for item in snippets[:16] if isinstance(item, str))[:8192]
    if status is None and (match := _HTTP_STATUS.search(text)):
        status = int(match.group(1) or match.group(2))
    category = "http_error" if status is not None else "remote_error"
    if status is None:
        for pattern, candidate in _CATEGORIES:
            if re.search(pattern, text, re.IGNORECASE):
                category = candidate
                break
    payload = {
        "code": "AGENT_REMOTE_TOOL_FAILED", "category": category,
        "message": _MESSAGES[category],
    }
    if status is not None:
        payload["http_status"] = status
    return json.dumps(payload, ensure_ascii=False)
