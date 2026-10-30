"""明确反馈的有限识别规则；原话永远不作为可执行提示词。"""

import re

TRAITS = ('verbosity', 'warmth', 'humor', 'initiative')
FEEDBACK = {
    '简洁一点': ('verbosity', -1), '简短一点': ('verbosity', -1),
    '详细一点': ('verbosity', 1), '多解释一点': ('verbosity', 1),
    '亲切一点': ('warmth', 1), '温柔一点': ('warmth', 1),
    '正式一点': ('warmth', -1), '客观一点': ('warmth', -1),
    '幽默一点': ('humor', 1), '轻松一点': ('humor', 1),
    '严肃一点': ('humor', -1), '少开玩笑': ('humor', -1),
    '主动一点': ('initiative', 1), '多主动建议': ('initiative', 1),
    '少主动建议': ('initiative', -1), '少主动一点': ('initiative', -1),
}
_PATTERN = re.compile(
    r'(?:以后|今后)(?:请|你|你可以|回复时请|回答时请)?'
    r'(' + '|'.join(FEEDBACK) + r')[。！!~]?',
)
SUGGESTIONS = {
    'verbosity': ('优先简洁概括，保留必要信息。', '适当补充解释和示例，避免重复。'),
    'warmth': ('表达偏正式、客观。', '表达更亲切、体贴，尊重用户边界。'),
    'humor': ('保持严肃，减少玩笑。', '场景合适时可适度幽默。'),
    'initiative': ('减少未经请求的延伸建议。', '适当主动给出相关的下一步建议。'),
}


def extract_feedback(text: str) -> tuple[tuple[str, int], ...]:
    """只识别完整的长期表达反馈句，不解释引用、条件、否定或混合内容。"""
    match = _PATTERN.fullmatch(text.strip())
    return (FEEDBACK[match[1]],) if match else ()


def evolution_prompt(traits: dict[str, int]) -> str:
    """只输出代码定义的建议，强度有界且不改变角色与工具授权。"""
    lines = [f'{SUGGESTIONS[key][int(value > 0)]}(习惯强度 {abs(value)}/3)'
             for key in TRAITS if (value := traits[key])]
    if not lines:
        return ''
    return ('【长期互动形成的表达习惯】\n仅用于当前互动对象；当前明确要求与角色规则优先，'
            '不改变职责、事实判断或工具权限。\n' + '\n'.join(lines))
