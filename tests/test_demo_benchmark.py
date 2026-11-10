"""比较器的合成反例；构造的匹配输出不能算实际模型验收证据。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from scripts.demo_benchmark import compare_results, load_benchmark

SAMPLES = Path(__file__).parent / 'fixtures/demo'


@pytest.fixture
def comparison_input():
    baseline = load_benchmark(SAMPLES)
    results = []
    for i, pair in enumerate(baseline['pairs'], 1):
        catalog = {}
        dimensions = []
        for name, weight in baseline['jobs'][pair['job_id']]['effective_weights'].items():
            criteria = []
            for source in pair['criteria']:
                if not source['id'].startswith(name + ':'):
                    continue
                criterion = deepcopy(source)
                quote = source['references'][0]['quote']
                catalog[source['id']] = quote
                criterion['references'] = [{'id': source['id'], 'quote': quote}] if source['score'] else []
                criteria.append(criterion)
            score = round(sum(c['score'] for c in criteria) / len(criteria), 2) if criteria else 0
            dimensions.append({'name': name, 'score': score, 'weight': weight, 'criteria': criteria})
        jd = baseline['jd_texts'][pair['job_id']]
        results.append({'sample_id': pair['sample_id'], 'job_id': pair['job_id'], 'application_id': i,
                        'dimensions': dimensions, 'total_score': pair['total_score'], 'job_version_id': 1,
                        'model': 'synthetic-unit-test', 'prompt_version': 'test',
                        'evaluation_version': baseline['scoring_rules_version'],
                        'is_stale': False,
                        'facts_unchanged': True, 'no_hiring_decision': True,
                        'input_snapshot': {'job': {'raw_jd': jd}, 'evidence_catalog': catalog,
                                           'input_version': {'application_id': i, 'job_version_id': 1,
                                                             'jd_hash': hashlib.sha256(jd.strip().encode()).hexdigest()}}})
    return baseline, results


def test_numerical_match_does_not_self_approve_semantics(comparison_input):
    report = compare_results(*comparison_input)
    assert report['automated_check'] == 'passed'
    assert report['semantic_acceptance'] == 'pending_independent_review'
    assert report['summary']['criterion_score_status_matches'] == 96


def test_failed_evaluation_is_not_a_zero_score_or_dropped_pair(comparison_input):
    baseline, rows = comparison_input
    rows[0].update(evaluation_error='请求超时', dimensions=[], total_score=None)
    report = compare_results(baseline, rows)
    assert len(report['pairs']) == 24
    assert report['summary']['failed_evaluations'] == 1
    assert report['pairs'][0]['actual_total'] is None
    assert report['automated_check'] == 'failed'
    assert any(not anchor['passed'] for anchor in report['required_rankings'])
    assert all('r01-backend' not in (row['left'], row['right']) for row in report['derived_ordering_changes'])


def test_equal_total_cannot_hide_swapped_criterion_scores(comparison_input):
    baseline, rows = comparison_input
    criteria = rows[2]['dimensions'][0]['criteria']
    criteria[0]['score'], criteria[1]['score'] = criteria[1]['score'], criteria[0]['score']
    report = compare_results(baseline, rows)
    assert report['pairs'][2]['total_match'] is True
    assert report['pairs'][2]['automated_match'] is False


def test_same_score_with_wrong_transfer_status_is_reported(comparison_input):
    baseline, rows = comparison_input
    rows[3]['dimensions'][0]['criteria'][1]['status'] = 'self_report'
    item = compare_results(baseline, rows)['pairs'][3]['criteria'][1]
    assert item['score_match'] and not item['status_match']


@pytest.mark.parametrize('refs', [[], [{'id': 'missing', 'quote': 'SQL'}], [{'id': 'required_skills:1', 'quote': '虚构原文'}]])
def test_missing_or_fabricated_references_fail(comparison_input, refs):
    baseline, rows = comparison_input
    rows[0]['dimensions'][0]['criteria'][0]['references'] = refs
    report = compare_results(baseline, rows)
    assert report['summary']['invalid_reference_criteria'] == 1
    assert report['automated_check'] == 'failed'


@pytest.mark.parametrize('mutation', ['missing_pair', 'duplicate_pair', 'duplicate_dimension', 'duplicate_criterion'])
def test_coverage_cannot_hide_duplicate_or_missing_records(comparison_input, mutation):
    baseline, rows = comparison_input
    if mutation == 'missing_pair': rows.pop()
    elif mutation == 'duplicate_pair': rows.append(deepcopy(rows[0]))
    elif mutation == 'duplicate_dimension': rows[0]['dimensions'].append(deepcopy(rows[0]['dimensions'][0]))
    else: rows[0]['dimensions'][0]['criteria'].append(deepcopy(rows[0]['dimensions'][0]['criteria'][0]))
    with pytest.raises(ValueError):
        compare_results(baseline, rows)


def test_wrong_jd_mutated_facts_and_hiring_decision_do_not_pass(comparison_input):
    baseline, rows = comparison_input
    rows[0]['input_snapshot']['job']['raw_jd'] = baseline['jd_texts']['frontend']
    rows[1]['facts_unchanged'] = False
    rows[2]['no_hiring_decision'] = False
    report = compare_results(baseline, rows)
    assert all(not p['automated_match'] for p in report['pairs'][:3])
    assert not report['pairs'][0]['context_valid']


@pytest.mark.parametrize('mutation', ['wrong_dimension', 'stale_result'])
def test_zero_scores_do_not_hide_wrong_dimension_or_stale_result(comparison_input, mutation):
    baseline, rows = comparison_input
    row = rows[1]
    if mutation == 'wrong_dimension':
        skills, project = row['dimensions'][0]['criteria'], row['dimensions'][1]['criteria']
        skills[0], project[0] = project[0], skills[0]
    else:
        row['is_stale'] = True
    pair = compare_results(baseline, rows)['pairs'][1]
    assert pair['total_match'] is True
    assert pair['automated_match'] is False


def test_tampered_or_unsigned_baseline_is_rejected(tmp_path):
    fixtures = tmp_path / 'samples'
    shutil.copytree(SAMPLES, fixtures)
    path = fixtures / 'annotations-v2.1.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    data['human_signoff'] = False
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError, match='冻结文件'):
        load_benchmark(fixtures)
    manifest_path = fixtures / 'manifest-v2.1.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['files'][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='尚未全部签收'):
        load_benchmark(fixtures)


def test_revision_changes_only_approved_b06_status_and_preserves_all_scores():
    old, current = load_benchmark(SAMPLES, 'v2'), load_benchmark(SAMPLES)
    assert old['version'] == 'demo-v2' and current['version'] == 'demo-v2.1'
    changes = []
    for previous, revised in zip(old['pairs'], current['pairs']):
        assert previous['id'] == revised['id'] and previous['total_score'] == revised['total_score']
        for left, right in zip(previous['criteria'], revised['criteria']):
            if left != right:
                changes.append((previous['id'], left['id'], left['status'], right['status']))
                assert {**left, 'status': 'self_report'} == right
    assert changes == [('r06-backend', 'required_skills:2', 'transfer', 'self_report')]
    assert old['comparisons'] == current['comparisons']


def test_old_rule_results_cannot_pass_current_acceptance_even_if_values_match(comparison_input):
    baseline, rows = comparison_input
    for row in rows:
        row['evaluation_version'] = '2.0-r1-r5'
    report = compare_results(baseline, rows)
    assert report['summary']['criterion_score_status_matches'] == 96
    assert report['summary']['automated_pair_matches'] == 0
    assert all(not pair['context_valid'] for pair in report['pairs'])


@pytest.mark.parametrize('version', ['v1', '../v2'])
def test_unknown_benchmark_version_is_rejected(version):
    with pytest.raises(ValueError, match='不支持的基准版本'):
        load_benchmark(SAMPLES, version)
