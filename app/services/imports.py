from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.ai.service import AIService
from app.config import ROOT_DIR, UPLOAD_DIR
from app.models import (
    Application,
    Candidate,
    ImportFile,
    ImportFileStatus,
    ImportTask,
    ImportTaskStatus,
    ParseRunStatus,
    ResumeParseRun,
    utc_now,
)
from app.schemas import ApplicationCreate, CandidateCreate, CandidateSourceCreate, JobCreate
from app.services.candidates import find_duplicate_candidate, ingest_candidate
from app.services.workflow import create_application, get_or_create_job_version, write_audit_log


SUPPORTED_EXTENSIONS = {"txt", "md", "pdf", "docx"}
PARSER_VERSION = "local-rule-1.0"


@dataclass(frozen=True)
class StoredUpload:
    original_filename: str
    stored_filename: str
    file_path: Path
    file_type: str
    content_type: str | None
    size_bytes: int


def store_upload_bytes(
    *,
    original_filename: str,
    content: bytes,
    content_type: str | None,
) -> StoredUpload:
    extension = _extension(original_filename)
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError("仅支持 txt、md、pdf、docx 文件")
    if not content:
        raise ValueError("上传文件不能为空")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored_filename = f"{uuid.uuid4().hex}.{extension}"
    file_path = UPLOAD_DIR / stored_filename
    file_path.write_bytes(content)
    return StoredUpload(
        original_filename=Path(original_filename).name or stored_filename,
        stored_filename=stored_filename,
        file_path=file_path,
        file_type=extension,
        content_type=content_type,
        size_bytes=len(content),
    )


def create_file_import_task(
    session: Session,
    *,
    uploads: list[StoredUpload],
    operator_id: int | None,
    job_title: str | None = None,
    jd_text: str | None = None,
    channel: str | None = None,
    owner: str | None = None,
) -> ImportTask:
    if not uploads:
        raise ValueError("至少需要上传一个文件")

    task = ImportTask(
        source_type="file",
        status=ImportTaskStatus.UPLOADED,
        operator_id=operator_id,
        job_title=_clean(job_title, limit=200),
        jd_text=_clean(jd_text, limit=10000),
        channel=_clean(channel, limit=100) or "本地上传",
        owner=_clean(owner, limit=120) or "HR",
        summary_json={
            "file_count": len(uploads),
            "supported_types": sorted(SUPPORTED_EXTENSIONS),
            "message": "文件已上传，尚未解析；点击开始解析后才会生成入库预览。",
        },
    )
    session.add(task)
    session.flush()
    for upload in uploads:
        session.add(
            ImportFile(
                task_id=task.id,
                original_filename=upload.original_filename,
                stored_filename=upload.stored_filename,
                file_path=str(upload.file_path.relative_to(ROOT_DIR)),
                file_type=upload.file_type,
                content_type=upload.content_type,
                size_bytes=upload.size_bytes,
            )
        )
    session.flush()
    write_audit_log(
        session,
        actor="本地管理员",
        action="import_task.created",
        entity_type="import_task",
        entity_id=task.id,
        after_data={
            "source_type": task.source_type,
            "file_count": len(uploads),
            "status": task.status.value,
        },
    )
    return task


