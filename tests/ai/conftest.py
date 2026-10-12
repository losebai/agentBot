import pytest

from agent_bot.ai.catalog import AiCatalog
from agent_bot.settings import DatabaseParams

from .fake_database import FakeDatabase


@pytest.fixture
def ai_database():
    database = FakeDatabase()
    yield database
    database.close()


@pytest.fixture
def ai_params():
    return DatabaseParams("localhost", 3306, "offline", "offline", "offline")


@pytest.fixture
def catalog(ai_database, ai_params):
    return AiCatalog(ai_params, connect=ai_database.connect)
