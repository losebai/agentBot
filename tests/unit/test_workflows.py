from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from agent_bot.services.workflows import (
    WorkflowCatalogImporter,
    WorkflowDefinition,
    WorkflowValidationError,
    prepare_workflow_prompt,
    validate_workflow_bindings,
)


def _write_metadata(
    workflow_path: Path,
    *,
    description: str = "适合写实人像",
    additional_positive_prompt: str = "",
    additional_negative_prompt: str = "",
) -> None:
    """为测试工作流写入独立且完整的伴随元数据。"""

    metadata_path = workflow_path.with_name(f"{workflow_path.stem}.meta.json")
    metadata_path.write_text(
        json.dumps(
            {
                "description": description,
                "additional_positive_prompt": additional_positive_prompt,
                "additional_negative_prompt": additional_negative_prompt,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _ui_workflow() -> dict[str, object]:
    return {
        "last_node_id": 7,
        "nodes": [
            {"id": 2, "type": "CLIPTextEncode", "widgets_values": ["old positive"]},
            {"id": 3, "type": "CLIPTextEncode", "widgets_values": ["old negative"]},
        ],
    }


def _convertible_ui_workflow() -> dict[str, object]:
    return {
        "last_node_id": 10,
        "nodes": [
            {
                "id": 1,
                "type": "CheckpointLoaderSimple",
                "inputs": [
                    {
                        "name": "ckpt_name",
                        "type": "COMBO",
                        "widget": {"name": "ckpt_name"},
                        "link": None,
                    }
                ],
                "outputs": [
                    {"name": "MODEL", "type": "MODEL"},
                    {"name": "CLIP", "type": "CLIP"},
                    {"name": "VAE", "type": "VAE"},
                ],
                "widgets_values": ["model.safetensors"],
            },
            {
                "id": 2,
                "type": "CLIPTextEncode",
                "inputs": [
                    {"name": "clip", "type": "CLIP", "link": 1},
                    {
                        "name": "text",
                        "type": "STRING",
                        "widget": {"name": "text"},
                        "link": None,
                    },
                ],
                "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING"}],
                "widgets_values": ["old positive"],
            },
            {
                "id": 3,
                "type": "CLIPTextEncode",
                "inputs": [
                    {"name": "clip", "type": "CLIP", "link": 2},
                    {
                        "name": "text",
                        "type": "STRING",
                        "widget": {"name": "text"},
                        "link": None,
                    },
                ],
                "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING"}],
                "widgets_values": ["old negative"],
            },
            {
                "id": 4,
                "type": "EmptyLatentImage",
                "inputs": [
                    {
                        "name": "width",
                        "type": "INT",
                        "widget": {"name": "width"},
                        "link": None,
                    },
                    {
                        "name": "height",
                        "type": "INT",
                        "widget": {"name": "height"},
                        "link": None,
                    },
                    {
                        "name": "batch_size",
                        "type": "INT",
                        "widget": {"name": "batch_size"},
                        "link": None,
                    },
                ],
                "outputs": [{"name": "LATENT", "type": "LATENT"}],
                "widgets_values": [1024, 1024, 1],
            },
            {
                "id": 10,
                "type": "KSampler",
                "inputs": [
                    {"name": "model", "type": "MODEL", "link": 3},
                    {"name": "positive", "type": "CONDITIONING", "link": 4},
                    {"name": "negative", "type": "CONDITIONING", "link": 5},
                    {"name": "latent_image", "type": "LATENT", "link": 6},
                    {
                        "name": "seed",
                        "type": "INT",
                        "widget": {"name": "seed"},
                        "link": None,
                    },
                    {
                        "name": "steps",
                        "type": "INT",
                        "widget": {"name": "steps"},
                        "link": None,
                    },
                    {
                        "name": "cfg",
                        "type": "FLOAT",
                        "widget": {"name": "cfg"},
                        "link": None,
                    },
                    {
                        "name": "sampler_name",
                        "type": "COMBO",
                        "widget": {"name": "sampler_name"},
                        "link": None,
                    },
                    {
                        "name": "scheduler",
                        "type": "COMBO",
                        "widget": {"name": "scheduler"},
                        "link": None,
                    },
                    {
                        "name": "denoise",
                        "type": "FLOAT",
                        "widget": {"name": "denoise"},
                        "link": None,
                    },
                ],
                "outputs": [{"name": "LATENT", "type": "LATENT"}],
                "widgets_values": [123, "randomize", 8, 4.0, "euler", "simple", 1.0],
            },
            {
                "id": 6,
                "type": "VAEDecode",
                "inputs": [
                    {"name": "samples", "type": "LATENT", "link": 7},
                    {"name": "vae", "type": "VAE", "link": 8},
                ],
                "outputs": [{"name": "IMAGE", "type": "IMAGE"}],
                "widgets_values": [],
            },
            {
                "id": 7,
                "type": "PreviewImage",
                "inputs": [{"name": "images", "type": "IMAGE", "link": 9}],
                "outputs": [{"name": "IMAGE", "type": "IMAGE"}],
                "widgets_values": [],
            },
        ],
        "links": [
            [1, 1, 1, 2, 0, "CLIP"],
            [2, 1, 1, 3, 0, "CLIP"],
            [3, 1, 0, 10, 0, "MODEL"],
            [4, 2, 0, 10, 1, "CONDITIONING"],
            [5, 3, 0, 10, 2, "CONDITIONING"],
            [6, 4, 0, 10, 3, "LATENT"],
            [7, 10, 0, 6, 0, "LATENT"],
            [8, 1, 2, 6, 1, "VAE"],
            [9, 6, 0, 7, 0, "IMAGE"],
        ],
    }


