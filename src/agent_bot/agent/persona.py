"""统一组合 Agent 的角色与人格，供聊天和系统任务共同使用。"""

MAX_SYSTEM_PROMPT_LENGTH = 32000


def compose_system_prompt(system_prompt: str, personality_prompt: str = "") -> str:
    """空人格保留旧角色原文；非空人格与角色分段组合，不授予额外权限。"""
    personality = personality_prompt.strip()
    if not personality:
        return system_prompt
    role = system_prompt.strip()
    prefix = f"【角色设定】\n{role}\n\n" if role else ""
    return f"{prefix}【人格设定】\n{personality}"
