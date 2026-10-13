"""数据库工作流使用的受控领域模型与磁盘导入器。"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agent_bot.services.workflow_inputs import WorkflowInputImage

MAX_WORKFLOW_FILES = 100
MAX_WORKFLOW_FILE_BYTES = 1_048_576
MAX_WORKFLOW_TOTAL_BYTES = 33_554_432
MAX_PROMPT_CHARS = 2_000
MAX_COMBINED_PROMPT_CHARS = 4_002
_OUTPUT_NODE_TYPES = frozenset({"PreviewImage", "SaveImage", "SaveImageAdvanced"})
_CONTROL_AFTER_GENERATE_VALUES = frozenset(
    {"fixed", "increment", "decrement", "randomize"}
)

WorkflowNode: TypeAlias = dict[str, Any]
ApiWorkflow: TypeAlias = dict[str, WorkflowNode]
WorkflowType: TypeAlias = Literal["text_to_image", "image_to_image", "fixed_reference"]


class WorkflowError(RuntimeError):
    """工作流功能的稳定错误基类。"""


class WorkflowValidationError(WorkflowError):
    """工作流或提示词未通过受控校验。"""


class _ImageWorkflowBindings(BaseModel):
    """管理员显式绑定技术字段，不包含可同步的业务描述或任意文件路径。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    workflow_type: Literal["image_to_image", "fixed_reference"]
    positive_node_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    negative_node_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    output_node_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    seed_node_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    input_image_node_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    positive_input_name: Literal["value"]
    negative_input_name: Literal["negative_prompt"]


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """数据库工作流记录的明确领域契约。"""

    workflow_key: str
    display_name: str
    source_file: str
    source_format: str
    ui_json: str | None
    api_json: str | None
    content_sha256: str
    positive_node_id: str | None = None
    negative_node_id: str | None = None
    output_node_id: str | None = None
    seed_node_id: str | None = None
    enabled: bool = True
    description: str = ""
    additional_positive_prompt: str = ""
    additional_negative_prompt: str = ""
    workflow_type: WorkflowType = "text_to_image"
    input_image_node_id: str | None = None
    positive_input_name: str = "text"
    negative_input_name: str = "text"

    @property
    def execution_sha256(self) -> str:
        """对真正参与执行的 API、绑定和追加词取快照，独立于 UI 去重摘要。"""
        try:
            api = json.loads(self.api_json or "")
        except json.JSONDecodeError as exc:
            raise WorkflowValidationError("stored API workflow is invalid") from exc
        snapshot = (
            self.content_sha256, api, self.workflow_type, self.input_image_node_id,
            self.positive_node_id, self.positive_input_name,
            self.negative_node_id, self.negative_input_name,
            self.output_node_id, self.seed_node_id,
            self.additional_positive_prompt, self.additional_negative_prompt,
        )
        return hashlib.sha256(json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    @property
    def executable(self) -> bool:
        """记录是否具备执行所需的 API JSON 和节点绑定。"""

        return (
            self.workflow_type in {"text_to_image", "image_to_image", "fixed_reference"}
            and (self.workflow_type == "text_to_image" or bool(self.input_image_node_id))
            and self.api_json is not None and all(
            (
                self.positive_node_id,
                self.negative_node_id,
                self.output_node_id,
                self.seed_node_id,
            )
            )
        )


class _UiWorkflowConverter:
    """把唯一生图输出链路的 Comfy 画布转换成 API prompt。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        """读取画布节点；连线在确认是生图工作流后再严格解析。"""

        self._payload = payload
        self._nodes = self._read_nodes(payload.get("nodes"))
        self._links: dict[object, tuple[str, int, str, int, str]] = {}

    def convert(self) -> ApiWorkflow | None:
        """转换唯一图片输出链路；非生图画布返回空。"""

        self._links = self._read_links(self._payload.get("links", []))
        if not any(node["type"] == "KSampler" for node in self._nodes.values()):
            return None
        output_ids = [
            node_id
            for node_id, node in self._nodes.items()
            if node["type"] in _OUTPUT_NODE_TYPES and self._has_linked_input(node)
        ]
        terminal_output_ids = [
            output_id
            for output_id in output_ids
            if not any(
                output_id in self._upstream_node_ids(other_id)
                for other_id in output_ids
                if other_id != output_id
            )
        ]
        if len(terminal_output_ids) != 1:
            raise WorkflowValidationError("UI workflow output binding is ambiguous")
        required_ids = self._upstream_node_ids(terminal_output_ids[0])
        sampler_ids = [
            node_id
            for node_id in required_ids
            if self._nodes[node_id]["type"] == "KSampler"
        ]
        if len(sampler_ids) != 1:
            raise WorkflowValidationError("UI workflow sampler binding is ambiguous")
        converted = {
            node_id: self._convert_node(node_id)
            for node_id in self._nodes
            if node_id in required_ids
        }
        return _validate_api_workflow(converted)

    def validate_pair(self, workflow: ApiWorkflow) -> None:
        """显式 API 配对逐节点核对控件及连线，不丢弃预览和 PE 节点。"""
        self._links = self._read_links(self._payload.get("links", []))
        for node_id, api_node in workflow.items():
            node = self._nodes.get(node_id)
            if node is None or node.get("mode", 0) != 0:
                raise WorkflowValidationError("UI/API workflow nodes differ")
            converted = self._convert_node(node_id)
            if converted["class_type"] != api_node["class_type"] or (
                converted["inputs"] != api_node["inputs"]
            ):
                raise WorkflowValidationError("UI/API workflow inputs differ")
        if any(
            node_id not in workflow and node["type"] not in {"Note", "MarkdownNote"}
            for node_id, node in self._nodes.items()
        ):
            raise WorkflowValidationError("UI/API workflow nodes differ")

    def _read_nodes(self, value: object) -> dict[str, dict[str, Any]]:
        """校验画布节点的 ID、类型和唯一性。"""

        if not isinstance(value, list) or len(value) > 200:
            raise WorkflowValidationError("UI workflow nodes are invalid")
        nodes: dict[str, dict[str, Any]] = {}
        for item in value:
            if not isinstance(item, dict):
                raise WorkflowValidationError("UI workflow node is invalid")
            node_id = self._node_id(item.get("id"))
            node_type = item.get("type")
            if (
                node_id in nodes
                or not isinstance(node_type, str)
                or not node_type
                or len(node_type) > 128
            ):
                raise WorkflowValidationError("UI workflow node is invalid")
            nodes[node_id] = cast(dict[str, Any], item)
        return nodes

    def _read_links(
        self, value: object
    ) -> dict[object, tuple[str, int, str, int, str]]:
        """把画布 links 数组索引为输入可验证的连接表。"""

        if not isinstance(value, list) or len(value) > 1_000:
            raise WorkflowValidationError("UI workflow links are invalid")
        links: dict[object, tuple[str, int, str, int, str]] = {}
        for item in value:
            if (
                not isinstance(item, list)
                or len(item) < 6
                or not isinstance(item[2], int)
                or isinstance(item[2], bool)
                or item[2] < 0
                or not isinstance(item[4], int)
                or isinstance(item[4], bool)
                or item[4] < 0
                or not isinstance(item[5], str)
                or not item[5]
                or len(item[5]) > 128
            ):
                raise WorkflowValidationError("UI workflow link is invalid")
            link_id = item[0]
            origin_id = self._node_id(item[1])
            target_id = self._node_id(item[3])
            if (
                not isinstance(link_id, (int, str))
                or isinstance(link_id, bool)
                or link_id in links
                or origin_id not in self._nodes
                or target_id not in self._nodes
            ):
                raise WorkflowValidationError("UI workflow link is invalid")
            links[link_id] = (origin_id, item[2], target_id, item[4], item[5])
        for link_id, link in links.items():
            self._validate_link(link_id, link)
        self._validate_input_links(links)
        return links

    def _node_id(self, value: object) -> str:
        """把画布整数或安全字符串节点 ID 规范化为 API 字符串。"""

        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return str(value)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
            return value
        raise WorkflowValidationError("UI workflow node ID is invalid")

    def _has_linked_input(self, node: dict[str, Any]) -> bool:
        """判断输出节点是否实际连接到上游图片。"""

        inputs = node.get("inputs")
        return isinstance(inputs, list) and any(
            isinstance(item, dict)
            and item.get("name") == "images"
            and item.get("type") == "IMAGE"
            and item.get("link") is not None
            for item in inputs
        )

    def _validate_link(
        self,
        link_id: object,
        link: tuple[str, int, str, int, str],
    ) -> None:
        """校验连接两端 slot、目标引用和端口类型一致。"""

        origin_id, origin_slot, target_id, target_slot, link_type = link
        outputs = self._node_outputs(self._nodes[origin_id])
        inputs = self._node_inputs(self._nodes[target_id])
        if origin_slot >= len(outputs) or target_slot >= len(inputs):
            raise WorkflowValidationError("UI workflow link is invalid")
        output = outputs[origin_slot]
        target = inputs[target_slot]
        output_type = output.get("type")
        target_type = target.get("type")
        if target.get("link") != link_id or not self._compatible_link_types(
            output_type,
            target_type,
            link_type,
        ):
            raise WorkflowValidationError("UI workflow link is invalid")

    def _compatible_link_types(
        self,
        output_type: object,
        target_type: object,
        link_type: str,
    ) -> bool:
        """忽略星号通配声明，并要求所有具体端口类型一致。"""

        if not isinstance(output_type, str) or not isinstance(target_type, str):
            return False
        concrete_types = {
            declared_type
            for declared_type in (output_type, target_type, link_type)
            if declared_type != "*"
        }
        return len(concrete_types) <= 1

    def _validate_input_links(
        self,
        links: dict[object, tuple[str, int, str, int, str]],
    ) -> None:
        """确认每个节点输入引用都存在，并准确指向该输入槽位。"""

        for node_id, node in self._nodes.items():
            for target_slot, descriptor in enumerate(self._node_inputs(node)):
                link_id = descriptor.get("link")
                if link_id is None:
                    continue
                if (
                    not isinstance(link_id, (int, str))
                    or isinstance(link_id, bool)
                    or link_id not in links
                ):
                    raise WorkflowValidationError("UI workflow link is missing")
                _, _, target_id, linked_slot, _ = links[link_id]
                if target_id != node_id or linked_slot != target_slot:
                    raise WorkflowValidationError("UI workflow link target is invalid")

    def _node_outputs(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        """读取源节点输出描述列表。"""

        value = node.get("outputs", [])
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise WorkflowValidationError("UI workflow outputs are invalid")
        return cast(list[dict[str, Any]], value)

    def _upstream_node_ids(self, output_id: str) -> set[str]:
        """从唯一输出向前遍历并校验每条被使用的连接。"""

        required: set[str] = set()
        pending = [output_id]
        while pending:
            node_id = pending.pop()
            if node_id in required:
                continue
            node = self._nodes[node_id]
            if node.get("mode", 0) != 0:
                raise WorkflowValidationError("UI workflow contains inactive linked node")
            required.add(node_id)
            for index, item in enumerate(self._node_inputs(node)):
                link_id = item.get("link")
                if link_id is None:
                    continue
                origin_id, _, target_id, target_slot, _ = self._linked_input(link_id)
                if target_id != node_id or target_slot != index:
                    raise WorkflowValidationError("UI workflow link target is invalid")
                pending.append(origin_id)
        return required

    def _convert_node(self, node_id: str) -> WorkflowNode:
        """把单个可达画布节点的连接和控件值转换成 API inputs。"""

        node = self._nodes[node_id]
        descriptors = self._node_inputs(node)
        widget_descriptors = [item for item in descriptors if item.get("widget") is not None]
        widget_values = self._widget_values(node, len(widget_descriptors))
        widget_index = 0
        inputs: dict[str, Any] = {}
        for descriptor in descriptors:
            name = descriptor.get("name")
            if not isinstance(name, str) or not name or len(name) > 128:
                raise WorkflowValidationError("UI workflow input is invalid")
            has_widget = descriptor.get("widget") is not None
            widget_value: Any = None
            if has_widget:
                widget_value = widget_values[widget_index]
                widget_index += 1
            link_id = descriptor.get("link")
            if link_id is not None:
                origin_id, origin_slot, target_id, _, _ = self._linked_input(link_id)
                if target_id != node_id:
                    raise WorkflowValidationError("UI workflow link target is invalid")
                inputs[name] = [origin_id, origin_slot]
            elif has_widget:
                inputs[name] = widget_value
        return {"class_type": node["type"], "inputs": inputs}

    def _node_inputs(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        """读取节点输入描述列表。"""

        value = node.get("inputs", [])
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise WorkflowValidationError("UI workflow inputs are invalid")
        return cast(list[dict[str, Any]], value)

    def _widget_values(self, node: dict[str, Any], expected: int) -> list[Any]:
        """校验控件值数量，并移除 KSampler 的前端随机策略字段。"""

        value = node.get("widgets_values", [])
        if not isinstance(value, list):
            raise WorkflowValidationError("UI workflow widget values are invalid")
        values = list(value)
        if node["type"] == "LoadImage" and len(values) == expected + 1 and values[-1] == "image":
            values.pop()
        if (
            node["type"] == "KSampler"
            and len(values) == expected + 1
            and len(values) > 1
            and values[1] in _CONTROL_AFTER_GENERATE_VALUES
        ):
            values.pop(1)
        if len(values) != expected:
            raise WorkflowValidationError("UI workflow widget values are ambiguous")
        return values

    def _linked_input(self, link_id: object) -> tuple[str, int, str, int, str]:
        """读取输入引用的连接，不接受孤立 link ID。"""

        try:
            return self._links[link_id]
        except (KeyError, TypeError) as exc:
            raise WorkflowValidationError("UI workflow link is missing") from exc


class WorkflowCatalogImporter:
    """有界扫描管理员目录并构造不丢失原始 JSON 的记录。"""

    def scan(self, directory: Path) -> tuple[WorkflowDefinition, ...]:
        """读取目录中的全部 JSON；任一非法文件都会使整次导入失败。"""

        if not directory.is_dir():
            raise WorkflowValidationError("workflow directory is unavailable")
        json_paths = sorted(
            directory.glob("*.json"),
            key=lambda item: (item.name.casefold(), item.name),
        )
        workflow_paths = [
            path
            for path in json_paths
            if not path.name.casefold().endswith(".meta.json")
        ]
        if len(workflow_paths) > MAX_WORKFLOW_FILES:
            raise WorkflowValidationError("workflow directory contains too many files")

        total_bytes = 0
        records: list[WorkflowDefinition] = []
        seen_keys: set[str] = set()
        seen_hashes: set[str] = set()
        for path in workflow_paths:
            size = path.stat().st_size
            total_bytes += size
            if size <= 0 or size > MAX_WORKFLOW_FILE_BYTES:
                raise WorkflowValidationError(f"workflow file is invalid: {path.name}")
            if total_bytes > MAX_WORKFLOW_TOTAL_BYTES:
                raise WorkflowValidationError("workflow directory exceeds total size limit")
            record = self._read_file(path)
            if record.content_sha256 in seen_hashes:
                continue
            if record.workflow_key in seen_keys:
                raise WorkflowValidationError("workflow keys are not unique")
            seen_keys.add(record.workflow_key)
            seen_hashes.add(record.content_sha256)
            records.append(record)
        return tuple(records)

    def _read_file(self, path: Path) -> WorkflowDefinition:
        """校验单个 UTF-8 JSON 对象并识别 UI/API 格式。"""

        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowValidationError(f"workflow file is invalid: {path.name}") from exc
        if not isinstance(payload, dict):
            raise WorkflowValidationError(f"workflow file is invalid: {path.name}")

        canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        workflow_key = _workflow_key(path.stem, digest)
        meta_path = path.with_name(f"{path.stem}.meta.json")
        if meta_path.exists():
            metadata = self._read_companion(meta_path, path.parent)
            if "workflow_bindings" in metadata:
                try:
                    binding = _ImageWorkflowBindings.model_validate(metadata["workflow_bindings"])
                except ValidationError as exc:
                    raise WorkflowValidationError("image workflow bindings are invalid") from exc
                if not _is_ui_workflow(payload):
                    raise WorkflowValidationError("image workflow requires a UI/API pair")
                api = _validate_api_workflow(self._read_companion(
                    path.parent / "api" / f"{path.stem}-api.json", path.parent,
                ))
                _UiWorkflowConverter(payload).validate_pair(api)
                definition = WorkflowDefinition(
                    workflow_key=workflow_key, display_name=path.stem, source_file=path.name,
                    source_format="ui", ui_json=canonical,
                    api_json=json.dumps(api, ensure_ascii=False, separators=(",", ":")),
                    content_sha256=digest, enabled=False, **binding.model_dump(),
                )
                validate_workflow_bindings(definition)
                return definition
        if _is_ui_workflow(payload):
            api_json = None
            bindings = (None, None, None, None)
            if workflow_key == "sdxl-lightning-4step":
                api_json = _load_sdxl_api_template()
                bindings = ("2", "3", "7", "10")
            else:
                workflow = _UiWorkflowConverter(payload).convert()
                if workflow is not None:
                    api_json = json.dumps(
                        workflow, ensure_ascii=False, separators=(",", ":")
                    )
                    bindings = _infer_bindings(workflow)
            return WorkflowDefinition(
                workflow_key=workflow_key,
                display_name=path.stem,
                source_file=path.name,
                source_format="ui",
                ui_json=canonical,
                api_json=api_json,
                content_sha256=digest,
                positive_node_id=bindings[0],
                negative_node_id=bindings[1],
                output_node_id=bindings[2],
                seed_node_id=bindings[3],
            )

        workflow = _validate_api_workflow(payload)
        positive_id, negative_id, output_id, seed_id = _infer_bindings(workflow)
        return WorkflowDefinition(
            workflow_key=workflow_key,
            display_name=path.stem,
            source_file=path.name,
            source_format="api",
            ui_json=None,
            api_json=canonical,
            content_sha256=digest,
            positive_node_id=positive_id,
            negative_node_id=negative_id,
            output_node_id=output_id,
            seed_node_id=seed_id,
        )

    def _read_companion(self, path: Path, root: Path) -> dict[str, Any]:
        """配对文件只能位于同一目录树内，读取前限制字节数。"""
        try:
            if not path.resolve().is_relative_to(root.resolve()):
                raise WorkflowValidationError("workflow companion path is invalid")
            if not 0 < path.stat().st_size <= MAX_WORKFLOW_FILE_BYTES:
                raise WorkflowValidationError("workflow companion size is invalid")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkflowValidationError("workflow companion is unavailable or invalid") from exc
        if not isinstance(payload, dict):
            raise WorkflowValidationError("workflow companion is invalid")
        return payload


def prepare_workflow_prompt(
    definition: WorkflowDefinition,
    *,
    positive_prompt: str,
    negative_prompt: str,
    seed: int,
    input_image: WorkflowInputImage | None = None,
) -> ApiWorkflow:
    """从数据库 API JSON 深复制并只修改已绑定的正负提示词和种子。"""

    if not definition.executable or definition.api_json is None:
        raise WorkflowValidationError("workflow is not executable")
    validate_workflow_bindings(definition)
    if (definition.workflow_type == "image_to_image") != (input_image is not None):
        raise WorkflowValidationError("workflow input image is required only for image editing")
    positive = _join_workflow_prompt(
        _validate_prompt_text(positive_prompt, required=True),
        definition.additional_positive_prompt,
    )
    negative = _join_workflow_prompt(
        _validate_prompt_text(negative_prompt, required=False),
        definition.additional_negative_prompt,
    )
    if not 0 <= seed <= 2**63 - 1:
        raise WorkflowValidationError("workflow seed is invalid")
    try:
        decoded = json.loads(definition.api_json)
    except json.JSONDecodeError as exc:
        raise WorkflowValidationError("stored API workflow is invalid") from exc
    workflow = copy.deepcopy(_validate_api_workflow(decoded))
    assert definition.positive_node_id is not None
    assert definition.negative_node_id is not None
    assert definition.seed_node_id is not None
    try:
        workflow[definition.positive_node_id]["inputs"][definition.positive_input_name] = positive
        workflow[definition.negative_node_id]["inputs"][definition.negative_input_name] = negative
        workflow[definition.seed_node_id]["inputs"]["seed"] = seed
        if input_image is not None:
            assert definition.input_image_node_id is not None
            workflow[definition.input_image_node_id]["inputs"]["image"] = input_image.comfy_path
    except (KeyError, TypeError) as exc:
        raise WorkflowValidationError("stored workflow bindings are invalid") from exc
    return workflow


def validate_workflow_bindings(definition: WorkflowDefinition) -> None:
    """校验完整编辑链；固定底图兼容已迁入的旧目录，仍只允许摘要文件名。"""
    if not definition.executable:
        raise WorkflowValidationError("workflow bindings are incomplete")
    if definition.workflow_type == "text_to_image":
        if (definition.positive_input_name, definition.negative_input_name) != ("text", "text"):
            raise WorkflowValidationError("text workflow fields are invalid")
        if definition.input_image_node_id is not None:
            raise WorkflowValidationError("text workflow cannot bind an input image")
        try:
            workflow = _validate_api_workflow(json.loads(definition.api_json or ""))
            inferred = _infer_bindings(workflow)
            configured = (
                definition.positive_node_id,
                definition.negative_node_id,
                definition.output_node_id,
                definition.seed_node_id,
            )
            if inferred != configured:
                raise WorkflowValidationError("text workflow binding is invalid")
            for node_id in (definition.positive_node_id, definition.negative_node_id):
                if not isinstance(workflow[node_id]["inputs"].get("text"), str):
                    raise WorkflowValidationError("text workflow binding is invalid")
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise WorkflowValidationError("text workflow bindings are invalid") from exc
        return
    try:
        workflow = _validate_api_workflow(json.loads(definition.api_json or ""))
        _validate_acyclic_dependencies(workflow)
        bindings = (
            (definition.positive_node_id, "PrimitiveStringMultiline", "value"),
            (definition.negative_node_id, "TextEncodeQwenImage21", "negative_prompt"),
            (definition.input_image_node_id, "LoadImage", "image"),
        )
        if (definition.positive_input_name, definition.negative_input_name) != (
            "value", "negative_prompt",
        ):
            raise WorkflowValidationError("image workflow fields are invalid")
        for node_id, class_type, field in bindings:
            node = workflow[node_id]
            if node["class_type"] != class_type or not isinstance(node["inputs"].get(field), str):
                raise WorkflowValidationError("image workflow binding is invalid")
        if definition.workflow_type == "fixed_reference" and not re.fullmatch(
            r"(?:chat-reference|dispatch-chat-reference)/[a-f0-9]{64}\.(png|jpg|webp)",
            workflow[definition.input_image_node_id]["inputs"]["image"],
        ):
            raise WorkflowValidationError("fixed reference path is invalid")
        if workflow[definition.output_node_id]["class_type"] not in _OUTPUT_NODE_TYPES:
            raise WorkflowValidationError("image workflow output is invalid")
        for class_type, expected in (
            ("LoadImage", definition.input_image_node_id),
            ("KSampler", definition.seed_node_id),
        ):
            if [key for key, node in workflow.items() if node["class_type"] == class_type] != [
                expected,
            ]:
                raise WorkflowValidationError("image workflow nodes are ambiguous")
        outputs = [
            key for key, node in workflow.items() if node["class_type"] in _OUTPUT_NODE_TYPES
        ]
        if outputs != [definition.output_node_id]:
            raise WorkflowValidationError("image workflow outputs are ambiguous")
        reachable = _api_upstream_node_ids(workflow, definition.output_node_id)
        if not {definition.positive_node_id, definition.negative_node_id,
                definition.input_image_node_id, definition.seed_node_id}.issubset(reachable):
            raise WorkflowValidationError("image workflow bindings are disconnected")
        encoder = workflow[definition.negative_node_id]["inputs"]
        image_link = [definition.input_image_node_id, 0]
        if encoder.get("images.image_1") != image_link or any(
            name.startswith("images.") and name != "images.image_1" for name in encoder
        ):
            raise WorkflowValidationError("image workflow reference is ambiguous")
        sampler = workflow[definition.seed_node_id]["inputs"]
        for field, slot in (("positive", 0), ("negative", 1), ("latent_image", 2)):
            if sampler.get(field) != [definition.negative_node_id, slot]:
                raise WorkflowValidationError("image workflow conditioning is invalid")
        generators = [key for key, node in workflow.items() if node["class_type"] == "TextGenerate"]
        if definition.workflow_type == "fixed_reference":
            # 固定角色使用 Chat LLM 的编辑指令，不再加载 PE 模型重复增强。
            if generators or encoder.get("prompt") != [definition.positive_node_id, 0]:
                raise WorkflowValidationError("fixed reference instruction is invalid")
            return
        if len(generators) != 1:
            raise WorkflowValidationError("image workflow enhancer reference is invalid")
        enhancer_id = generators[0]
        enhancer = workflow[enhancer_id]["inputs"]
        instruction = enhancer.get("prompt")
        if (
            enhancer.get("image") != image_link or not _is_api_link(instruction)
            or definition.positive_node_id not in _api_upstream_node_ids(
                workflow, _linked_node_id(instruction),
            )
        ):
            raise WorkflowValidationError("image workflow enhancer instruction is invalid")
        prompt_link = encoder.get("prompt")
        if not _is_api_link(prompt_link) or prompt_link[1] != 0:
            raise WorkflowValidationError("image workflow enhanced prompt is invalid")
        extractor = workflow[_linked_node_id(prompt_link)]
        if (
            extractor["class_type"] != "JsonExtractString"
            or extractor["inputs"].get("json_string") != [enhancer_id, 0]
            or extractor["inputs"].get("key") != "rewritten_prompt"
        ):
            raise WorkflowValidationError("image workflow enhanced prompt is invalid")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise WorkflowValidationError("image workflow bindings are invalid") from exc


def _join_workflow_prompt(model_prompt: str, fixed_prompt: str) -> str:
    """按模型在前、管理员固定内容在后的顺序合并提示词。"""

    combined = ", ".join(part for part in (model_prompt, fixed_prompt) if part)
    if len(combined) > MAX_COMBINED_PROMPT_CHARS:
        raise WorkflowValidationError("combined workflow prompt is too long")
    return combined


def _validate_prompt_text(value: str, *, required: bool) -> str:
    """规范化工具提示词并拒绝控制字符与越界内容。"""

    normalized = unicodedata.normalize("NFKC", value).strip()
    if required and not normalized:
        raise WorkflowValidationError("positive prompt is required")
    if len(normalized) > MAX_PROMPT_CHARS or _has_invalid_control(normalized):
        raise WorkflowValidationError("workflow prompt is invalid")
    return normalized


def _has_invalid_control(value: str) -> bool:
    """判断文本是否包含提示词不接受的控制或代理字符。"""

    return any(
        unicodedata.category(character) in {"Cc", "Cs"}
        and character not in {"\n", "\r", "\t"}
        for character in value
    )


def _is_ui_workflow(payload: dict[str, Any]) -> bool:
    """识别 Comfy 画布格式，避免把它误当作 API prompt。"""

    return isinstance(payload.get("nodes"), list)


def _validate_api_workflow(payload: object) -> ApiWorkflow:
    """验证 API 工作流是以节点 ID 为键的 class_type/inputs 对象。"""

    if not isinstance(payload, dict) or not payload or len(payload) > 200:
        raise WorkflowValidationError("API workflow is invalid")
    workflow = cast(dict[object, object], payload)
    validated: ApiWorkflow = {}
    for node_id, node in workflow.items():
        if not isinstance(node_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", node_id):
            raise WorkflowValidationError("API workflow node ID is invalid")
        if not isinstance(node, dict):
            raise WorkflowValidationError("API workflow node is invalid")
        class_type = node.get("class_type")
        inputs = node.get("inputs")
        if (
            not isinstance(class_type, str)
            or not class_type
            or len(class_type) > 128
            or not isinstance(inputs, dict)
        ):
            raise WorkflowValidationError("API workflow node is invalid")
        validated[node_id] = cast(WorkflowNode, node)
    return validated


def _infer_bindings(workflow: ApiWorkflow) -> tuple[str, str, str, str]:
    """通过 KSampler 的显式链接推断提示词、种子与单一输出节点。"""

    _validate_acyclic_dependencies(workflow)
    samplers = [
        node_id
        for node_id, node in workflow.items()
        if node.get("class_type") == "KSampler"
    ]
    outputs = [
        node_id
        for node_id, node in workflow.items()
        if node.get("class_type") in _OUTPUT_NODE_TYPES
    ]
    terminal_outputs = [
        output_id
        for output_id in outputs
        if not any(
            output_id in _api_upstream_node_ids(workflow, other_id)
            for other_id in outputs
            if other_id != output_id
        )
    ]
    if len(samplers) != 1 or len(terminal_outputs) != 1:
        raise WorkflowValidationError("API workflow bindings are ambiguous")
    sampler_id = samplers[0]
    inputs = workflow[sampler_id]["inputs"]
    for input_name in ("model", "positive", "negative", "latent_image"):
        try:
            source_id = _linked_node_id(inputs.get(input_name))
        except WorkflowValidationError as exc:
            raise WorkflowValidationError("API workflow sampler input is invalid") from exc
        if source_id not in workflow:
            raise WorkflowValidationError("API workflow sampler input is invalid")
    seed = inputs.get("seed")
    if (
        not isinstance(seed, int)
        or isinstance(seed, bool)
        or not 0 <= seed <= 2**64 - 1
    ):
        raise WorkflowValidationError("API workflow sampler seed is invalid")
    positive_id = _find_prompt_node_id(workflow, inputs.get("positive"), "positive")
    negative_id = _find_prompt_node_id(workflow, inputs.get("negative"), "negative")
    if positive_id == negative_id:
        raise WorkflowValidationError("API workflow prompt bindings are ambiguous")
    return positive_id, negative_id, terminal_outputs[0], sampler_id


def _api_upstream_node_ids(workflow: ApiWorkflow, node_id: str) -> set[str]:
    """收集 API 节点的所有上游依赖，供末端输出判定使用。"""

    upstream: set[str] = set()
    pending = [node_id]
    while pending:
        current_id = pending.pop()
        if current_id in upstream:
            continue
        upstream.add(current_id)
        for value in workflow[current_id]["inputs"].values():
            if _is_api_link(value):
                pending.append(_linked_node_id(value))
    return upstream


def _validate_acyclic_dependencies(workflow: ApiWorkflow) -> None:
    """遍历 API 节点依赖图，拒绝缺失来源和有向依赖环。"""

    states: dict[str, int] = {}
    for root_id in workflow:
        stack: list[tuple[str, bool]] = [(root_id, False)]
        while stack:
            node_id, expanded = stack.pop()
            if expanded:
                states[node_id] = 2
                continue
            state = states.get(node_id, 0)
            if state == 2:
                continue
            if state == 1:
                raise WorkflowValidationError("API workflow dependency cycle is invalid")
            node = workflow.get(node_id)
            if node is None:
                raise WorkflowValidationError("API workflow link is invalid")
            states[node_id] = 1
            stack.append((node_id, True))
            for value in node["inputs"].values():
                if _is_api_link(value):
                    stack.append((_linked_node_id(value), False))


def _find_prompt_node_id(
    workflow: ApiWorkflow,
    binding: object,
    role: str,
) -> str:
    """沿 conditioning 输入向上寻找唯一 CLIPTextEncode 节点。"""

    pending = [_linked_node_id(binding)]
    visited: set[str] = set()
    prompt_ids: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id in visited:
            continue
        visited.add(node_id)
        node = workflow.get(node_id)
        if node is None:
            raise WorkflowValidationError(f"API workflow {role} binding is invalid")
        if node.get("class_type") == "CLIPTextEncode":
            prompt_ids.add(node_id)
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            raise WorkflowValidationError(f"API workflow {role} binding is invalid")
        for value in inputs.values():
            if _is_api_link(value):
                pending.append(cast(list[object], value)[0])
    if len(prompt_ids) != 1:
        raise WorkflowValidationError(f"API workflow {role} binding is invalid")
    return next(iter(prompt_ids))


def _is_api_link(value: object) -> bool:
    """判断 API input 值是否是节点输出引用。"""

    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value[0]) is not None
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
        and value[1] >= 0
    )


def _linked_node_id(value: object) -> str:
    """读取 Comfy 输入链接的来源节点 ID。"""

    if not _is_api_link(value):
        raise WorkflowValidationError("API workflow link is invalid")
    return cast(list[str], value)[0]


def _workflow_key(stem: str, digest: str) -> str:
    """为文件生成有界稳定 key，并固定识别已确认的 SDXL 工作流。"""

    normalized = unicodedata.normalize("NFKC", stem).casefold()
    if normalized.startswith("sdxl-lightning-4"):
        return "sdxl-lightning-4step"
    key = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return key[:128] if key else f"workflow-{digest[:12]}"


def _load_sdxl_api_template() -> str:
    """读取随包发布且经仓库评审的 SDXL API 工作流模板。"""

    resource = files("agent_bot").joinpath("resources/sdxl_lightning_4step_api.json")
    raw = resource.read_text(encoding="utf-8")
    _validate_api_workflow(json.loads(raw))
    return raw
