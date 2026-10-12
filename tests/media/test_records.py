"""媒体运行记录的分页与不可覆盖终态。"""

import pytest

from agent_bot.media.contracts import MediaRunFinish, MediaRunStart
from agent_bot.media.errors import MediaError


async def test_records_keyset_and_terminal_cas(media_database, agent_params, media_clock):
    """同一时间的运行按执行标识稳定分页，过滤条件绑定游标。"""
    from agent_bot.media.records import MediaRunStore

    records = MediaRunStore(agent_params, connect=media_database.connect)
    for identifier in ("a", "b", "c"):
        await records.begin(
            MediaRunStart(
                execution_id=identifier * 32,
                entry="admin",
                session_id="s",
                request_id="r",
                model="alias",
                output_type="IMAGE",
                prompt="画图",
                created_at=media_clock(),
            )
        )
    first = await records.list_runs(limit=1, session_id="s")
    second = await records.list_runs(limit=1, session_id="s", cursor=first.next_cursor)
    assert first.items[0].execution_id == "c" * 32 and second.items[0].execution_id == "b" * 32
    with pytest.raises(MediaError):
        await records.list_runs(cursor=first.next_cursor)
    finished = MediaRunFinish(
        execution_id="a" * 32, status="completed", duration_ms=1, content="done"
    )
    await records.finish(finished)
    assert (await records.get_run("a" * 32)).content == "done"
    with pytest.raises(MediaError) as conflict:
        await records.finish(finished)
    assert conflict.value.status_code == 409
