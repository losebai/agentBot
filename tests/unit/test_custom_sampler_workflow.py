"""自定义采样保留管理员调度，并将请求种子写入实际噪声字段。"""

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from test_fixed_reference_workflow import REFERENCE, fixed_definition

from agent_bot.services.workflows import (
    WorkflowCatalogImporter,
    WorkflowDefinition,
    WorkflowValidationError,
    prepare_workflow_prompt,
    validate_workflow_bindings,
)


def custom_definition() -> WorkflowDefinition:
    """提供带手动调度的固定底图，避免真实 ComfyUI 和数据库调用。"""
    definition = fixed_definition()
    api = json.loads(definition.api_json)
    api['14'] = {'class_type': 'SamplerCustom', 'inputs': {
        'model': ['13', 0], 'add_noise': True, 'noise_seed': 7, 'cfg': 1,
        'positive': ['12', 0], 'negative': ['12', 1], 'latent_image': ['12', 2],
        'sampler': ['20', 0], 'sigmas': ['21', 0],
    }}
    api['20'] = {'class_type': 'KSamplerSelect', 'inputs': {'sampler_name': 'euler'}}
    api['21'] = {'class_type': 'ManualSigmas', 'inputs': {'sigmas': '1, 0.7, 0.4, 0'}}
    return replace(definition, api_json=json.dumps(api))


def test_custom_sampler_writes_request_noise_seed_and_preserves_schedule() -> None:
    """若误写 seed 而非 noise_seed，生产任务会复用模板种子而非请求种子。"""
    definition = custom_definition()
    prompt = prepare_workflow_prompt(
        definition, positive_prompt='自然自拍', negative_prompt='', seed=42,
    )
    assert prompt['14']['inputs']['noise_seed'] == 42
    assert 'seed' not in prompt['14']['inputs']
    assert prompt['21']['inputs']['sigmas'] == '1, 0.7, 0.4, 0'
    assert prompt['1']['inputs']['image'] == REFERENCE
    assert prompt['2']['inputs']['value'] == '自然自拍'
    assert json.loads(definition.api_json)['14']['inputs']['noise_seed'] == 7


def test_custom_sampler_rejects_duplicate_sampling_chain() -> None:
    """新增采样协议不能绕过单一采样节点约束。"""
    definition = custom_definition()
    api = json.loads(definition.api_json)
    api['22'] = json.loads(fixed_definition().api_json)['14']
    with pytest.raises(WorkflowValidationError, match='ambiguous'):
        validate_workflow_bindings(replace(definition, api_json=json.dumps(api)))


def test_importer_accepts_custom_sampler_randomize_control(tmp_path: Path) -> None:
    """真实画布在噪声种子后携带随机策略，导入不能把它当作 cfg。"""
    definition = custom_definition()
    api = json.loads(definition.api_json)
    nodes, links = [], []
    for node_id, node in api.items():
        ports, widgets = [], []
        for field, value in node['inputs'].items():
            port = {'name': field, 'type': '*', 'link': None}
            if isinstance(value, list):
                link_id = len(links) + 1
                port['link'] = link_id
                links.append([link_id, int(value[0]), value[1], int(node_id), len(ports), '*'])
            else:
                port['widget'] = {'name': field}
                widgets.append(value)
                if node['class_type'] == 'SamplerCustom' and field == 'noise_seed':
                    widgets.append('randomize')
            ports.append(port)
        nodes.append({'id': int(node_id), 'type': node['class_type'], 'inputs': ports,
                      'outputs': [{'type': '*'}] * 3, 'widgets_values': widgets})
    (tmp_path / 'fixed.json').write_text(json.dumps({'nodes': nodes, 'links': links}),
                                       encoding='utf-8')
    (tmp_path / 'api').mkdir()
    (tmp_path / 'api/fixed-api.json').write_text(definition.api_json, encoding='utf-8')
    fields = ('workflow_type', 'positive_node_id', 'negative_node_id', 'output_node_id',
              'seed_node_id', 'input_image_node_id', 'positive_input_name', 'negative_input_name')
    (tmp_path / 'fixed.meta.json').write_text(json.dumps({
        'workflow_bindings': {name: asdict(definition)[name] for name in fields},
    }), encoding='utf-8')
    (imported,) = WorkflowCatalogImporter().scan(tmp_path)
    prompt = prepare_workflow_prompt(
        imported, positive_prompt='窗边自拍', negative_prompt='', seed=99,
    )
    assert prompt['14']['inputs']['noise_seed'] == 99
    assert prompt['14']['inputs']['cfg'] == 1
    assert prompt['21']['inputs']['sigmas'] == '1, 0.7, 0.4, 0'
