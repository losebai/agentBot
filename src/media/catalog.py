"""在调用方事务内读写媒体能力，兼容尚未安装媒体表的文本服务。"""

import pymysql

from agent_bot.ai.contracts import ModelMediaCapabilities, ModelView
from agent_bot.ai.database import query
from agent_bot.media.errors import MediaError


def read_model_media(connection, model_id: int) -> tuple[ModelMediaCapabilities, bool]:
    """只有明确的缺表错误可以降级；其他数据库错误交回事务层。"""
    entries, initialized = read_media_batch(connection, (model_id,))
    return entries.get(model_id, ModelMediaCapabilities()), initialized


def read_media_batch(connection, ids) -> tuple[dict[int, ModelMediaCapabilities], bool]:
    """同一事务内批量取能力，避免每个模型重新借数据库连接。"""
    try:
        rows = (
            query(
                connection,
                "SELECT model_id,vision_enabled,image_api_profile,"
                "audio_chat_enabled FROM agent_bot_ai_model_media WHERE model_id IN ("
                + ",".join("%s" for _ in ids)
                + ")",
                tuple(ids),
            )
            if ids
            else query(connection, "SELECT model_id FROM agent_bot_ai_model_media LIMIT 0")
        )
    except pymysql.DatabaseError as error:
        if error.args[0] == 1146:
            return {}, False
        raise
    return {
        row["model_id"]: ModelMediaCapabilities.model_validate(
            {key: value for key, value in row.items() if key != "model_id"}
        )
        for row in rows
    }, True


def model_view(connection, row) -> ModelView:
    """组装模型和能力的同事务快照。"""
    capabilities, initialized = read_model_media(connection, row["id"])
    return ModelView.model_validate(
        {**row, "media": capabilities, "media_initialized": initialized}
    )


def validate_protocol(protocol: str, capabilities: ModelMediaCapabilities) -> None:
    """Ollama 仅支持图像输入，不支持这里的图片或对话音频生成。"""
    if protocol == "ollama" and (
        capabilities.image_api_profile != "none" or capabilities.audio_chat_enabled
    ):
        raise MediaError("MEDIA_PROTOCOL_UNSUPPORTED", status_code=422)


def write_model_media(connection, model_id: int, capabilities: ModelMediaCapabilities) -> None:
    """与模型版本共享事务；使用跨 MySQL 和离线替身的显式更新。"""
    _, initialized = read_model_media(connection, model_id)
    if not initialized:
        raise MediaError("MEDIA_NOT_INITIALIZED", status_code=503)
    values = (
        capabilities.vision_enabled,
        capabilities.image_api_profile,
        capabilities.audio_chat_enabled,
        model_id,
    )
    if query(
        connection,
        "SELECT model_id FROM agent_bot_ai_model_media WHERE model_id=%s",
        (model_id,),
        one=True,
    ):
        query(
            connection,
            "UPDATE agent_bot_ai_model_media SET vision_enabled=%s,"
            "image_api_profile=%s,audio_chat_enabled=%s WHERE model_id=%s",
            values,
        )
    else:
        query(
            connection,
            "INSERT INTO agent_bot_ai_model_media "
            "(vision_enabled,image_api_profile,audio_chat_enabled,model_id) VALUES (%s,%s,%s,%s)",
            values,
        )
