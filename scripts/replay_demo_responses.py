"""离线回放已保存的真实响应；不读取密钥、不请求模型、不改写原始证据。"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]


def replay_results(source_report, requests, generation_prompt_version):
    import httpx
    from app.ai.client import LLMClient
    from app.ai.service import AIService
    from app.ai.prompts import PROMPT_VERSION, EVALUATION_VERSION
    from app.services.redaction import redact_data

    indexed = {row['pair']: row for row in requests}
    rows = deepcopy(source_report['results'])
    pairs = {f"{row['sample_id']}-{row['job_id']}" for row in rows}
    if len(indexed) != len(requests) or len(pairs) != len(rows) or pairs != indexed.keys():
        raise ValueError('已存请求和结果必须逐组唯一对应')
    if not generation_prompt_version:
        raise ValueError('缺少原响应生成时的提示版本，不能用新版本冒充')
    generation_rules = source_report.get('scoring_rules')
    if not generation_rules:
        raise ValueError('缺少原响应生成时的评分规则版本，不能用新版本冒充')

    def deny(*args, **kwargs):
        raise AssertionError('离线回放禁止网络')

    for row in rows:
        recorded = indexed[f"{row['sample_id']}-{row['job_id']}"]
        payload = json.loads(recorded['request']['messages'][1]['content'])['structured_data']
        if payload != redact_data(row['input_snapshot']) or recorded['request']['model'] != row['model']:
            raise ValueError('已存请求与投递输入或模型不一致，拒绝错配回放')
        if row.get('evaluation_version') not in (None, generation_rules):
            raise ValueError('结果与原报告的评分规则版本不一致')
        row.update(source_evaluation_error=row.get('evaluation_error'), offline_replay=True,
                   prompt_version=generation_prompt_version, replay_validator_prompt_version=PROMPT_VERSION,
                   evaluation_version=generation_rules, replay_validator_evaluation_version=EVALUATION_VERSION)
        if recorded.get('http_status') != 200 or recorded['status'] != 'responded':
            row.update(replay_status='no_complete_response', evaluation_error='原请求无可回放的完整成功响应',
                       dimensions=[], total_score=None)
            continue
        client = LLMClient(provider='openai_compatible', base_url='https://offline.invalid/v1',
                           api_key='offline-placeholder', model=row['model'], connect_timeout=30, read_timeout=120)
        response = httpx.Response(200, json=recorded['response_body'], request=httpx.Request('POST', client._chat_completions_url()))
        try:
            with patch('socket.create_connection', deny), patch('socket.socket.connect', deny), \
                    patch('app.ai.client.httpx.post', return_value=response) as transport:
                result = AIService(client).evaluate_candidate(row['input_snapshot'])
                assert transport.call_count == 1
            row.pop('evaluation_error', None)
            row.update(replay_status='validated', dimensions=result['dimensions'], total_score=result['total_score'],
                       model=result['model'], provider='recorded_response',
                       reason=result['recommendation_reason'], missing_information=result['missing_information'],
                       fallback=False, is_stale=False)
        except ValueError as error:
            while error.__cause__ is not None:
                error = error.__cause__
            row.update(replay_status='rejected', evaluation_error=str(error), dimensions=[], total_score=None)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recorded_run', type=Path, help='原24次调用的证据目录，须含report/requests/analysis.json')
    args = parser.parse_args()
    source = args.recorded_run.resolve()
    output = ROOT / '.test-runs' / f'replay-{uuid.uuid4().hex}'
    output.mkdir(parents=True)
    os.environ.update(APP_DATA_DIR=str(output), APP_RUNTIME_ENV=str(output / 'runtime.env'),
                      DATABASE_URL='sqlite:///:memory:', LLM_PROVIDER='mock', LLM_API_KEY='',
                      LLM_BASE_URL='', LLM_MODEL='', SMTP_HOST='', SMTP_PASSWORD='', WECOM_WEBHOOK_URL='')
    sys.path.insert(0, str(ROOT))
    from app.ai.prompts import PROMPT_VERSION, EVALUATION_VERSION
    from scripts.demo_benchmark import load_benchmark, compare_results
    current_baseline = load_benchmark(ROOT / 'tests/fixtures/demo')
    paths = [source / name for name in ('report.json', 'requests.json', 'analysis.json', 'demo.db')]
    digests = lambda: {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    before = digests()
    original = json.loads(paths[0].read_text(encoding='utf-8'))
    requests = json.loads(paths[1].read_text(encoding='utf-8'))
    metadata = json.loads(paths[2].read_text(encoding='utf-8'))
    source_baseline_version = original['benchmark_check']['baseline_version'].removeprefix('demo-')
    baseline = load_benchmark(ROOT / 'tests/fixtures/demo', source_baseline_version)
    if original.get('scoring_rules') != metadata['scoring_rules_version'] or original['scoring_rules'] != baseline['scoring_rules_version']:
        raise ValueError('原报告、生成元数据与原基准的评分规则版本不一致')
    rows = replay_results(original, requests, metadata['prompt_version'])
    comparison = compare_results(baseline, rows)
    current_comparison = compare_results(current_baseline, rows)
    after = digests()
    assert before == after
    load_benchmark(ROOT / 'tests/fixtures/demo')
    load_benchmark(ROOT / 'tests/fixtures/demo', source_baseline_version)
    report = {'artifact_type': 'offline_recorded_response_replay', 'source_run': str(source),
              'source_prompt_version': metadata['prompt_version'], 'validator_prompt_version': PROMPT_VERSION,
              'source_scoring_rules_version': original['scoring_rules'], 'validator_scoring_rules_version': EVALUATION_VERSION,
              'new_model_requests': 0, 'source_request_count': len(requests),
              'summary': {status: sum(row['replay_status'] == status for row in rows)
                          for status in ('validated', 'rejected', 'no_complete_response')},
              'benchmark_check': comparison, 'results': rows,
              'current_baseline_diagnostic': {
                  'comparison': current_comparison,
                  'pairs_matching_scores_and_statuses': sum(p['total_match'] and p['criterion_set_match'] and all(
                      c['score_match'] and c['status_match'] for c in p['criteria']) for p in current_comparison['pairs']),
                  'limits': '仅用旧输出诊断新基准的分数和状态差异；保留原生成规则版本，跨版本结果不得算新规则验收通过。'},
              'source_evidence': {'before': before, 'after': after, 'unchanged': before == after},
              'new_prompt_quality': 'not_executed', 'online_timeout_effectiveness': 'not_executed',
              'limits': '只重放旧生成响应，未验证新提示的生成效果或线上超时改善；原业务库和失败记录保持原样。'}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(output / 'report.json')
    print(json.dumps({'replay': report['summary'], 'benchmark': comparison['summary']}, ensure_ascii=False))
    return 0 if comparison['automated_check'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