def test_importer_does_not_require_or_import_workflow_metadata(
    tmp_path: Path,
) -> None:
    workflow_path = tmp_path / "portrait.json"
    workflow_path.write_text(json.dumps(_ui_workflow()), encoding="utf-8")
    _write_metadata(workflow_path, description="不得同步到数据库")

    (definition,) = WorkflowCatalogImporter().scan(tmp_path)

    assert definition.description == ""
    assert definition.additional_positive_prompt == ""
    assert definition.additional_negative_prompt == ""


def test_importer_collapses_identical_content_by_sorted_filename(tmp_path: Path) -> None:
    payload = json.dumps(_ui_workflow())
    for name, description in (("a.json", "保留"), ("b.json", "忽略")):
        workflow_path = tmp_path / name
        workflow_path.write_text(payload, encoding="utf-8")
        _write_metadata(workflow_path, description=description)

    records = WorkflowCatalogImporter().scan(tmp_path)

    assert [item.source_file for item in records] == ["a.json"]


def test_importer_collapses_identical_content_before_key_collision_check(
    tmp_path: Path,
) -> None:
    """内容相同时，即使文件名规范化为同一 key 也只保留排序首项。"""

    payload = _ui_workflow()
    first = tmp_path / "A Portrait.json"
    second = tmp_path / "a-portrait.json"
    first.write_text(json.dumps(payload), encoding="utf-8")
    second.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _write_metadata(first, description="排序首项")
    _write_metadata(second, description="重复内容")

    records = WorkflowCatalogImporter().scan(tmp_path)

    assert len(records) == 1
    assert records[0].source_file == "A Portrait.json"


def test_importer_pairs_case_only_names_exactly_and_sorts_deterministically(
    tmp_path: Path,
) -> None:
    """大小写敏感文件系统上的同名变体不能串用伴随元数据。"""

    upper_directory = tmp_path / "upper"
    lower_directory = tmp_path / "lower"
    upper_directory.mkdir()
    lower_directory.mkdir()
    upper_workflow = upper_directory / "A.json"
    lower_workflow = lower_directory / "a.json"
    payload = json.dumps(_ui_workflow())
    upper_workflow.write_text(payload, encoding="utf-8")
    lower_workflow.write_text(payload, encoding="utf-8")
    _write_metadata(upper_workflow, description="大写文件元数据")
    _write_metadata(lower_workflow, description="小写文件元数据")

    class DirectoryEntries:
        """模拟可同时返回大小写文件名的目录。"""

        def is_dir(self) -> bool:
            """测试目录始终可用。"""

            return True

        def glob(self, _pattern: str) -> list[Path]:
            """故意以小写文件在前的顺序返回目录项。"""

            return [
                lower_workflow,
                upper_workflow,
                lower_directory / "a.meta.json",
                upper_directory / "A.meta.json",
            ]

    records = WorkflowCatalogImporter().scan(DirectoryEntries())  # type: ignore[arg-type]

    assert [record.source_file for record in records] == ["A.json"]


def test_importer_ignores_orphan_metadata(tmp_path: Path) -> None:
    workflow_path = tmp_path / "portrait.json"
    workflow_path.write_text(json.dumps(_ui_workflow()), encoding="utf-8")
    (tmp_path / "orphan.meta.json").write_text("not json", encoding="utf-8")

    records = WorkflowCatalogImporter().scan(tmp_path)

    assert [record.source_file for record in records] == ["portrait.json"]


