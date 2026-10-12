from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from agent_bot.services.workflows import WorkflowDefinition
from agent_bot.settings import DatabaseParams
from agent_bot.workflow_cli import run


def test_sync_uses_configured_directory_and_reports_only_counts(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    workflow_file = tmp_path / "custom.json"
    workflow_file.write_text('{"nodes":[]}', encoding="utf-8")
    store = Mock()
    store.upsert.return_value = 1
    monkeypatch.setattr(
        "agent_bot.workflow_cli._load_store",
        lambda _env_file: (
            store,
            DatabaseParams(
                host="127.0.0.1",
                port=3306,
                user="dispatch_chat",
                password="secret",
                database="dispatch_chat",
            ),
        ),
    )

    assert run(["sync", str(tmp_path)]) == 0

    store.migrate.assert_called_once_with()
    store.upsert.assert_called_once()
    definitions = store.upsert.call_args.args[0]
    assert definitions[0].description == ""
    assert definitions[0].additional_positive_prompt == ""
    assert definitions[0].additional_negative_prompt == ""
    output = capsys.readouterr().out
    assert "count=1" in output
    assert str(tmp_path) not in output
    assert "nodes" not in output


def test_list_reports_description_without_fixed_prompts(monkeypatch, capsys) -> None:
    """管理命令显示用途描述，但不输出运行时固定提示词。"""

    store = Mock()
    store.list_all.return_value = (
        WorkflowDefinition(
            workflow_key="portrait",
            display_name="Portrait",
            source_file="portrait.json",
            source_format="api",
            ui_json=None,
            api_json='{"1":{"class_type":"PreviewImage","inputs":{}}}',
            content_sha256="a" * 64,
            positive_node_id="2",
            negative_node_id="3",
            output_node_id="1",
            seed_node_id="4",
            description="适合写实人像",
            additional_positive_prompt="PRIVATE_FIXED_POSITIVE",
            additional_negative_prompt="PRIVATE_FIXED_NEGATIVE",
        ),
    )
    monkeypatch.setattr(
        "agent_bot.workflow_cli._load_store",
        lambda _env_file: (
            store,
            DatabaseParams(
                host="127.0.0.1",
                port=3306,
                user="dispatch_chat",
                password="secret",
                database="dispatch_chat",
            ),
        ),
    )

    assert run(["list"]) == 0

    output = capsys.readouterr().out
    assert "description=适合写实人像" in output
    assert "PRIVATE_FIXED" not in output
