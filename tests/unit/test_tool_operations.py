"""跨工具会话重试的持久副作用记录。"""

import asyncio
import hashlib

import fakeredis.aioredis
import pytest


@pytest.mark.asyncio
async def test_tool_operation_survives_new_store_and_refuses_uncertain_replay() -> None:
    """完成结果跨实例复用，未确认完成的操作不能再次获得执行权。"""

    from agent_bot.integrations.tool_operations import RedisToolOperationStore
    from agent_bot.services.tool_operations import MentionEffect, ToolOperationConflict

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    signature = hashlib.sha256(b"mention-10002").hexdigest()
    store = RedisToolOperationStore(redis, key_prefix="test:effects", ttl_seconds=60)
    try:
        assert await store.begin("job-1", "mention_member", signature) is None
        with pytest.raises(ToolOperationConflict):
            await store.begin("job-1", "mention_member", signature)
        result = MentionEffect(message_id=9, target_user_id=10002)
        await store.complete("job-1", "mention_member", signature, result)
        replacement = RedisToolOperationStore(redis, key_prefix="test:effects", ttl_seconds=60)
        assert await replacement.begin("job-1", "mention_member", signature) == result
        with pytest.raises(ToolOperationConflict):
            await replacement.begin("job-1", "mention_member", "a" * 64)
        assert await replacement.begin("job-2", "mention_member", signature) is None
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_tool_operation_atomic_claim_has_one_owner() -> None:
    """并发重试只能让一个调用获得真实副作用的执行权。"""

    from agent_bot.integrations.tool_operations import RedisToolOperationStore
    from agent_bot.services.tool_operations import ToolOperationConflict

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisToolOperationStore(redis, key_prefix="test:claim", ttl_seconds=60)
    try:
        results = await asyncio.gather(
            *(store.begin("job", "tts_generate", "a" * 64) for _ in range(5)),
            return_exceptions=True,
        )
        assert results.count(None) == 1
        assert sum(isinstance(result, ToolOperationConflict) for result in results) == 4
        keys = await redis.keys("test:claim:*")
        assert len(keys) == 1
        assert 0 < await redis.ttl(keys[0]) <= 60
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_completed_tool_effect_restores_media_from_persistent_result() -> None:
    """媒体结果独立保存，进程重建后仍可恢复，无需重新生成。"""

    from agent_bot.integrations.tool_operations import RedisToolOperationStore
    from agent_bot.services.tool_operations import ImageEffect

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisToolOperationStore(redis, key_prefix="test:image", ttl_seconds=60)
    try:
        await store.begin("job", "local_workflow_generate_image", "a" * 64)
        result = ImageEffect(workflow_key="example", media_type="image/png", data="aGVsbG8=")
        await store.complete("job", "local_workflow_generate_image", "a" * 64, result)
        assert await store.completed("job", "local_workflow_generate_image") == result
        assert await store.completed("different-job", "local_workflow_generate_image") is None
    finally:
        await redis.aclose()