def test_importer_converts_single_output_ui_workflow_to_api_json(tmp_path: Path) -> None:
    workflow_path = tmp_path / "custom.json"
    workflow_path.write_text(
        json.dumps(_convertible_ui_workflow()), encoding="utf-8"
    )
    _write_metadata(workflow_path)

    (record,) = WorkflowCatalogImporter().scan(tmp_path)

    assert record.source_format == "ui"
    assert record.executable is True
    assert record.api_json is not None
    assert record.positive_node_id == "2"
    assert record.negative_node_id == "3"
    assert record.output_node_id == "7"
    assert record.seed_node_id == "10"
    prompt = json.loads(record.api_json)
    assert prompt["1"]["inputs"] == {"ckpt_name": "model.safetensors"}
    assert prompt["2"]["inputs"] == {"clip": ["1", 1], "text": "old positive"}
    assert prompt["10"]["inputs"] == {
        "model": ["1", 0],
        "positive": ["2", 0],
        "negative": ["3", 0],
        "latent_image": ["4", 0],
        "seed": 123,
        "steps": 8,
        "cfg": 4.0,
        "sampler_name": "euler",
        "scheduler": "simple",
        "denoise": 1.0,
    }


def test_importer_converts_save_image_advanced_output(tmp_path: Path) -> None:
    workflow = _convertible_ui_workflow()
    nodes = workflow["nodes"]
    assert isinstance(nodes, list)
    output = nodes[-1]
    assert isinstance(output, dict)
    output["type"] = "SaveImageAdvanced"
    output["inputs"].append(
        {
            "name": "filename_prefix",
            "type": "STRING",
            "widget": {"name": "filename_prefix"},
            "link": None,
        }
    )
    output["widgets_values"] = ["generated"]
    workflow_path = tmp_path / "advanced.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    _write_metadata(workflow_path)

    (record,) = WorkflowCatalogImporter().scan(tmp_path)

    assert record.executable is True
    assert record.api_json is not None
    prompt = json.loads(record.api_json)
    assert record.output_node_id == "7"
    assert prompt["7"]["inputs"] == {
        "images": ["6", 0],
        "filename_prefix": "generated",
    }


def test_importer_uses_terminal_node_when_image_outputs_are_chained(
    tmp_path: Path,
) -> None:
    """串联的图片输出只有一个末端节点，不应被误判为并列输出。"""

    workflow = _convertible_ui_workflow()
    nodes = workflow["nodes"]
    links = workflow["links"]
    assert isinstance(nodes, list)
    assert isinstance(links, list)
    nodes.append(
        {
            "id": 8,
            "type": "SaveImage",
            "inputs": [
                {"name": "images", "type": "IMAGE", "link": 10},
                {
                    "name": "filename_prefix",
                    "type": "STRING",
                    "widget": {"name": "filename_prefix"},
                    "link": None,
                },
            ],
            "outputs": [{"name": "IMAGE", "type": "IMAGE"}],
            "widgets_values": ["generated"],
        }
    )
    links.append([10, 7, 0, 8, 0, "IMAGE"])
    workflow_path = tmp_path / "chained-outputs.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")

    (record,) = WorkflowCatalogImporter().scan(tmp_path)

    assert record.output_node_id == "8"
    assert record.api_json is not None
    prompt = json.loads(record.api_json)
    assert prompt["8"]["inputs"] == {
        "images": ["7", 0],
        "filename_prefix": "generated",
    }


def test_importer_rejects_ui_workflow_with_multiple_image_outputs(tmp_path: Path) -> None:
    workflow = _convertible_ui_workflow()
    nodes = workflow["nodes"]
    links = workflow["links"]
    assert isinstance(nodes, list)
    assert isinstance(links, list)
    second_output = deepcopy(nodes[-1])
    second_output["id"] = 8
    second_output["inputs"][0]["link"] = 10
    nodes.append(second_output)
    links.append([10, 6, 0, 8, 0, "IMAGE"])
    workflow_path = tmp_path / "ambiguous.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    _write_metadata(workflow_path)

    with pytest.raises(WorkflowValidationError):
        WorkflowCatalogImporter().scan(tmp_path)


