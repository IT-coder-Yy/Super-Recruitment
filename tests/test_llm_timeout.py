"""验证超时配置实际传入客户端；HTTP替身不请求真实模型。"""
import httpx
import pytest

from app.ai.client import LLMClient
from app import config


def test_timeout_defaults_and_explicit_override(monkeypatch):
    for name in ('LLM_CONNECT_TIMEOUT_SECONDS', 'LLM_READ_TIMEOUT_SECONDS'):
        monkeypatch.delenv(name, raising=False)
    assert LLMClient().timeout.as_dict() == {'connect': 30.0, 'read': 120.0, 'write': 30.0, 'pool': 30.0}
    monkeypatch.setenv('LLM_CONNECT_TIMEOUT_SECONDS', '9')
    monkeypatch.setenv('LLM_READ_TIMEOUT_SECONDS', '85')
    assert LLMClient(timeout=7).timeout.as_dict() == dict.fromkeys(('connect', 'read', 'write', 'pool'), 7.0)
    assert LLMClient(timeout=7, read_timeout=45).timeout.read == 45


@pytest.mark.parametrize('error_type', [httpx.ReadTimeout, httpx.ConnectTimeout])
def test_configured_timeouts_reach_http_and_failure_is_not_retried(monkeypatch, error_type):
    monkeypatch.setenv('LLM_CONNECT_TIMEOUT_SECONDS', '8.5')
    monkeypatch.setenv('LLM_READ_TIMEOUT_SECONDS', '91')
    calls = []
    def request(url, **kwargs):
        calls.append(kwargs)
        raise error_type('synthetic-sensitive-error')
    monkeypatch.setattr(httpx, 'post', request)
    client = LLMClient(provider='openai_compatible', base_url='https://example.test/v1', model='synthetic', api_key='synthetic')
    with pytest.raises(ValueError) as error:
        client.generate_json(messages=[], fallback=lambda: pytest.fail('在线失败不得回退'))
    assert len(calls) == 1
    assert calls[0]['timeout'].connect == 8.5 and calls[0]['timeout'].read == 91
    assert error_type.__name__ in str(error.value)
    assert 'synthetic-sensitive-error' not in str(error.value)


@pytest.mark.parametrize('name', ['LLM_CONNECT_TIMEOUT_SECONDS', 'LLM_READ_TIMEOUT_SECONDS'])
@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf', 'invalid'])
def test_invalid_timeout_fails_before_any_request(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(httpx, 'post', lambda *a, **k: pytest.fail('无效配置不得发送'))
    with pytest.raises(ValueError, match='有限正数'):
        LLMClient()


def test_runtime_file_timeouts_override_old_environment(tmp_path, monkeypatch):
    path = tmp_path / 'runtime.env'
    path.write_text('LLM_CONNECT_TIMEOUT_SECONDS=12\nLLM_READ_TIMEOUT_SECONDS=88\n', encoding='utf-8')
    monkeypatch.setattr(config, 'ENV_FILE', path)
    monkeypatch.setenv('LLM_CONNECT_TIMEOUT_SECONDS', '2')
    monkeypatch.setenv('LLM_READ_TIMEOUT_SECONDS', '3')
    config.reload_env()
    assert LLMClient().timeout.connect == 12 and LLMClient().timeout.read == 88
