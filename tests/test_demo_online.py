"""真实模型验收入口的离线替身检查，不作为真实AI质量证据。"""
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from app.ai.client import LLMClient
from app.ai.service import AIService
from scripts.demo_online import online_client, RequestRecorder


ROOT = Path(__file__).resolve().parents[1]
SECRET = 'synthetic-offline-only-api-secret'


def test_prepare_mode_uses_real_redacted_prompt_without_scoring():
    result = subprocess.run([sys.executable, '-B', 'scripts/check_demo_samples.py', '--prepare-online'],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    report_path = Path(result.stdout.strip())
    report = json.loads(report_path.read_text(encoding='utf-8'))
    prepared = json.loads(report_path.with_name('requests.preview.json').read_text(encoding='utf-8'))
    assert report['request_count'] == 0 and report['quality_acceptance'] == 'not_executed'
    assert report['results'] == [] and len(prepared) == report['pair_count'] == 24
    assert report['isolation']['unchanged'] is True
    class PreparedOnly(Exception): pass
    class Capture:
        def generate_json(self, *, messages, **kwargs):
            self.messages = messages
            raise PreparedOnly
    client = Capture()
    for row in prepared:
        with pytest.raises(PreparedOnly):
            AIService(client).evaluate_candidate(row['input_snapshot'])
        assert client.messages == row['messages']
        payload = json.loads(row['messages'][1]['content'])
        assert set(payload) == {'structured_data', 'dimensions', 'scoring_contract', 'output_template'}
        assert row['parsed_candidate']['email'] not in row['messages'][1]['content']


def config_file(tmp_path, **overrides):
    values = dict(LLM_PROVIDER='openai_compatible', LLM_BASE_URL='https://example.test/v1',
                  LLM_MODEL='synthetic-model', LLM_API_KEY=SECRET)
    values.update(overrides)
    config = tmp_path / 'online.runtime.env'
    config.write_text('\n'.join(f'{key}={value}' for key, value in values.items()), encoding='utf-8')
    return config


@pytest.mark.parametrize('overrides', [dict(LLM_API_KEY=''), dict(LLM_PROVIDER='mock'),
    dict(LLM_BASE_URL='http://example.test'), dict(LLM_BASE_URL='https://user:password@example.test'),
    dict(LLM_BASE_URL='https://example.test?key=secret')])
def test_online_config_does_not_inherit_missing_or_unsafe_values(tmp_path, monkeypatch, overrides):
    monkeypatch.setenv('LLM_API_KEY', 'production-secret-must-not-be-used')
    with pytest.raises(ValueError):
        online_client(config_file(tmp_path, **overrides))


def test_test_config_timeouts_are_explicit_and_do_not_inherit_ambient(tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_CONNECT_TIMEOUT_SECONDS', 'invalid')
    monkeypatch.setenv('LLM_READ_TIMEOUT_SECONDS', '999')
    client = online_client(config_file(tmp_path))
    assert client.timeout.connect == 30 and client.timeout.read == 120
    client = online_client(config_file(tmp_path, LLM_CONNECT_TIMEOUT_SECONDS='15', LLM_READ_TIMEOUT_SECONDS='95'))
    assert client.timeout.connect == 15 and client.timeout.read == 95
    with pytest.raises(ValueError, match='有限正数'):
        online_client(config_file(tmp_path, LLM_READ_TIMEOUT_SECONDS='nan'))


def test_recorder_enforces_endpoint_single_attempt_and_24_limit(tmp_path, monkeypatch):
    sent = []
    def send(url, **kwargs):
        sent.append(kwargs)
        return httpx.Response(200, json={'id': 'completion-test', 'model': 'synthetic-model',
            'usage': {'total_tokens': 42}}, headers={'x-request-id': 'request-test'})
    monkeypatch.setattr(httpx, 'post', send)
    recorder = RequestRecorder(online_client(config_file(tmp_path)), tmp_path / 'requests.json')
    payload = {'json': {'model': 'synthetic-model', 'messages': []}, 'headers': {'Authorization': f'Bearer {SECRET}'}}
    for number in range(24):
        recorder.pair = f'pair-{number}'
        recorder.post(recorder.endpoint, **payload)
        with pytest.raises(ValueError):
            recorder.post(recorder.endpoint, **payload)
    recorder.pair = 'pair-25'
    with pytest.raises(ValueError): recorder.post(recorder.endpoint, **payload)
    with pytest.raises(ValueError): recorder.post('https://other.test', **payload)
    text = recorder.output.read_text(encoding='utf-8')
    rows = json.loads(text)
    assert len(sent) == len(rows) == 24
    assert all(row['trust_env'] is False and row['follow_redirects'] is False for row in sent)
    assert SECRET not in text and 'Authorization' not in text
    assert rows[0]['service_request_id'] == 'request-test'
    assert rows[0]['completion_id'] == 'completion-test' and rows[0]['usage']['total_tokens'] == 42


@pytest.mark.parametrize('secret', [SECRET, 'synthetic-quote"key\\suffix'])
def test_echoed_secret_is_redacted_and_never_enters_business_output(tmp_path, monkeypatch, secret):
    def send(url, **kwargs):
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({'reason': secret})}}]})
    monkeypatch.setattr(httpx, 'post', send)
    recorder = RequestRecorder(online_client(config_file(tmp_path)), tmp_path / 'requests.json')
    recorder.client.api_key = secret
    recorder.pair = 'r01-backend'
    with pytest.raises(ValueError, match='回显密钥'):
        recorder.post(recorder.endpoint, json={'model': 'synthetic-model', 'messages': []})
    rows = json.loads(recorder.output.read_text(encoding='utf-8'))
    content = json.loads(rows[0]['response_body']['choices'][0]['message']['content'])
    assert content['reason'] == '[REDACTED]'
    assert rows[0]['status'] == 'request_failed'


