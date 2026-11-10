"""R1—R5已确认政策的确定性契约；语义质量仍单独签收。"""
from copy import deepcopy
import json

import pytest

from app.ai.client import LLMClient
from app.ai.prompts import build_scoring_contract, EVALUATION_VERSION
from app.ai.service import AIService, _mock_evaluation, _normalize_evaluation
from app.services.imports import build_rule_parse


def fixture_data():
    return build_rule_parse("技能：Python、SQL\n项目：本人使用Python实现接口", jd_text="要求：Python；SQL；说明本人负责的实现；每周至少四天")


def normalized(data):
    return _normalize_evaluation(_mock_evaluation(data), data)


def test_jd_applicability_and_missing_candidate_evidence_are_different():
    data = fixture_data()
    result = normalized(data)
    weights = {item['name']: item['weight'] for item in result['dimensions']}
    assert weights == {'required_skills': 35/60, 'project_evidence': 25/60,
                       'relevant_experience': 0, 'domain_match': 0, 'communication_intent': 0}
    data['candidate']['skills'] = []
    data['candidate']['project_experience'] = []
    empty = normalized(data)
    assert {item['name']: item['weight'] for item in empty['dimensions']} == weights
    assert empty['total_score'] == 0
    assert any('简历未提供' in item for item in empty['missing_information'])


def test_one_missing_requirement_does_not_zero_the_whole_dimension():
    data = fixture_data(); payload = _mock_evaluation(data)
    entries = payload['dimensions'][0]['criteria']
    entries[0].update(score=30, status='self_report')
    entries[1].update(score=0, status='unknown')
    result = _normalize_evaluation(payload, data)
    assert result['dimensions'][0]['score'] == 15
    assert result['evaluation_version'] == EVALUATION_VERSION


@pytest.mark.parametrize('bad_score', [10, 60, 85])
def test_unapproved_score_anchors_are_rejected(bad_score):
    data = fixture_data(); payload = _mock_evaluation(data)
    payload['dimensions'][0]['criteria'][0].update(score=bad_score, status='implemented')
    with pytest.raises(ValueError, match='档位'):
        _normalize_evaluation(payload, data)


@pytest.mark.parametrize('metric,accepted', [('延迟降低40%', True), ('无量化指标', False), ('实习3个月', False), ('Python3.12', False)])
def test_100_requires_implementation_verification_and_quantified_result(metric, accepted):
    data = fixture_data(); payload = _mock_evaluation(data)
    entry = payload['dimensions'][0]['criteria'][0]
    entry.update(score=100, status='quantified', implementation_evidence='本人实现接口',
                 verification_evidence='回归验证', metric_evidence=metric,
                 resume_evidence=f'本人实现接口，回归验证，{metric}')
    if accepted:
        assert _normalize_evaluation(payload, data)['dimensions'][0]['criteria'][0]['score'] == 100
        entry['implementation_evidence'] = ''
        with pytest.raises(ValueError, match='本人实现'):
            _normalize_evaluation(payload, data)
    else:
        with pytest.raises(ValueError, match='量化指标'):
            _normalize_evaluation(payload, data)


def test_course_project_can_support_required_practice_without_employer_bonus():
    data = build_rule_parse('项目：本人使用Python开发接口', jd_text='要求：Python开发经验')
    course = normalized(data)
    changed = deepcopy(data)
    changed['candidate']['internship_experience'] = changed['candidate']['project_experience']
    assert normalized(changed)['total_score'] == course['total_score']
    experience = next(item for item in course['dimensions'] if item['name'] == 'relevant_experience')
    assert experience['score'] == 70


def test_attendance_and_unknown_start_date_never_change_total():
    data = fixture_data()
    totals = []
    for text, status in [('每周四天','自述满足'), ('每周三天','明确冲突'), ('不知道','未知'), ('HR已核实：每周四天','已核实')]:
        data['chat'] = {'summary': text, 'summary_source': 'hr_manual'}
        result = normalized(data)
        totals.append(result['total_score'])
        check = result['dimensions'][-1]
        assert check['weight'] == check['score'] == 0
        assert check['eligibility']['每周出勤']['status'] == status
        assert check['eligibility']['到岗时间']['status'] == '未知'
    assert len(set(totals)) == 1


