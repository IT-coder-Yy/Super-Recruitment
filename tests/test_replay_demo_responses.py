"""验证离线回放的隔离与溯源；合成响应不代表模型质量。"""
from copy import deepcopy
import json

import pytest

from app.ai.prompts import PROMPT_VERSION, EVALUATION_VERSION, build_evaluation_messages
from app.ai.service import _mock_evaluation, AIService
from app.services.imports import build_rule_parse
from app.services.redaction import redact_data
from scripts.replay_demo_responses import replay_results


@pytest.fixture
def recording():
    text = '技能：Python\n项目：本人使用Python实现接口'
    data = build_rule_parse(text, jd_text='要求：Python；说明本人负责的实现；每周四天')
    data['evidence_catalog'] = {'source:1': text}
    raw = _mock_evaluation(data)
    raw['evaluation_version'] = '2.0-r1-r5'
    raw['dimensions'][-1]['criteria'] = [{'id':'communication_intent:1','score':0,'status':'self_report'}]
    source = {'scoring_rules': '2.0-r1-r5', 'results': [{'sample_id':'sample','job_id':'role','model':'recorded-model','input_snapshot':data,
                          'evaluation_error':'旧校验失败','dimensions':[],'total_score':None}]}
    requests = [{'pair':'sample-role', 'status':'responded', 'http_status':200,
                 'request':{'model':'recorded-model','messages':build_evaluation_messages(redact_data(data))},
                 'response_body':{'choices':[{'message':{'content':json.dumps(raw,ensure_ascii=False)}}]}}]
    return source, requests


def test_replay_uses_saved_response_and_keeps_generation_version_and_sources(recording):
    source, requests = recording
    before = deepcopy(recording)
    rows = replay_results(source, requests, 'original-prompt')
    assert recording == before
    assert rows[0]['replay_status'] == 'validated' and rows[0]['total_score'] > 0
    assert rows[0]['provider'] == 'recorded_response'
    assert rows[0]['prompt_version'] == 'original-prompt'
    assert rows[0]['replay_validator_prompt_version'] == PROMPT_VERSION
    assert rows[0]['evaluation_version'] == '2.0-r1-r5'
    assert rows[0]['replay_validator_evaluation_version'] == EVALUATION_VERSION
    assert rows[0]['source_evaluation_error'] == '旧校验失败'
    assert rows[0]['dimensions'][-1]['criteria'] == []


def test_timeouts_remain_unscored_and_do_not_call_the_evaluator(recording, monkeypatch):
    source, requests = recording
    requests[0].update(status='request_failed', http_status=None, error_type='ReadTimeout')
    requests[0].pop('response_body')
    monkeypatch.setattr(AIService, 'evaluate_candidate', lambda *a, **k: pytest.fail('超时没有可回放响应'))
    row = replay_results(source, requests, 'original-prompt')[0]
    assert row['replay_status'] == 'no_complete_response'
    assert row['total_score'] is None and row['evaluation_error']


def test_replay_does_not_repair_paraphrased_implementation_to_force_a_pass(recording):
    source, requests = recording
    raw = json.loads(requests[0]['response_body']['choices'][0]['message']['content'])
    raw['dimensions'][0]['criteria'][0].update(score=70,status='implemented',implementation_evidence='推测本人实现，非逐字引用')
    requests[0]['response_body']['choices'][0]['message']['content'] = json.dumps(raw,ensure_ascii=False)
    row = replay_results(source, requests, 'original-prompt')[0]
    assert row['replay_status'] == 'rejected' and row['total_score'] is None
    assert '逐字本人实现' in row['evaluation_error']


@pytest.mark.parametrize('mutation', ['duplicate_request','missing_request','wrong_model','wrong_input','unknown_prompt_version',
                                      'missing_scoring_rules','wrong_scoring_rules'])
def test_replay_rejects_mismatched_evidence(recording, mutation):
    source, requests = recording
    version = 'original-prompt'
    if mutation == 'duplicate_request': requests.append(deepcopy(requests[0]))
    elif mutation == 'missing_request': requests.clear()
    elif mutation == 'wrong_model': requests[0]['request']['model'] = 'other-model'
    elif mutation == 'wrong_input': source['results'][0]['input_snapshot']['candidate']['skills'] = ['other']
    elif mutation == 'unknown_prompt_version': version = ''
    elif mutation == 'missing_scoring_rules': source.pop('scoring_rules')
    else: source['results'][0]['evaluation_version'] = 'different-rule-version'
    with pytest.raises(ValueError):
        replay_results(source, requests, version)
