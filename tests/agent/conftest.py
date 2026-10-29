import pytest

from agent_bot.ai.catalog import AiCatalog
from agent_bot.ai.contracts import ModelCreate, ProviderCreate
from agent_bot.settings import DatabaseParams
from ai.fake_database import FakeDatabase


@pytest.fixture
def agent_database():
    db = FakeDatabase()
    db.connection.executescript("""
    CREATE TABLE agent_bot_agent_run(execution_id TEXT PRIMARY KEY, session_id TEXT,
        request_id TEXT, entry TEXT, model TEXT, backend TEXT, status TEXT, duration_ms INTEGER,
        usage TEXT, created_at TEXT, error_code TEXT, prompt TEXT, content TEXT, revisions TEXT);
    CREATE TABLE agent_bot_agent_step(
        execution_id TEXT REFERENCES agent_bot_agent_run(execution_id),
        sequence INTEGER, turn INTEGER, kind TEXT, tool TEXT, call_id TEXT, status TEXT,
        duration_ms INTEGER, summary TEXT, arguments TEXT, result TEXT,
        PRIMARY KEY(execution_id,sequence));
    CREATE TABLE agent_bot_skill(id INTEGER PRIMARY KEY, name TEXT, description TEXT,
        category TEXT, system_prompt TEXT, sample_prompt TEXT, tools TEXT, enabled INTEGER,
        revision INTEGER, created_at TEXT, updated_at TEXT);
    CREATE TABLE agent_bot_mcp(id INTEGER PRIMARY KEY, name TEXT, description TEXT,
        code TEXT UNIQUE, category TEXT, transport TEXT, endpoint TEXT,
        credential TEXT, credential_required INTEGER NOT NULL DEFAULT 0,
        allowed_tools TEXT, search_tool TEXT, timeout_seconds INTEGER, required INTEGER,
        enabled INTEGER, revision INTEGER, created_at TEXT, updated_at TEXT);
    CREATE TABLE agent_bot_agent(id INTEGER PRIMARY KEY, name TEXT, description TEXT,
        code TEXT UNIQUE, system_prompt TEXT, personality_prompt TEXT NOT NULL DEFAULT '',
        model_id INTEGER REFERENCES agent_bot_ai_model(id),
        vision_model_id INTEGER REFERENCES agent_bot_ai_model(id),
        fallback_model_id INTEGER REFERENCES agent_bot_ai_model(id),
        tools TEXT, max_steps INTEGER, options TEXT, enabled INTEGER,
        revision INTEGER, created_at TEXT, updated_at TEXT);
    CREATE TABLE agent_bot_agent_skill(agent_id INTEGER REFERENCES agent_bot_agent(id)
        ON DELETE CASCADE, skill_id INTEGER REFERENCES agent_bot_skill(id),
        PRIMARY KEY(agent_id,skill_id));
    CREATE TABLE agent_bot_agent_mcp(agent_id INTEGER REFERENCES agent_bot_agent(id)
        ON DELETE CASCADE, mcp_id INTEGER REFERENCES agent_bot_mcp(id),
        PRIMARY KEY(agent_id,mcp_id));
    """)
    yield db
    db.close()


@pytest.fixture
def agent_params():
    return DatabaseParams("localhost", 3306, "offline", "offline", "offline")


@pytest.fixture
async def seeded_model(agent_database, agent_params):
    catalog = AiCatalog(agent_params, connect=agent_database.connect)
    provider = await catalog.create_provider(
        ProviderCreate(
            name="local",
            protocol="openai-chat",
            base_url="https://original.invalid/v1",
            enabled=True,
        )
    )
    model = await catalog.create_model(
        ModelCreate(
            alias="local-model", upstream_model="exact/id", provider_id=provider.id, enabled=True
        )
    )
    return catalog, provider, model