def test_model_cannot_drop_unknown_jd_clause_or_set_its_own_weights():
    data = fixture_data(); payload = _mock_evaluation(data)
    baseline = _normalize_evaluation(payload, data)
    payload['dimensions'][0].update(weight=0, applicable=False, score=100)
    assert _normalize_evaluation(payload, data)['total_score'] == baseline['total_score']
    payload['dimensions'][0]['criteria'].pop()
    with pytest.raises(ValueError, match='条款缺失'):
        _normalize_evaluation(payload, data)


@pytest.mark.parametrize('text,status', [('到岗时间：2026-11-01','自述满足'),
    ('HR已核实：到岗时间：2026-11-01','已核实'), ('到岗时间：2026-12-01','明确冲突'),
    ('到岗时间：2026-13-99','未知'), ('到岗时间：下个月','未知')])
def test_explicit_start_date_is_separate_from_score(text, status):
    data = fixture_data(); data['job']['raw_jd'] += '；到岗时间：2026-11-02'
    initial = normalized(data)['total_score']
    data['chat'] = {'summary': text, 'summary_source': 'hr_manual'}
    result = normalized(data)
    assert result['total_score'] == initial
    assert result['dimensions'][-1]['eligibility']['到岗时间']['status'] == status


def test_empty_technical_jd_cannot_produce_a_comparable_score():
    data = build_rule_parse('技能：Python', jd_text='每周四天')
    with pytest.raises(ValueError, match='JD未提供'):
        AIService(LLMClient(provider='mock')).evaluate_candidate(data)


def test_model_or_candidate_cannot_claim_hr_verified():
    data = fixture_data(); data['chat'] = {'summary': 'HR已核实：每周四天'}
    assert normalized(data)['dimensions'][-1]['eligibility']['每周出勤']['status'] == '自述满足'


def test_explicit_domain_is_active_but_not_inferred_from_school_or_candidate():
    data = fixture_data(); data['job']['raw_jd'] += '；业务领域：电商'
    contract = {item['name']: item for item in build_scoring_contract(data)}
    assert contract['domain_match']['applicable'] is True
    assert contract['domain_match']['weight'] == pytest.approx(10/70)


def test_personal_contribution_in_unrelated_project_does_not_satisfy_target_job():
    data = build_rule_parse('技能：Java\n项目：本人独立使用Java实现接口', jd_text='要求：React；说明本人负责的实现')
    assert normalized(data)['total_score'] == 0


def test_direct_online_service_also_requires_each_positive_claim_reference():
    data = fixture_data()
    class Stub:
        def generate_json(self, *, messages, fallback, validator):
            assert 'candidate:skills' in messages[-1]['content']
            payload = fallback()
            for dimension in payload['dimensions']:
                for criterion in dimension['criteria']:
                    criterion['references'] = []
            return {'content': validator(payload), 'meta': {'provider': 'stub', 'model': 'synthetic', 'fallback': False, 'fallback_reason': None}}
    with pytest.raises(ValueError, match='可定位证据'):
        AIService(Stub()).evaluate_candidate(data)


def test_mixed_attendance_cannot_silently_remove_technical_requirement():
    data = fixture_data()
    data['job']['raw_jd'] = '要求：Python接口开发，每周四天；SQL与事务'
    with pytest.raises(ValueError, match='混写'):
        build_scoring_contract(data)
    data['job']['raw_jd'] = '岗位：后端工程师，要求Python接口开发；SQL与事务'
    requirements = build_scoring_contract(data)[0]['requirements']
    assert [item['text'] for item in requirements] == ['Python接口开发', 'SQL与事务']


def test_conflicting_start_dates_remain_unknown_even_with_structured_date():
    data = fixture_data(); data['job']['raw_jd'] += '；到岗时间：2026-11-02'
    data['chat'] = {'summary': '到岗时间：2026-11-01\n到岗时间：2026-12-01', 'start_date': '2026-11-01'}
    assert normalized(data)['dimensions'][-1]['eligibility']['到岗时间']['status'] == '未知'


