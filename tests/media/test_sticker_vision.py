"""从真实表情预览到模型HTTP请求验证图片字节，不调用外部供应商。"""

import base64
import json
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from agent.test_service import service_for
from agent_bot.ai.contracts import ProviderUpdate
from agent_bot.ai.providers.langchain import LangChainProvider
from agent_bot.plugins.stickers.service import StickerService
from ai.test_providers import ByteStream
from stickers.test_images import picture
from stickers.test_store import store as sticker_store  # noqa: F401


@pytest.mark.parametrize("protocol", ["openai-chat", "openai-responses"])
@pytest.mark.parametrize("format", ["GIF", "WEBP"])
async def test_sticker_preview_bytes_reach_provider_request(
    media_model, media_database, agent_params, tmp_path, media_clock,
    sticker_store, protocol, format,  # noqa: F811
):
    """附件引用必须转换成真实Base64图片块，不能只发送提示词或本地附件ID。"""
    from media.test_store import storage

    catalog, row, model = media_model
    await catalog.update_provider(row.id, ProviderUpdate(
        expected_revision=row.revision, protocol=protocol,
    ))
    data = picture(format, animated=True)
    item = await sticker_store.create("动画.jpg", data, "image/jpeg")
    _, preview = await sticker_store.read(item.id, preview=True)
    answer = json.dumps({"description": "红蓝两帧", "emotion": "开心", "usage_hint": "打招呼"})
    sent = []

    def handle(request):
        """在最终HTTP边界截获负载，并返回合法流式识图结果。"""
        sent.append(json.loads(request.content))
        if protocol == "openai-chat":
            events = [{"model": "exact/id", "choices": [{
                "index": 0, "delta": {"content": answer}, "finish_reason": "stop",
            }]}]
        else:
            events = [
                {"type": "response.output_text.delta", "delta": answer},
                {"type": "response.completed", "response": {
                    "status": "completed", "model": "exact/id", "output": [{
                        "type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": answer}],
                    }],
                }},
            ]
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
        if protocol == "openai-chat":
            body += "data: [DONE]\n\n"
        return httpx.Response(200, stream=ByteStream(body.encode()))

    provider = LangChainProvider(protocol, transport=httpx.MockTransport(handle))
    try:
        async with storage(
            media_database, agent_params, tmp_path / "inputs", media_clock,
        ) as inputs:
            agent = service_for(media_database, agent_params)
            agent.providers = {protocol: provider}
            agent.media_store = inputs
            agent.tool_runtime.definitions = ()
            runtime = SimpleNamespace(
                config=SimpleNamespace(
                    chat_sticker_model=f"model:{model.id}",
                    chat_sticker_vision_prompt="", chat_sticker_timeout_seconds=60,
                ),
                state=SimpleNamespace(ai_client=SimpleNamespace(agent_service=agent)),
                media_resources=SimpleNamespace(store=inputs),
            )
            saved = await StickerService(sticker_store, runtime).analyze(
                item.id, expected_revision=item.revision,
            )
            assert saved.analysis_status == "ready" and saved.description == "红蓝两帧"
            assert not inputs._leases
    finally:
        await provider.aclose()

    assert len(sent) == 1
    messages = sent[0]["messages" if protocol == "openai-chat" else "input"]
    user = next(message for message in messages if message.get("role") == "user")
    if protocol == "openai-chat":
        url = next(block["image_url"]["url"] for block in user["content"]
                   if block.get("type") == "image_url")
    else:
        url = next(block["image_url"] for block in user["content"]
                   if block.get("type") == "input_image")
    prefix, encoded = url.split(",", 1)
    assert prefix == "data:image/png;base64"
    with Image.open(BytesIO(base64.b64decode(encoded, validate=True))) as actual:
        with Image.open(BytesIO(preview)) as expected:
            assert actual.size == expected.size
            assert actual.convert("RGB").tobytes() == expected.convert("RGB").tobytes()
