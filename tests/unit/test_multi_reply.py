"""验证 QQ 多条回复的严格协议及受信任提示词。"""

import pytest
from pydantic import ValidationError

from agent_bot.ai.chat_errors import ChatAiProtocolError
from agent_bot.config import RuntimeConfig


def make_config(**values):
    """提供独立测试密钥，不读取本地启动文件。"""
    return RuntimeConfig(
        onebot_v11_access_token="test-onebot-access-token-with-enough-entropy",
        chat_session_secret="test-session-secret-with-enough-entropy", **values,
    )


def test_multi_reply_decodes_only_nonempty_string_messages():
    """模型给定的消息边界与代码块原样保留。"""
    from agent_bot.services.multi_reply import parse_multi_reply

    assert parse_multi_reply('["第一条", "```py\\nx = 1\\n```"]', 3) == (
        "第一条", "```py\nx = 1\n```",
    )


def test_multi_reply_cleans_internal_at_markers_before_building_delivery_parts() -> None:
    """分条正文与持久化正文必须使用相同的清理结果，不能在实际发送时才改写。"""
    from agent_bot.services.multi_reply import parse_multi_reply

    assert parse_multi_reply('["[AT 3616076808] 第一条", "第二条"]', 3) == ("第一条", "第二条")
    with pytest.raises(ChatAiProtocolError):
        parse_multi_reply('["[AT 3616076808]"]', 3)


@pytest.mark.parametrize(("text", "expected"), [
    ('["你好", "呀"]', "你好呀"),
    ('["第一行\\n", "第二行"]', "第一行\n第二行"),
    ('["只有一条"]', "只有一条"),
    ('"第一行\\n第二行"', "第一行\n第二行"),
    ('“真嘟，我刚看的就是群里的最新消息。”', '真嘟，我刚看的就是群里的最新消息。'),
    ('  “第一行\n第二行”  ', '第一行\n第二行'),
    ('{"参数不清楚就先别冲，具体型号、显卡功耗和实测散热出来再说。"}',
     '参数不清楚就先别冲，具体型号、显卡功耗和实测散热出来再说。'),
    (' { "第一行\\n第二行" } ', '第一行\n第二行'),
    ('{“正文”}', '正文'),
    ('{""}', ''),
    (r'{"路径 C:\\new\\notes；保留字面 \\n"}', r'路径 C:\new\notes；保留字面 \n'),
    (r'{"保留 {name} 和 \"引用\""}', '保留 {name} 和 "引用"'),
    ('{"甲","乙"}', '{"甲","乙"}'),
    ('{"未闭合}', '{"未闭合}'),
    ('{123}', '{123}'),
    ('{["正文"]}', '{["正文"]}'),
    ('{}', '{}'),
    ('{"key":"value"}', '{"key":"value"}'),
    ('示例：{"正文"}', '示例：{"正文"}'),
    ('```python\n{"正文"}\n```', '```python\n{"正文"}\n```'),
    ('“”', ''),
    ('“   ”', '   '),
    ('他说“你好”。', '他说“你好”。'),
    ('“甲”与“乙”', '“甲”与“乙”'),
    ('“未闭合', '“未闭合'),
    ('只有结尾”', '只有结尾”'),
    ('["“保留正文引用”"]', '“保留正文引用”'),
    ('{"消息内容":"92都快九块了，加满一箱心都在滴血，95这是要奔十去啊\uff08笑\uff09"}',
     "92都快九块了，加满一箱心都在滴血，95这是要奔十去啊\uff08笑\uff09"),
    (' { "消息内容": "第一行\\n第二行" } ', "第一行\n第二行"),
    (r'{"消息内容":"路径 C:\\new\\notes；保留字面 \\n"}', r'路径 C:\new\notes；保留字面 \n'),
    (r'{"消息内容":"{\"消息内容\":\"只解码一层\"}"}', '{"消息内容":"只解码一层"}'),
    ('{"消息内容":""}', ""),
    (r'"路径 C:\\new\\notes；保留字面 \\n"', r'路径 C:\new\notes；保留字面 \n'),
    ('[]', ""),
    ('普通正文', '普通正文'),
    (r'普通正文\n下一行', r'普通正文\n下一行'),
    ('["尚未闭合"', '["尚未闭合"'),
    ('["文字", 123]', '["文字", 123]'),
    ('{"messages": ["内容"]}', '{"messages": ["内容"]}'),
    ('{"消息内容":"正文","状态":"成功"}', '{"消息内容":"正文","状态":"成功"}'),
    ('{"消息内容":["正文"]}', '{"消息内容":["正文"]}'),
    ('{"消息内容":null}', '{"消息内容":null}'),
    ('{"消息内容":123}', '{"消息内容":123}'),
    ('[{"消息内容":"正文"}]', '[{"消息内容":"正文"}]'),
])
def test_disabled_multi_reply_decodes_message_json_only(text, expected):
    """只移除已确认的单层包装，正文引用、其他对象及字面反斜杠保持原样。"""
    from agent_bot.services.multi_reply import join_reply_array

    assert join_reply_array(text) == expected


@pytest.mark.parametrize("text", [
    '普通正文', '```json\n["正文"]\n```', '{}', '[]', '[null]', '[1]',
    '[true]', '["有效", "   "]', '["一", "二", "三", "四"]', '["未完成"',
    '{"消息内容":"正文"}',
])
def test_invalid_multi_reply_is_rejected_without_fallback(text):
    """格式错误必须拒绝，不能把包装内容投递或擅自改写。"""
    from agent_bot.services.multi_reply import parse_multi_reply

    with pytest.raises(ChatAiProtocolError, match="多条回复"):
        parse_multi_reply(text, 3)


def test_multi_reply_settings_are_dynamic_and_defaults_preserve_existing_delivery():
    """新配置默认关闭且进入数据库字段集合，不接受启动环境覆盖。"""
    from agent_bot.static_config import DYNAMIC_SETTING_NAMES

    config = make_config()
    assert config.chat_multi_reply_enabled is False
    assert config.chat_multi_reply_max_messages == 3
    assert config.chat_multi_reply_interval_seconds == 1
    assert {
        "chat_multi_reply_enabled", "chat_multi_reply_max_messages",
        "chat_multi_reply_interval_seconds", "chat_multi_reply_prompt",
    } <= DYNAMIC_SETTING_NAMES


@pytest.mark.parametrize("values", [
    {"chat_multi_reply_max_messages": 0}, {"chat_multi_reply_max_messages": 11},
    {"chat_multi_reply_interval_seconds": -1}, {"chat_multi_reply_interval_seconds": 11},
    {"chat_multi_reply_interval_seconds": float("nan")},
])
def test_multi_reply_settings_enforce_bounds(values):
    """运行配置在保存前拒绝不可执行的条数和间隔。"""
    with pytest.raises(ValidationError):
        make_config(**values)


def test_multi_reply_prompt_keeps_protocol_after_custom_instructions():
    """管理员可改写表达风格，程序仍在其后固定最终正文协议。"""
    from agent_bot.services.multi_reply import build_multi_reply_prompt

    config = make_config(chat_multi_reply_prompt="按语义分条", chat_multi_reply_max_messages=2)
    prompt = build_multi_reply_prompt(config)
    assert prompt.startswith("按语义分条")
    assert "JSON" in prompt and "2" in prompt
    assert "工具" in prompt and "代码块" in prompt
    assert '["' in prompt