def parse_import_task(session: Session, task_id: int) -> ImportTask:
    task = _get_task(session, task_id)
    if task.status == ImportTaskStatus.CONFIRMED:
        raise ValueError("已入库的导入任务不能重复解析")
    if task.status == ImportTaskStatus.REJECTED:
        raise ValueError("已废弃的导入任务不能解析")

    task.status = ImportTaskStatus.EXTRACTING_TEXT
    task.error_message = None
    session.flush()

    raw_text_parts: list[str] = []
    for item in task.files:
        try:
            text = extract_text_from_file(ROOT_DIR / item.file_path, item.file_type)
        except Exception as exc:  # noqa: BLE001 - 需要把解析失败写回任务，供 HR 处理
            item.extract_status = ImportFileStatus.FAILED
            item.error_message = str(exc)
            task.status = ImportTaskStatus.FAILED
            task.error_message = f"{item.original_filename} 文本提取失败：{exc}"
            _record_parse_run(
                session,
                task_id=task.id,
                parser_name="text_extractor",
                status=ParseRunStatus.FAILED,
                error_message=task.error_message,
            )
            session.flush()
            return task
        item.raw_text = text
        item.raw_text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        item.extract_status = ImportFileStatus.EXTRACTED
        raw_text_parts.append(f"【{item.original_filename}】\n{text}")

    raw_text = "\n\n".join(raw_text_parts).strip()
    if not raw_text:
        task.status = ImportTaskStatus.FAILED
        task.error_message = "未提取到可解析文本；扫描版 PDF/OCR 暂标注为待接入。"
        _record_parse_run(
            session,
            task_id=task.id,
            parser_name="text_extractor",
            status=ParseRunStatus.FAILED,
            error_message=task.error_message,
        )
        session.flush()
        return task

    _record_parse_run(
        session,
        task_id=task.id,
        parser_name="text_extractor",
        status=ParseRunStatus.SUCCESS,
        confidence=0.9,
        output_json={"text_length": len(raw_text), "file_count": len(task.files)},
    )

    task.status = ImportTaskStatus.RULE_PARSING
    partial = build_rule_parse(raw_text, job_title=task.job_title, jd_text=task.jd_text)
    _record_parse_run(
        session,
        task_id=task.id,
        parser_name="resume_rule_parser",
        status=ParseRunStatus.SUCCESS,
        confidence=partial["quality"]["confidence"],
        output_json=partial,
    )

    task.status = ImportTaskStatus.AI_NORMALIZING
    try:
        structured = AIService().structure_candidate(
            raw_text,
            "",
            task.jd_text or task.job_title or "",
        )
        structured["human_summary"] = build_human_summary(structured, raw_text=raw_text)
        _record_parse_run(
            session,
            task_id=task.id,
            parser_name="resume_llm_normalizer",
            status=ParseRunStatus.SUCCESS,
            confidence=structured.get("quality", {}).get("confidence"),
            output_json=structured,
        )
    except Exception as exc:  # noqa: BLE001 - AI 不可用时必须降级
        structured = partial
        structured["human_summary"] = build_human_summary(structured, raw_text=raw_text)
        structured.setdefault("quality", {}).setdefault("warnings", []).append(
            f"AI 修复不可用，已降级到本地规则解析：{exc}"
        )
        _record_parse_run(
            session,
            task_id=task.id,
            parser_name="resume_llm_normalizer",
            status=ParseRunStatus.FAILED,
            error_message=str(exc),
        )

    candidate = structured.get("candidate", {})
    duplicate = _duplicate_preview(
        find_duplicate_candidate(
            session,
            name=candidate.get("name"),
            phone=candidate.get("phone"),
        )
    )
    task.preview_json = structured
    task.duplicate_json = duplicate
    task.summary_json = {
        "human_summary": structured.get("human_summary", {}),
        "missing_fields": structured.get("quality", {}).get("missing_fields", []),
        "warnings": structured.get("quality", {}).get("warnings", []),
        "confidence": structured.get("quality", {}).get("confidence"),
    }
    task.status = ImportTaskStatus.NEEDS_REVIEW
    session.flush()
    return task


