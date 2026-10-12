"""Static assets must not intercept management, OneBot or MCP paths."""

import httpx
from fastapi import FastAPI


async def test_missing_build_has_clear_error_and_does_not_mask_api(tmp_path):
    from agent_bot.static import install_static

    app = FastAPI()
    install_static(app, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline.test"
    ) as client:
        assert (await client.get("/")).status_code == 503
        for path in ("/api/v1/missing", "/onebot/v11/ws", "/mcp", "/unknown"):
            response = await client.get(path)
            assert response.status_code == 404
            assert "text/html" not in response.headers.get("content-type", "")


async def test_built_index_and_assets_are_available_without_spa_catchall(tmp_path):
    from agent_bot.static import install_static

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>AgentBot</html>", encoding="utf-8")
    (tmp_path / "assets/app.js").write_text("console.log('app')", encoding="utf-8")
    app = FastAPI()
    install_static(app, tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline.test"
    ) as client:
        assert (await client.get("/")).text == "<html>AgentBot</html>"
        assert (await client.get("/assets/app.js")).status_code == 200
        assert (await client.get("/api/missing")).status_code == 404