def test_importer_rejects_output_with_only_non_image_input_link(tmp_path: Path) -> None:
    workflow = _convertible_ui_workflow()
    nodes = workflow["nodes"]
    links = workflow["links"]
    assert isinstance(nodes, list)
    assert isinstance(links, list)
    output = nodes[-1]
    assert isinstance(output, dict)
    output["inputs"][0]["link"] = None
    output["inputs"].append(
        {
            "name": "filename_prefix",
            "type": "STRING",
            "widget": {"name": "filename_prefix"},
            "link": 9,
        }
    )
    output["widgets_values"] = ["generated"]
    links[-1][4] = 1
    workflow_path = tmp_path / "wrong-output.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    _write_metadata(workflow_path)

    with pytest.raises(WorkflowValidationError):
        WorkflowCatalogImporter().scan(tmp_path)


def test_importer_rejects_negative_link_source_slot(tmp_path: Path) -> None:
    workflow = _convertible_ui_workflow()
    links = workflow["links"]
    assert isinstance(links, list)
    links[-1][2] = -1
    workflow_path = tmp_path / "invalid-slot.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    _write_metadata(workflow_path)

    with pytest.raises(WorkflowValidationError, match="link is invalid"):
        WorkflowCatalogImporter().scan(tmp_path)


def test_importer_rejects_concrete_type_mismatch_hidden_by_wildcard(
    tmp_path: Path,
) -> None:
    workflow = _convertible_ui_workflow()
    links = workflow["links"]
    assert isinstance(links, list)
    links[-1] = [9, 10, 0, 7, 0, "*"]
    workflow_path = tmp_path / "wildcard-mismatch.json"
    workflow_path.write_text(
        json.dumps(workflow), encoding="utf-8"
    )
    _write_metadata(workflow_path)

    with pytest.raises(WorkflowValidationError, match="link is invalid"):
        WorkflowCatalogImporter().scan(tmp_path)


@pytest.mark.parametrize(
    ("input_name", "link_id"),
    (("model", 3), ("latent_image", 6)),
)
def test_importer_rejects_missing_required_sampler_link(
    tmp_path: Path,
    input_name: str,
    link_id: int,
) -> None:
    workflow = _convertible_ui_workflow()
    nodes = workflow["nodes"]
    links = workflow["links"]
    assert isinstance(nodes, list)
    assert isinstance(links, list)
    sampler = next(node for node in nodes if isinstance(node, dict) and node["id"] == 10)
    sampler_input = next(item for item in sampler["inputs"] if item["name"] == input_name)
    sampler_input["link"] = None
    workflow["links"] = [link for link in links if link[0] != link_id]
    workflow_path = tmp_path / f"missing-{input_name}.json"
    workflow_path.write_text(
        json.dumps(workflow), encoding="utf-8"
    )
    _write_metadata(workflow_path)

    with pytest.raises(WorkflowValidationError, match="sampler input is invalid"):
        WorkflowCatalogImporter().scan(tmp_path)


def test_importer_rejects_missing_sampler_seed(tmp_path: Path) -> None:
    workflow = _convertible_ui_workflow()
    nodes = workflow["nodes"]
    assert isinstance(nodes, list)
    sampler = next(node for node in nodes if isinstance(node, dict) and node["id"] == 10)
    sampler["inputs"] = [
        item for item in sampler["inputs"] if item["name"] != "seed"
    ]
    sampler["widgets_values"] = [8, 4.0, "euler", "simple", 1.0]
    workflow_path = tmp_path / "missing-seed.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    _write_metadata(workflow_path)

    with pytest.raises(WorkflowValidationError, match="sampler seed is invalid"):
        WorkflowCatalogImporter().scan(tmp_path)


def test_importer_rejects_shared_positive_and_negative_prompt_node(
    tmp_path: Path,
) -> None:
    workflow = _convertible_ui_workflow()
    links = workflow["links"]
    assert isinstance(links, list)
    links[4][1] = 2
    workflow_path = tmp_path / "shared-prompt.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    _write_metadata(workflow_path)

    with pytest.raises(WorkflowValidationError, match="prompt bindings are ambiguous"):
        WorkflowCatalogImporter().scan(tmp_path)