def test_online_batch_retains_failed_requests_and_never_falls_back(tmp_path):
    config = config_file(tmp_path)
    # 子进程同样禁网；只替换HTTP传输，解析、入库、业务评分和证据校验走真实代码。
    code = '''
import json, runpy, socket, sys
from pathlib import Path
import httpx
def deny(*args, **kwargs):
    raise AssertionError('offline test: no network')
socket.create_connection = socket.socket.connect = deny
calls = 0
def send(url, **kwargs):
    global calls
    from app.ai.service import _mock_evaluation
    from app.config import DATA_DIR, ENV_FILE, UPLOAD_DIR
    from app.db import engine
    assert DATA_DIR.name.startswith('samples-')
    assert Path(engine.url.database).parent == DATA_DIR
    assert ENV_FILE.parent == UPLOAD_DIR.parent == DATA_DIR
    calls += 1
    request = httpx.Request('POST', url)
    if calls == 1:
        return httpx.Response(401, json={'error':'synthetic authentication failure'}, request=request)
    if calls == 2:
        raise httpx.ReadTimeout('synthetic timeout')
    if calls == 3:
        return httpx.Response(200, text='not JSON', request=request)
    data = json.loads(kwargs['json']['messages'][1]['content'])['structured_data']
    result = _mock_evaluation(data)
    return httpx.Response(200, json={'id':f'synthetic-{calls}', 'model':'synthetic-model',
        'usage':{'total_tokens':42}, 'choices':[{'message':{'content':json.dumps(result,ensure_ascii=False)}}]}, request=request)
httpx.post = send
config = sys.argv[1]
sys.argv = ['scripts/check_demo_samples.py', '--online-config', config]
runpy.run_path(sys.argv[0], run_name='__main__')
'''
    result = subprocess.run([sys.executable, '-B', '-c', code, str(config)], cwd=ROOT, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 1, result.stderr
    report_path = next(Path(line) for line in result.stdout.splitlines() if line.endswith('report.json'))
    report = json.loads(report_path.read_text(encoding='utf-8'))
    requests = json.loads(report_path.with_name('requests.json').read_text(encoding='utf-8'))
    assert report['pair_count'] == report['request_count'] == len(requests) == 24
    assert report['benchmark_check']['summary']['failed_evaluations'] == 3
    assert all(row['total_score'] is None and row['status_after'] == row['status_before'] for row in report['results'][:3])
    assert all(row['fallback'] is False for row in report['results'])
    assert report['isolation']['unchanged'] is True
    assert report['quality_acceptance'] == 'not_passed'
    assert SECRET not in report_path.with_name('runtime.env').read_text(encoding='utf-8')
    assert SECRET not in report_path.with_name('requests.json').read_text(encoding='utf-8')
