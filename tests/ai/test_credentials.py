"""数据库凭据校验拒绝无效输入且禁止环境引用。"""

import pytest
from pydantic import ValidationError

from agent_bot.ai.contracts import ProviderCreate


@pytest.mark.parametrize("credential", ["", " ", "line\nbreak", "x" * 4097, "**********"])
def test_invalid_credentials_do_not_echo_input(credential):
    """写入请求不得保存空值、控制字符或脱敏占位符。"""
    with pytest.raises(ValidationError) as caught:
        ProviderCreate(
            name="test", protocol="ollama", base_url="http://localhost", credential=credential
        )
    assert "input_value" not in str(caught.value)


def test_environment_reference_is_not_an_accepted_configuration():
    """旧变量引用已经退出 API 契约。"""
    with pytest.raises(ValidationError):
        ProviderCreate(
            name="test",
            protocol="ollama",
            base_url="http://localhost",
            credential_ref="AGENT_BOT_PROVIDER_TEST_KEY",
        )