def test_importer_follows_conditioning_node_to_unique_prompt(tmp_path: Path) -> None:
    workflow = _convertible_ui_workflow()
    nodes = workflow["nodes"]
    links = workflow["links"]
    assert isinstance(nodes, list)
    assert isinstance(links, list)
    sampler = next(node for node in nodes if isinstance(node, dict) and node["id"] == 10)
    sampler["inputs"][1]["link"] = 10
    nodes.append(
        {
            "id": 11,
            "type": "ConditioningSetArea",
            "inputs": [
                {"name": "conditioning", "type": "CONDITIONING", "link": 4},
                {
                    "name": "strength",
                    "type": "FLOAT",
                    "widget": {"name": "strength"},
                    "link": None,
                },
            ],
            "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING"}],
            "widgets_values": [1.0],
        }
    )
    links[3][3] = 11
    links[3][4] = 0
    links.append([10, 11, 0, 10, 1, "CONDITIONING"])
    workflow_path = tmp_path / "conditioning.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    _write_metadata(workflow_path)

    (record,) = WorkflowCatalogImporter().scan(tmp_path)

    assert record.positive_node_id == "2"
    assert record.negative_node_id == "3"


def test_importer_rejects_conditioning_dependency_cycle(tmp_path: Path) -> None:
    workflow = _convertible_ui_workflow()
    nodes = workflow["nodes"]
    links = workflow["links"]
    assert isinstance(nodes, list)
    assert isinstance(links, list)
    sampler = next(node for node in nodes if isinstance(node, dict) and node["id"] == 10)
    sampler["inputs"][1]["link"] = 12
    nodes.extend(
        [
            {
                "id": 11,
                "type": "ConditioningCombine",
                "inputs": [
                    {"name": "conditioning_1", "type": "CONDITIONING", "link": 4},
                    {"name": "conditioning_2", "type": "CONDITIONING", "link": 11},
                ],
                "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING"}],
                "widgets_values": [],
            },
            {
                "id": 12,
                "type": "ConditioningZeroOut",
                "inputs": [
                    {"name": "conditioning", "type": "CONDITIONING", "link": 10},
                ],
                "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING"}],
                "widgets_values": [],
            },
        ]
    )
    links[3][3] = 11
    links[3][4] = 0
    links.extend(
        [
            [10, 11, 0, 12, 0, "CONDITIONING"],
            [11, 12, 0, 11, 1, "CONDITIONING"],
            [12, 11, 0, 10, 1, "CONDITIONING"],
        ]
    )
    workflow_path = tmp_path / "cycle.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    _write_metadata(workflow_path)

    with pytest.raises(WorkflowValidationError, match="dependency cycle"):
        WorkflowCatalogImporter().scan(tmp_path)


def test_importer_rejects_invalid_links_in_non_generation_ui(tmp_path: Path) -> None:
    workflow = _ui_workflow()
    workflow["links"] = "invalid"
    workflow_path = tmp_path / "not-generation.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    _write_metadata(workflow_path)

    with pytest.raises(WorkflowValidationError, match="links are invalid"):
        WorkflowCatalogImporter().scan(tmp_path)


def test_importer_rejects_missing_input_link_in_non_generation_ui(
    tmp_path: Path,
) -> None:
    workflow = _ui_workflow()
    nodes = workflow["nodes"]
    assert isinstance(nodes, list)
    first_node = nodes[0]
    assert isinstance(first_node, dict)
    first_node["inputs"] = [
        {"name": "clip", "type": "CLIP", "link": 999},
    ]
    workflow["links"] = []
    workflow_path = tmp_path / "missing-link.json"
    workflow_path.write_text(json.dumps(workflow), encoding="utf-8")
    _write_metadata(workflow_path)

    with pytest.raises(WorkflowValidationError, match="link is missing"):
        WorkflowCatalogImporter().scan(tmp_path)


def test_importer_preserves_all_ui_json_and_attaches_sdxl_api_template(
    tmp_path: Path,
) -> None:
    sdxl_path = tmp_path / "SDXL-Lightning-4步文生图.json"
    sdxl_path.write_text(
        json.dumps(_ui_workflow(), ensure_ascii=False), encoding="utf-8"
    )
    _write_metadata(sdxl_path)
    custom_path = tmp_path / "custom.json"
    custom_path.write_text(
        json.dumps({"nodes": [{"id": 1, "type": "CustomNode"}]}),
        encoding="utf-8",
    )
    _write_metadata(custom_path)

    records = WorkflowCatalogImporter().scan(tmp_path)

    assert [record.workflow_key for record in records] == ["custom", "sdxl-lightning-4step"]
    custom, sdxl = records
    assert custom.ui_json is not None
    assert custom.api_json is None
    assert sdxl.ui_json is not None
    assert sdxl.api_json is not None
    assert sdxl.positive_node_id == "2"
    assert sdxl.negative_node_id == "3"
    assert sdxl.output_node_id == "7"
    assert sdxl.seed_node_id == "10"


