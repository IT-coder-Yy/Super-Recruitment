from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import init_db, session_scope
from app.models import Application, ApplicationStatus, Evaluation, User
from app.schemas import (
    ApplicationCreate,
    CandidateCreate,
    CandidateSourceCreate,
    EvaluationCreate,
    EvaluationDimension,
    FollowUpCreate,
    InterviewCreate,
    InterviewSlotCreate,
    JobCreate,
)
from app.services.candidates import ingest_candidate
from app.services.auth import hash_password, normalize_username
from app.services.workflow import (
    add_interview_slot,
    create_application,
    create_evaluation,
    create_interview,
    get_or_create_follow_up,
    get_or_create_job_version,
    transition_application,
)


SEED_ACTOR = "seed"


def seed_database(session: Session) -> dict[str, int]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _seed_demo_user(session)

    ai_job, ai_v1, _ = get_or_create_job_version(
        session,
        JobCreate(
            platform="mock",
            source_job_id="mock-job-ai-001",
            title="AI 应用开发实习生",
            department="智能产品部",
            owner="虚构HR-林老师",
            jd_raw="负责 Python 后端、FastAPI 接口及大模型应用开发。",
            structured_jd={
                "required_skills": ["Python", "FastAPI"],
                "preferred_skills": ["SQLAlchemy", "大模型应用"],
            },
        ),
        actor=SEED_ACTOR,
    )
    ai_job, ai_v2, _ = get_or_create_job_version(
        session,
        JobCreate(
            platform="mock",
            source_job_id="mock-job-ai-001",
            title="AI 应用开发实习生",
            department="智能产品部",
            owner="虚构HR-林老师",
            jd_raw=(
                "负责 Python 后端、FastAPI 接口及大模型应用开发；"
                "要求了解 SQLAlchemy 和基础测试。"
            ),
            structured_jd={
                "required_skills": ["Python", "FastAPI", "SQLAlchemy"],
                "preferred_skills": ["大模型应用", "自动化测试"],
            },
        ),
        actor=SEED_ACTOR,
    )
    data_job, data_v1, _ = get_or_create_job_version(
        session,
        JobCreate(
            platform="mock",
            source_job_id="mock-job-data-001",
            title="数据分析实习生",
            department="业务分析部",
            owner="虚构HR-周老师",
            jd_raw="负责招聘数据清洗、SQL 分析和可视化报表。",
            structured_jd={
                "required_skills": ["SQL", "Python"],
                "preferred_skills": ["数据可视化"],
            },
        ),
        actor=SEED_ACTOR,
    )
    backend_job, backend_v1, _ = get_or_create_job_version(
        session,
        JobCreate(
            platform="mock",
            source_job_id="mock-job-backend-001",
            title="后端开发实习生",
            department="平台研发部",
            owner="虚构HR-赵老师",
            jd_raw="负责 FastAPI 服务、数据库建模、接口测试和内部自动化工具开发。",
            structured_jd={
                "required_skills": ["Python", "FastAPI", "SQL", "接口测试"],
                "preferred_skills": ["Docker", "Redis", "异步任务"],
            },
        ),
        actor=SEED_ACTOR,
    )
    qa_job, qa_v1, _ = get_or_create_job_version(
        session,
        JobCreate(
            platform="mock",
            source_job_id="mock-job-qa-001",
            title="测试开发实习生",
            department="质量工程部",
            owner="虚构HR-王老师",
            jd_raw="负责自动化测试、接口回归、测试数据构造和质量看板。",
            structured_jd={
                "required_skills": ["Python", "pytest", "接口测试"],
                "preferred_skills": ["Playwright", "CI", "测试平台"],
            },
        ),
        actor=SEED_ACTOR,
    )

    candidate_a, source_a, _ = ingest_candidate(
        session,
        candidate_data=CandidateCreate(
            name="林晓岚",
            phone="+86 138-0000-1201",
            email="xiaolan.lin@example.test",
            structured_data={
                "education": "虚构大学软件工程专业",
                "projects": ["校园智能问答助手"],
            },
        ),
        source_data=CandidateSourceCreate(
            platform="mock",
            source_candidate_id="mock-candidate-001",
            source_application_id="mock-application-001",
            channel="模拟招聘平台A",
            raw_resume="虚构简历：使用 Python、FastAPI 开发校园智能问答助手。",
            raw_chat=[{"role": "candidate", "content": "每周可实习四天。"}],
            captured_at=now - timedelta(days=3),
        ),
        actor=SEED_ACTOR,
    )
    candidate_b, source_b, _ = ingest_candidate(
        session,
        candidate_data=CandidateCreate(
            name="周言川",
            phone="139 0000 2202",
            email="yanchuan.zhou@example.test",
            structured_data={
                "education": "虚构理工学院统计学专业",
                "projects": ["招聘漏斗分析看板"],
            },
        ),
        source_data=CandidateSourceCreate(
            platform="mock",
            source_candidate_id="mock-candidate-002",
            source_application_id="mock-application-002",
            channel="模拟招聘平台B",
            raw_resume="虚构简历：使用 SQL、Python 完成招聘漏斗分析。",
            raw_chat=[{"role": "candidate", "content": "下周可开始实习。"}],
            captured_at=now - timedelta(days=2),
        ),
        actor=SEED_ACTOR,
    )
    candidate_c, source_c, _ = ingest_candidate(
        session,
        candidate_data=CandidateCreate(
            name="顾青禾",
            phone=None,
            email="qinghe.gu@example.test",
            structured_data={"education": "虚构学院计算机专业"},
        ),
        source_data=CandidateSourceCreate(
            platform="mock",
            source_candidate_id="mock-candidate-003",
            source_application_id="mock-application-003",
            channel="模拟文件导入",
            raw_resume="虚构简历：联系电话缺失，等待人工确认。",
            captured_at=now - timedelta(hours=6),
        ),
        actor=SEED_ACTOR,
    )
    candidate_d, source_d, _ = ingest_candidate(
        session,
        candidate_data=CandidateCreate(
            name="陈泊舟",
            phone="13800130004",
            email="bozhou.chen@example.test",
            structured_data={
                "human_summary": {
                    "headline": "后端方向候选人，做过 FastAPI 招聘自动化和权限模块。",
                    "skills": ["Python", "FastAPI", "SQLAlchemy", "pytest"],
                    "education": ["虚构科技大学 计算机科学 本科"],
                    "projects": [
                        {
                            "title": "招聘自动化 API 服务",
                            "description": "负责候选人、投递、评估和导出接口，补充 pytest 回归测试。",
                        }
                    ],
                    "chat_summary": "每周可实习 5 天，偏好后端和 AI 应用方向。",
                    "availability": "一周内可到岗",
                }
            },
        ),
        source_data=CandidateSourceCreate(
            platform="mock",
            source_candidate_id="mock-candidate-004",
            source_application_id="mock-application-004",
            channel="模拟招聘平台C",
            raw_resume=(
                "姓名：陈泊舟\n手机号：13800130004\n邮箱：bozhou.chen@example.test\n"
                "教育：虚构科技大学 计算机科学 本科\n"
                "技能：Python、FastAPI、SQLAlchemy、pytest\n"
                "项目：招聘自动化 API 服务，负责候选人、投递、评估和导出接口。"
            ),
            raw_chat=[{"role": "candidate", "content": "一周内可到岗，每周可实习五天。"}],
            captured_at=now - timedelta(days=1, hours=5),
        ),
        actor=SEED_ACTOR,
    )
    candidate_e, source_e, _ = ingest_candidate(
        session,
        candidate_data=CandidateCreate(
            name="许知遥",
            phone="13800130005",
            email="zhiyao.xu@example.test",
            structured_data={
                "human_summary": {
                    "headline": "测试开发候选人，熟悉 pytest、Playwright 和接口自动化。",
                    "skills": ["Python", "pytest", "Playwright", "接口测试"],
                    "education": ["虚构邮电大学 软件工程 本科"],
                    "projects": [
                        {
                            "title": "接口回归测试平台",
                            "description": "维护 120+ 接口用例，输出失败原因归因和日报。",
                        }
                    ],
                    "chat_summary": "希望参与测试平台建设，可接受两轮技术面。",
                    "availability": "两周后到岗",
                }
            },
        ),
        source_data=CandidateSourceCreate(
            platform="mock",
            source_candidate_id="mock-candidate-005",
            source_application_id="mock-application-005",
            channel="模拟招聘平台D",
            raw_resume=(
                "姓名：许知遥\n手机号：13800130005\n邮箱：zhiyao.xu@example.test\n"
                "技能：Python、pytest、Playwright、接口测试\n"
                "项目：接口回归测试平台，维护 120+ 接口用例。"
            ),
            raw_chat=[{"role": "candidate", "content": "两周后可到岗，希望做测试平台。"}],
            captured_at=now - timedelta(hours=20),
        ),
        actor=SEED_ACTOR,
    )
    candidate_f, source_f, _ = ingest_candidate(
        session,
        candidate_data=CandidateCreate(
            name="沈若宁",
            phone="13800130006",
            email="ruoning.shen@example.test",
            structured_data={
                "human_summary": {
                    "headline": "数据分析方向候选人，擅长 SQL、Python 和业务看板。",
                    "skills": ["SQL", "Python", "数据清洗", "可视化"],
                    "education": ["虚构财经大学 数据科学 本科"],
                    "projects": [
                        {
                            "title": "招聘渠道质量分析",
                            "description": "清洗多渠道投递数据，计算进入面试率、Offer 接受率和到岗率。",
                        }
                    ],
                    "chat_summary": "可实习 4 天，关注业务指标和数据质量。",
                    "availability": "下周可到岗",
                }
            },
        ),
        source_data=CandidateSourceCreate(
            platform="mock",
            source_candidate_id="mock-candidate-006",
            source_application_id="mock-application-006",
            channel="模拟招聘平台E",
            raw_resume=(
                "姓名：沈若宁\n手机号：13800130006\n邮箱：ruoning.shen@example.test\n"
                "技能：SQL、Python、数据清洗、可视化\n"
                "项目：招聘渠道质量分析，看板指标包括进入面试率、Offer 接受率和到岗率。"
            ),
            raw_chat=[{"role": "candidate", "content": "下周可到岗，每周可实习四天。"}],
            captured_at=now - timedelta(hours=12),
        ),
        actor=SEED_ACTOR,
    )

    application_a, _ = create_application(
        session,
        ApplicationCreate(
            candidate_id=candidate_a.id,
            job_id=ai_job.id,
            job_version_id=ai_v2.id,
            source_id=source_a.id,
            channel=source_a.channel,
            owner="虚构HR-林老师",
            applied_at=now - timedelta(days=3),
        ),
        actor=SEED_ACTOR,
    )
    application_b, _ = create_application(
        session,
        ApplicationCreate(
            candidate_id=candidate_b.id,
            job_id=data_job.id,
            job_version_id=data_v1.id,
            source_id=source_b.id,
            channel=source_b.channel,
            owner="虚构HR-周老师",
            applied_at=now - timedelta(days=2),
        ),
        actor=SEED_ACTOR,
    )
    application_c, _ = create_application(
        session,
        ApplicationCreate(
            candidate_id=candidate_c.id,
            job_id=ai_job.id,
            job_version_id=ai_v1.id,
            source_id=source_c.id,
            channel=source_c.channel,
            owner="虚构HR-林老师",
            applied_at=now - timedelta(hours=6),
        ),
        actor=SEED_ACTOR,
    )
    application_d, _ = create_application(
        session,
        ApplicationCreate(
            candidate_id=candidate_d.id,
            job_id=backend_job.id,
            job_version_id=backend_v1.id,
            source_id=source_d.id,
            channel=source_d.channel,
            owner="虚构HR-赵老师",
            applied_at=now - timedelta(days=1, hours=5),
        ),
        actor=SEED_ACTOR,
    )
    application_e, _ = create_application(
        session,
        ApplicationCreate(
            candidate_id=candidate_e.id,
            job_id=qa_job.id,
            job_version_id=qa_v1.id,
            source_id=source_e.id,
            channel=source_e.channel,
            owner="虚构HR-王老师",
            applied_at=now - timedelta(hours=20),
        ),
        actor=SEED_ACTOR,
    )
    application_f, _ = create_application(
        session,
        ApplicationCreate(
            candidate_id=candidate_f.id,
            job_id=data_job.id,
            job_version_id=data_v1.id,
            source_id=source_f.id,
            channel=source_f.channel,
            owner="虚构HR-周老师",
            applied_at=now - timedelta(hours=12),
        ),
        actor=SEED_ACTOR,
    )

    _advance(
        session,
        application_a,
        [
            ApplicationStatus.PARSING,
            ApplicationStatus.PARSED,
            ApplicationStatus.DEDUPLICATED,
            ApplicationStatus.EVALUATING,
            ApplicationStatus.EVALUATION_REVIEW,
        ],
    )
    _advance(
        session,
        application_b,
        [
            ApplicationStatus.PARSING,
            ApplicationStatus.PARSED,
            ApplicationStatus.DEDUPLICATED,
            ApplicationStatus.EVALUATING,
            ApplicationStatus.EVALUATION_REVIEW,
            ApplicationStatus.INTERVIEW_PENDING,
            ApplicationStatus.SCHEDULING,
            ApplicationStatus.INTERVIEW_CONFIRMED,
        ],
    )
    _advance(
        session,
        application_c,
        [ApplicationStatus.PARSING, ApplicationStatus.NEEDS_REVIEW],
    )
    _advance(
        session,
        application_d,
        [
            ApplicationStatus.PARSING,
            ApplicationStatus.PARSED,
            ApplicationStatus.DEDUPLICATED,
            ApplicationStatus.EVALUATING,
            ApplicationStatus.EVALUATION_REVIEW,
        ],
    )
    _advance(
        session,
        application_e,
        [
            ApplicationStatus.PARSING,
            ApplicationStatus.PARSED,
            ApplicationStatus.DEDUPLICATED,
            ApplicationStatus.EVALUATING,
            ApplicationStatus.EVALUATION_REVIEW,
            ApplicationStatus.INTERVIEW_PENDING,
        ],
    )
    _advance(
        session,
        application_f,
        [
            ApplicationStatus.PARSING,
            ApplicationStatus.PARSED,
            ApplicationStatus.DEDUPLICATED,
        ],
    )

    if not session.scalar(
        select(Evaluation).where(Evaluation.application_id == application_a.id)
    ):
        create_evaluation(
            session,
            application_a.id,
            EvaluationCreate(
                model="mock-evaluator-v1",
                prompt_version="seed-1",
                dimensions=[
                    EvaluationDimension(
                        name="required_skills",
                        score=88,
                        weight=0.35,
                        jd_requirement="Python、FastAPI、SQLAlchemy",
                        resume_evidence="虚构项目使用 Python 和 FastAPI",
                        confidence=0.92,
                    ),
                    EvaluationDimension(
                        name="project_evidence",
                        score=84,
                        weight=0.25,
                        jd_requirement="具有可说明的项目成果",
                        resume_evidence="完成校园智能问答助手",
                        confidence=0.88,
                    ),
                    EvaluationDimension(
                        name="experience_relevance",
                        score=78,
                        weight=0.20,
                        jd_requirement="相关开发经验",
                        resume_evidence="具备 AI 应用后端项目经验",
                        confidence=0.84,
                    ),
                    EvaluationDimension(
                        name="domain_match",
                        score=80,
                        weight=0.10,
                        jd_requirement="理解大模型应用",
                        resume_evidence="项目为智能问答场景",
                        confidence=0.82,
                    ),
                    EvaluationDimension(
                        name="communication_intention",
                        score=85,
                        weight=0.10,
                        jd_requirement="实习时间稳定",
                        resume_evidence="每周可实习四天",
                        confidence=0.90,
                    ),
                ],
                reason="虚构候选人的 Python 与 AI 应用项目匹配度较高，测试经验需面试确认。",
                missing_information=["未说明自动化测试经验"],
            ),
            actor="mock_evaluation_agent",
        )
    if not session.scalar(
        select(Evaluation).where(Evaluation.application_id == application_d.id)
    ):
        create_evaluation(
            session,
            application_d.id,
            EvaluationCreate(
                model="mock-evaluator-v1",
                prompt_version="seed-1",
                dimensions=[
                    EvaluationDimension(
                        name="required_skills",
                        score=91,
                        weight=0.35,
                        jd_requirement="Python、FastAPI、SQL、接口测试",
                        resume_evidence="项目使用 FastAPI、SQLAlchemy，并补充 pytest 回归测试",
                        confidence=0.93,
                    ),
                    EvaluationDimension(
                        name="project_evidence",
                        score=88,
                        weight=0.25,
                        jd_requirement="后端 API 和自动化工具经验",
                        resume_evidence="招聘自动化 API 服务覆盖候选人、投递、评估和导出接口",
                        confidence=0.90,
                    ),
                    EvaluationDimension(
                        name="experience_relevance",
                        score=86,
                        weight=0.20,
                        jd_requirement="数据库建模和接口测试",
                        resume_evidence="简历提到 SQLAlchemy 与 pytest",
                        confidence=0.88,
                    ),
                    EvaluationDimension(
                        name="domain_match",
                        score=82,
                        weight=0.10,
                        jd_requirement="内部自动化工具开发",
                        resume_evidence="项目为招聘自动化业务场景",
                        confidence=0.84,
                    ),
                    EvaluationDimension(
                        name="communication_intention",
                        score=90,
                        weight=0.10,
                        jd_requirement="实习时间稳定",
                        resume_evidence="每周可实习五天，一周内可到岗",
                        confidence=0.92,
                    ),
                ],
                reason="后端能力和 Demo 业务场景匹配度高，可进入技术面重点确认工程质量。",
                missing_information=["未明确 Docker 或异步任务经验"],
            ),
            actor="mock_evaluation_agent",
        )

    interview, _ = create_interview(
        session,
        application_b.id,
        InterviewCreate(
            round=1,
            interviewers=[
                {
                    "name": "虚构面试官-陈老师",
                    "email": "interviewer.chen@example.test",
                }
            ],
            booking_token="seed-demo-booking-token-not-for-production",
            booking_token_expires_at=now + timedelta(days=2),
        ),
        actor=SEED_ACTOR,
    )
    for day_offset, hour in ((1, 10), (1, 14), (2, 15)):
        start_at = (now + timedelta(days=day_offset)).replace(
            hour=hour, minute=0, second=0
        )
        add_interview_slot(
            session,
            interview.id,
            InterviewSlotCreate(
                start_at=start_at,
                end_at=start_at + timedelta(minutes=45),
            ),
            actor=SEED_ACTOR,
        )

    get_or_create_follow_up(
        session,
        FollowUpCreate(
            target_type="application",
            target_id=application_c.id,
            rule_code="candidate_identity_needs_review",
            window_key="seed-open-window",
            owner="虚构HR-林老师",
            due_at=now + timedelta(hours=2),
            reason="候选人手机号缺失，无法自动查重。",
            suggested_action="联系候选人补充手机号并人工确认身份。",
            reminder_draft="请处理待人工查重的虚构候选人记录。",
            next_check_at=now + timedelta(hours=4),
        ),
        actor="mock_follow_up_agent",
    )

    session.flush()
    return {
        "jobs": 4,
        "job_versions": 5,
        "candidates": 6,
        "applications": 6,
    }


def _seed_demo_user(session: Session) -> None:
    normalized = normalize_username("demo-admin")
    if session.scalar(select(User).where(User.normalized_username == normalized)):
        return
    session.add(
        User(
            username="demo-admin",
            normalized_username=normalized,
            password_hash=hash_password("demo123456"),
        )
    )
    session.flush()


def _advance(
    session: Session,
    application: Application,
    targets: list[ApplicationStatus],
) -> None:
    for target in targets:
        if application.status == target:
            continue
        if target not in {
            item for item in _remaining_path(application.status, targets)
        }:
            continue
        transition_application(
            session,
            application.id,
            target,
            actor=SEED_ACTOR,
            is_human_action=True,
            reason="虚构种子流程",
        )


def _remaining_path(
    current: ApplicationStatus,
    targets: list[ApplicationStatus],
) -> list[ApplicationStatus]:
    if current == ApplicationStatus.CAPTURED:
        return targets
    try:
        current_index = targets.index(current)
    except ValueError:
        return []
    return targets[current_index + 1 :]


def main() -> None:
    init_db()
    with session_scope() as session:
        summary = seed_database(session)
    print(json.dumps({"seed_ready": True, **summary}, ensure_ascii=True))


if __name__ == "__main__":
    main()