def confirm_import_task(
    session: Session,
    task_id: int,
    *,
    start_evaluation: bool = False,
) -> Application:
    task = _get_task(session, task_id)
    if task.status == ImportTaskStatus.CONFIRMED and task.confirmed_application_id:
        application = session.get(Application, task.confirmed_application_id)
        if application is None:
            raise ValueError("已确认任务关联的投递记录不存在")
        return application
    if task.status != ImportTaskStatus.NEEDS_REVIEW or not task.preview_json:
        raise ValueError("请先完成解析预览，再确认入库")

    structured = dict(task.preview_json)
    candidate_data = structured.get("candidate") or {}
    name = _clean(candidate_data.get("name"), limit=100) or "待补充姓名"
    job_data = structured.get("job") or {}
    job_title = (
        _clean(task.job_title, limit=200)
        or _clean(job_data.get("title"), limit=200)
        or "待补充岗位"
    )
    jd_raw = task.jd_text or _build_jd_text(job_title, job_data)
    raw_text = "\n\n".join(file.raw_text or "" for file in task.files).strip()

    candidate, source, _ = ingest_candidate(
        session,
        candidate_data=CandidateCreate(
            name=name,
            phone=_clean(candidate_data.get("phone"), limit=40),
            email=_clean(candidate_data.get("email"), limit=255),
            structured_data=structured,
        ),
        source_data=CandidateSourceCreate(
            platform="file",
            source_candidate_id=f"import-candidate-{task.id}",
            source_application_id=f"import-task-{task.id}",
            channel=task.channel or "本地上传",
            raw_payload={
                "import_task_id": task.id,
                "files": [
                    {
                        "name": item.original_filename,
                        "type": item.file_type,
                        "size": item.size_bytes,
                    }
                    for item in task.files
                ],
            },
            raw_resume=raw_text,
            raw_chat=[],
            captured_at=utc_now(),
        ),
        actor="本地导入",
    )
    if not candidate.structured_data:
        candidate.structured_data = structured

    job, job_version, _ = get_or_create_job_version(
        session,
        JobCreate(
            platform="file",
            source_job_id=f"local-{_slug(job_title)}",
            title=job_title,
            department=None,
            owner=task.owner,
            jd_raw=jd_raw,
            structured_jd=job_data,
        ),
        actor="本地导入",
    )
    application, _ = create_application(
        session,
        ApplicationCreate(
            candidate_id=candidate.id,
            job_id=job.id,
            job_version_id=job_version.id,
            source_id=source.id,
            channel=task.channel or "本地上传",
            owner=task.owner or "HR",
            applied_at=utc_now(),
            extra_data={
                "import_task_id": task.id,
                "start_evaluation_requested": start_evaluation,
            },
        ),
        actor="本地导入",
    )

    task.status = ImportTaskStatus.CONFIRMED
    task.confirmed_application_id = application.id
    write_audit_log(
        session,
        actor="本地管理员",
        action="import_task.confirmed",
        entity_type="import_task",
        entity_id=task.id,
        after_data={
            "candidate_id": candidate.id,
            "application_id": application.id,
            "start_evaluation_requested": start_evaluation,
        },
    )
    session.flush()
    return application


def reject_import_task(session: Session, task_id: int) -> ImportTask:
    task = _get_task(session, task_id)
    if task.status == ImportTaskStatus.CONFIRMED:
        raise ValueError("已入库的导入任务不能废弃")
    task.status = ImportTaskStatus.REJECTED
    write_audit_log(
        session,
        actor="本地管理员",
        action="import_task.rejected",
        entity_type="import_task",
        entity_id=task.id,
        after_data={"status": task.status.value},
    )
    session.flush()
    return task


def import_task_rows(session: Session, *, limit: int = 50) -> list[dict[str, Any]]:
    tasks = session.scalars(
        select(ImportTask).order_by(desc(ImportTask.created_at)).limit(limit)
    ).unique().all()
    return [serialize_import_task(task, include_detail=False) for task in tasks]


def serialize_import_task(
    task: ImportTask,
    *,
    include_detail: bool = True,
) -> dict[str, Any]:
    candidate = (task.preview_json or {}).get("candidate", {})
    quality = (task.preview_json or {}).get("quality", {})
    row: dict[str, Any] = {
        "id": task.id,
        "status": task.status.value,
        "status_label": IMPORT_STATUS_LABELS.get(task.status.value, task.status.value),
        "job_title": task.job_title or (task.preview_json or {}).get("job", {}).get("title") or "待补充岗位",
        "channel": task.channel or "本地上传",
        "owner": task.owner or "HR",
        "candidate_name": candidate.get("name") or "待解析",
        "phone": candidate.get("phone") or "待解析",
        "confidence": quality.get("confidence"),
        "missing_fields": quality.get("missing_fields") or [],
        "warnings": quality.get("warnings") or [],
        "duplicate": task.duplicate_json or {"status": "unchecked", "label": "待查重"},
        "files": [
            {
                "id": item.id,
                "name": item.original_filename,
                "type": item.file_type,
                "size_bytes": item.size_bytes,
                "status": item.extract_status.value,
                "error_message": item.error_message,
            }
            for item in task.files
        ],
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "confirmed_application_id": task.confirmed_application_id,
    }
    if include_detail:
        row.update(
            {
                "preview": task.preview_json,
                "summary": task.summary_json,
                "parse_runs": [
                    {
                        "parser_name": run.parser_name,
                        "status": run.status.value,
                        "confidence": run.confidence,
                        "error_message": run.error_message,
                        "output_json": run.output_json,
                        "created_at": run.created_at,
                    }
                    for run in task.parse_runs
                ],
            }
        )
    return row