@pytest.mark.parametrize('quote', ['团队共处理1000次请求', '1000次'])
@pytest.mark.parametrize('separator', ['；', '，', ','])
def test_team_metric_cannot_become_personal_result_by_shortening_reference(quote, separator):
    data = fixture_data(); payload = _mock_evaluation(data)
    data['evidence_catalog'] = {'project:1': f'本人实现接口并回归验证{separator}团队共处理1000次请求'}
    entry = payload['dimensions'][0]['criteria'][0]
    entry.update(score=100, status='quantified', implementation_evidence='本人实现接口',
                 verification_evidence='回归验证', metric_evidence='1000次',
                 references=[{'id':'project:1', 'quote':'本人实现接口并回归验证'}, {'id':'project:1', 'quote':quote}])
    with pytest.raises(ValueError, match='团队量化指标'):
        _normalize_evaluation(payload, data)


def test_mock_references_point_to_catalog_and_keep_confirmed_skills_priority():
    data = fixture_data()
    data['candidate']['skills'] = ['Java']
    data['job']['raw_jd'] = '要求：Java'
    data['evidence_catalog'] = {'source:1:line:1': '技能：Python、SQL', 'facts:2:skills': '["Java"]'}
    result = AIService(LLMClient(provider='mock')).evaluate_candidate(data)
    criterion = result['dimensions'][0]['criteria'][0]
    assert criterion['score'] == 30
    assert criterion['references'] == [{'id': 'facts:2:skills', 'quote': 'Java'}]


def test_direct_mock_can_reference_implementation_without_inventing_quotes():
    data = fixture_data()
    result = AIService(LLMClient(provider='mock')).evaluate_candidate(data)
    implemented = next(c for dim in result['dimensions'] for c in dim['criteria'] if c['score'] == 70)
    assert implemented['references']
    reference = implemented['references'][0]
    assert reference['id'].startswith('candidate:')
    assert reference['quote'] in json.dumps(data['candidate'][reference['id'].split(':', 1)[1]], ensure_ascii=False)
    assert reference['quote'] == implemented['implementation_evidence']


def test_irrelevant_model_entries_cannot_change_scores_or_attendance():
    data = fixture_data()
    data['chat'] = {'summary': '每周四天'}
    raw = _mock_evaluation(data)
    baseline = _normalize_evaluation(raw, data)
    for dim in raw['dimensions'][2:]:
        dim.update(criteria=[{'id': 'invented', 'score': 100, 'status': 'quantified', 'references': [{'id':'fake', 'quote':'fake'}]}],
                   score=100, weight=1, eligibility={'每周出勤': {'status': '已核实'}})
    actual = _normalize_evaluation(raw, data)
    assert actual['total_score'] == baseline['total_score']
    assert all(d['criteria'] == [] and d['weight'] == d['score'] == 0 for d in actual['dimensions'][2:])
    assert actual['dimensions'][-1]['eligibility']['每周出勤']['status'] == '自述满足'
    raw['dimensions'][0]['criteria'].pop()
    with pytest.raises(ValueError, match='条款缺失'):
        _normalize_evaluation(raw, data)


@pytest.mark.parametrize('field', ['implementation_evidence', 'verification_evidence', 'metric_evidence'])
def test_scored_evidence_cannot_be_stitched_across_separate_quotes(field):
    data = fixture_data(); raw = _mock_evaluation(data)
    entry = raw['dimensions'][0]['criteria'][0]
    entry.update(score=100, status='quantified', implementation_evidence='本人实现接口',
                 verification_evidence='回归验证', metric_evidence='降低40%',
                 references=[{'id':'a','quote':'本人实现接口'}, {'id':'b','quote':'回归验证'}, {'id':'c','quote':'降低40%'}])
    fragments = {'implementation_evidence':'本人实现接口\n回归验证',
                 'verification_evidence':'回归验证\n降低40%', 'metric_evidence':'回归验证\n降低40%'}
    entry[field] = fragments[field]
    with pytest.raises(ValueError, match='本人实现|量化指标'):
        _normalize_evaluation(raw, data)