def test_importer_rejects_invalid_json_without_dropping_other_files(tmp_path: Path) -> None:
    valid_path = tmp_path / "valid.json"
    valid_path.write_text(json.dumps(_ui_workflow()), encoding="utf-8")
    _write_metadata(valid_path)
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("[]", encoding="utf-8")
    _write_metadata(invalid_path)

    with pytest.raises(WorkflowValidationError, match=r"invalid\.json"):
        WorkflowCatalogImporter().scan(tmp_path)


def test_prepare_workflow_appends_fixed_prompts_after_model_prompts(
    tmp_path: Path,
) -> None:
    workflow_path = tmp_path / "SDXL-Lightning-4步文生图.json"
    workflow_path.write_text(
        json.dumps(_ui_workflow(), ensure_ascii=False), encoding="utf-8"
    )
    definition = replace(
        WorkflowCatalogImporter().scan(tmp_path)[0],
        additional_positive_prompt="high detail",
        additional_negative_prompt="blurry",
    )

    prompt = prepare_workflow_prompt(
        definition,
        positive_prompt="portrait",
        negative_prompt="watermark",
        seed=42,
    )

    assert prompt["2"]["inputs"]["text"] == "portrait, high detail"
    assert prompt["3"]["inputs"]["text"] == "watermark, blurry"


@pytest.mark.parametrize(
    ("model_prompt", "fixed_prompt", "expected"),
    [
        ("watermark", "", "watermark"),
        ("", "blurry", "blurry"),
        ("", "", ""),
    ],
)
def test_prepare_workflow_joins_empty_negative_prompts_without_separator(
    tmp_path: Path,
    model_prompt: str,
    fixed_prompt: str,
    expected: str,
) -> None:
    workflow_path = tmp_path / "SDXL-Lightning-4步文生图.json"
    workflow_path.write_text(
        json.dumps(_ui_workflow(), ensure_ascii=False), encoding="utf-8"
    )
    definition = replace(
        WorkflowCatalogImporter().scan(tmp_path)[0],
        additional_negative_prompt=fixed_prompt,
    )

    prompt = prepare_workflow_prompt(
        definition,
        positive_prompt="portrait",
        negative_prompt=model_prompt,
        seed=42,
    )

    assert prompt["3"]["inputs"]["text"] == expected


def test_prepare_workflow_writes_prompt_nodes_and_safe_lightning_defaults(
    tmp_path: Path,
) -> None:
    workflow_path = tmp_path / "SDXL-Lightning-4步文生图.json"
    workflow_path.write_text(
        json.dumps(_ui_workflow(), ensure_ascii=False), encoding="utf-8"
    )
    _write_metadata(workflow_path)
    definition = next(
        record
        for record in WorkflowCatalogImporter().scan(tmp_path)
        if record.workflow_key == "sdxl-lightning-4step"
    )

    prompt = prepare_workflow_prompt(
        definition,
        positive_prompt="blue fox girl",
        negative_prompt="blurry",
        seed=42,
    )

    assert prompt["2"]["inputs"]["text"] == "blue fox girl"
    assert prompt["3"]["inputs"]["text"] == "blurry"
    sampler = prompt["10"]["inputs"]
    assert sampler == {
        **sampler,
        "seed": 42,
        "steps": 4,
        "cfg": 1.0,
        "sampler_name": "euler",
        "scheduler": "sgm_uniform",
        "denoise": 1.0,
    }
    assert prompt["4"]["inputs"]["batch_size"] == 1


def test_prepare_workflow_rejects_non_executable_definition() -> None:
    definition = WorkflowDefinition(
        workflow_key="ui-only",
        display_name="UI only",
        source_file="ui-only.json",
        source_format="ui",
        ui_json="{}",
        api_json=None,
        content_sha256="0" * 64,
    )

    with pytest.raises(WorkflowValidationError, match="not executable"):
        prepare_workflow_prompt(
            definition,
            positive_prompt="valid",
            negative_prompt="",
            seed=1,
        )


def test_text_workflow_rejects_stale_database_bindings(tmp_path: Path) -> None:
    """API 内容更新后不得继续使用指向旧节点的数据库绑定。"""

    workflow_path = tmp_path / "portrait.json"
    workflow_path.write_text(
        json.dumps(_convertible_ui_workflow()), encoding="utf-8"
    )
    definition = WorkflowCatalogImporter().scan(tmp_path)[0]

    with pytest.raises(WorkflowValidationError, match="binding is invalid"):
        validate_workflow_bindings(replace(definition, positive_node_id="999"))
