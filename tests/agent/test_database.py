import asyncio
import threading

import pytest

from agent_bot.ai.database import AiDatabase


async def test_cancelled_read_joins_database_worker(agent_database, agent_params):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def read(connection):
        entered.set()
        release.wait(2)
        finished.set()

    database = AiDatabase(agent_params, connect=agent_database.connect)
    task = asyncio.create_task(database.run(read))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done(), "Cancellation must retain the database lease until the worker ends"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.to_thread(finished.wait, 2)