def extract_text_from_file(path: Path, file_type: str) -> str:
    if file_type in {"txt", "md"}:
        return _read_text(path)
    if file_type == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给明确错误
            raise RuntimeError("缺少 pypdf 依赖，无法解析 PDF") from exc
        reader = PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return text.strip()
    if file_type == "docx":
        try:
            from docx import Document
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("缺少 python-docx 依赖，无法解析 DOCX") from exc
        document = Document(str(path))
        return "\n".join(paragraph.text for paragraph in document.paragraphs).strip()
    raise ValueError("不支持的文件类型")


def build_rule_parse(
    raw_text: str,
    *,
    job_title: str | None = None,
    jd_text: str | None = None,
) -> dict[str, Any]:
    phone = _first_match(raw_text, r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")
    email = _first_match(raw_text, r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
    name = _labeled_value(raw_text, ("姓名", "候选人", "Name"))
    if not name:
        name = _guess_name(raw_text)
    skills = _extract_known_skills(raw_text)
    education = _section_lines(raw_text, ("教育", "学历", "大学", "学院", "本科", "硕士", "博士"), limit=4)
    projects = _section_lines(raw_text, ("项目", "系统", "平台", "Project"), limit=6)
    internships = _section_lines(raw_text, ("实习", "Intern"), limit=4)
    works = _section_lines(raw_text, ("工作经历", "任职", "有限公司", "公司"), limit=4)
    missing = []
    if not name:
        missing.append("candidate.name")
    if not phone:
        missing.append("candidate.phone")
    if not skills:
        missing.append("candidate.skills")
    if not job_title and not jd_text:
        missing.append("job.title")

    evidence_count = sum(bool(value) for value in (name, phone, email, skills, education, projects, internships, works))
    confidence = min(0.35 + evidence_count * 0.07, 0.88)
    return {
        "schema_version": "1.0",
        "candidate": {
            "name": name,
            "phone": phone,
            "email": email,
            "summary": _snippet(raw_text, 500),
            "skills": skills,
            "education": [{"evidence": line, "school": None, "major": None, "degree": None} for line in education],
            "project_experience": [{"name": None, "role": None, "description": line, "evidence": line} for line in projects],
            "internship_experience": [{"company": None, "role": None, "description": line, "evidence": line} for line in internships],
            "work_experience": [{"company": None, "role": None, "description": line, "evidence": line} for line in works],
        },
        "chat": {
            "summary": None,
            "intent": None,
            "availability": None,
            "start_date": None,
            "evidence": [],
        },
        "job": {
            "title": job_title,
            "responsibilities": _section_lines(jd_text or "", ("负责", "职责", "工作内容"), limit=5),
            "required_skills": _extract_known_skills(jd_text or ""),
            "preferred_skills": _section_lines(jd_text or "", ("优先", "加分"), limit=5),
            "experience_requirements": _section_lines(jd_text or "", ("经验", "经历"), limit=5),
            "education_requirements": _section_lines(jd_text or "", ("学历", "本科", "硕士", "博士"), limit=5),
            "domain": None,
            "other_requirements": [],
        },
        "quality": {
            "confidence": round(confidence, 2),
            "missing_fields": missing,
            "warnings": ["本地规则解析结果，入库前请人工确认"],
        },
    }


def build_human_summary(
    structured: dict[str, Any],
    *,
    raw_text: str = "",
) -> dict[str, Any]:
    candidate = structured.get("candidate") or {}
    chat = structured.get("chat") or {}
    quality = structured.get("quality") or {}
    projects = _experience_summary(candidate.get("project_experience") or [], key="name")
    internships = _experience_summary(candidate.get("internship_experience") or [], key="company")
    works = _experience_summary(candidate.get("work_experience") or [], key="company")
    education = [
        _compact_join(item.get("school"), item.get("degree"), item.get("major"), item.get("evidence"))
        for item in candidate.get("education") or []
        if isinstance(item, dict)
    ]
    return {
        "headline": candidate.get("summary") or _snippet(raw_text, 240) or "暂无简历摘要",
        "contact": {
            "phone": candidate.get("phone") or "待补充",
            "email": candidate.get("email") or "待补充",
        },
        "skills": candidate.get("skills") or [],
        "education": [item for item in education if item][:5],
        "projects": projects,
        "internships": internships,
        "work_experience": works,
        "chat_summary": chat.get("summary") or "本地上传未提供聊天记录",
        "availability": chat.get("availability") or "待人工确认",
        "missing_fields": quality.get("missing_fields") or [],
        "warnings": quality.get("warnings") or [],
    }


IMPORT_STATUS_LABELS = {
    "uploaded": "已上传",
    "extracting_text": "提取文本中",
    "rule_parsing": "规则解析中",
    "ai_normalizing": "AI 修复中",
    "needs_review": "待人工确认",
    "confirmed": "已入库",
    "rejected": "已废弃",
    "failed": "解析失败",
}


def _get_task(session: Session, task_id: int) -> ImportTask:
    task = session.get(ImportTask, task_id)
    if task is None:
        raise ValueError("导入任务不存在")
    return task


def _record_parse_run(
    session: Session,
    *,
    task_id: int,
    parser_name: str,
    status: ParseRunStatus,
    confidence: float | None = None,
    output_json: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> None:
    session.add(
        ResumeParseRun(
            task_id=task_id,
            parser_name=parser_name,
            parser_version=PARSER_VERSION,
            status=status,
            confidence=confidence,
            output_json=output_json,
            error_message=error_message,
        )
    )
    session.flush()


def _duplicate_preview(candidate: Candidate | None) -> dict[str, Any]:
    if candidate is None:
        return {"status": "none", "label": "未发现重复候选人"}
    return {
        "status": "matched",
        "label": "疑似重复，确认后将复用候选人主记录",
        "candidate_id": candidate.id,
        "name": candidate.name,
        "phone": candidate.phone,
    }


def _read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding).strip()
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore").strip()


def _extension(filename: str) -> str:
    return Path(filename).suffix.lower().lstrip(".")


def _clean(value: Any, *, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _first_match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(0).strip() if match else None


def _labeled_value(text: str, labels: tuple[str, ...]) -> str | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?:^|\n)\s*(?:{label_pattern})\s*[:：]\s*([^\n]+)", text, re.IGNORECASE)
    if not match:
        return None
    value = match.group(1).strip()
    return value[:60] if value else None


def _guess_name(text: str) -> str | None:
    for line in text.splitlines()[:8]:
        clean = line.strip(" \t-—|：:")
        if 2 <= len(clean) <= 8 and not re.search(r"\d|@|电话|手机|邮箱|简历", clean):
            return clean
    return None


def _extract_known_skills(text: str) -> list[str]:
    skills = [
        "Python",
        "Java",
        "JavaScript",
        "TypeScript",
        "React",
        "Vue",
        "FastAPI",
        "Django",
        "Flask",
        "SQL",
        "MySQL",
        "PostgreSQL",
        "Redis",
        "Docker",
        "Kubernetes",
        "Linux",
        "PyTorch",
        "TensorFlow",
        "机器学习",
        "深度学习",
        "大模型",
        "RAG",
        "Agent",
        "数据分析",
        "Excel",
    ]
    lower = text.casefold()
    return [skill for skill in skills if skill.casefold() in lower]


def _section_lines(text: str, keywords: tuple[str, ...], *, limit: int) -> list[str]:
    result: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip(" \t-•")
        if len(line) < 4:
            continue
        if any(keyword.casefold() in line.casefold() for keyword in keywords):
            result.append(line[:500])
        if len(result) >= limit:
            break
    return result


def _snippet(text: str, limit: int) -> str | None:
    compact = " ".join(text.split())
    return compact[:limit] if compact else None


def _experience_summary(items: list[dict[str, Any]], *, key: str) -> list[dict[str, str]]:
    result = []
    for item in items[:6]:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "title": _compact_join(item.get(key), item.get("role")) or "未命名经历",
                "description": item.get("description") or item.get("evidence") or "待补充描述",
                "evidence": item.get("evidence") or item.get("description") or "",
            }
        )
    return result


def _compact_join(*values: Any) -> str:
    return " / ".join(str(value).strip() for value in values if value)


def _build_jd_text(job_title: str, job_data: dict[str, Any]) -> str:
    parts = [f"岗位：{job_title}"]
    for key in ("responsibilities", "required_skills", "preferred_skills", "experience_requirements", "education_requirements"):
        values = job_data.get(key) or []
        if values:
            parts.append(f"{key}：" + "；".join(str(item) for item in values))
    return "\n".join(parts)


def _slug(value: str) -> str:
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return digest
