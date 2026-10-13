"""自动发言只记录选择结果和脱敏状态，不记录群聊状态正文。"""

import uuid
from datetime import datetime, timezone

from agent_bot.ai.database import AiDatabase


class AutoSpeakRecords(AiDatabase):
    """复用有界事务和取消等待，独立于模型正文会话记录。"""

    async def save(
        self,
        *,
        session_id,
        request_id,
        mcp_code,
        mcp_revision,
        status,
        response,
        duration_ms,
        error_code=None,
    ):
        """显式白名单保存字段，调用失败不包含任何远端响应。"""
        values = {
            "id": uuid.uuid4().hex,
            "session_id": session_id,
            "request_id": request_id,
            "mcp_code": mcp_code,
            "mcp_revision": mcp_revision,
            "status": status,
            "should_speak": int(response.should_speak) if response else None,
            "confidence": response.confidence if response else None,
            "duration_ms": duration_ms,
            "error_code": error_code,
            "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
        }

        def write(connection):
            """每次分类只写一条终态，不将记录失败转化为重新分类。"""
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO agent_bot_auto_speak_decision ({','.join(values)}) "
                    f"VALUES ({','.join(['%s'] * len(values))})",
                    tuple(values.values()),
                )

        await self.run(write, write=True)
