from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config import ROOT_DIR, UPLOAD_DIR, get_setting
from app.models import (
    Application,
    Candidate,
    ImportFile,
    ImportFileStatus,
    ImportTask,
    ImportTaskStatus,
    ParseRunStatus,
    ResumeParseRun,
    Job,
    utc_now,
)
from app.schemas import ApplicationCreate, CandidateCreate, CandidateSourceCreate, JobCreate
from app.services.candidates import find_duplicate_candidate, ingest_candidate, validate_identity
from copy import deepcopy
from app.services.workflow import create_application, get_or_create_job_version, write_audit_log, lock_record


SUPPORTED_EXTENSIONS = {"txt", "md", "pdf", "docx"}
PARSER_VERSION = "local-rule-2.1"


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
    if len(content) > int(get_setting("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024))):
        raise ValueError("文件超过允许的大小")

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
    job_id: int | None = None,
    recruitment_type: str = "autumn",
    new_job_source_id: str | None = None,
) -> ImportTask:
    if len(uploads) != 1:
        raise ValueError("每个导入任务只能包含一份简历")
    job = session.get(Job, job_id) if job_id is not None else None
    if job_id is not None and job is None:
        raise ValueError("所选岗位不存在")
    recruitment_type = job.recruitment_type if job else recruitment_type
    if recruitment_type not in {"autumn", "internship"}:
        raise ValueError("请选择秋招或实习招聘类型")

    task = ImportTask(
        source_type="file",
        status=ImportTaskStatus.UPLOADED,
        operator_id=operator_id,
        job_title=_clean(job_title, limit=200),
        jd_text=_clean(jd_text, limit=10000),
        channel=_clean(channel, limit=100) or "本地上传",
        owner=_clean(owner, limit=120) or "HR",
        summary_json={
            "job_id": job_id,
            "recruitment_type": recruitment_type,
            "new_job_source_id": new_job_source_id if job_id is None else None,
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
                file_path=str(upload.file_path.resolve()),
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
    task = lock_record(session, ImportTask, task_id)
    if task.status == ImportTaskStatus.CONFIRMED:
        raise ValueError("已入库的导入任务不能重复解析")
    if task.status == ImportTaskStatus.REJECTED:
        raise ValueError("已废弃的导入任务不能解析")
    if len(task.files) != 1:
        raise ValueError("历史多文件任务只读，请按一人一份重新导入")
    previous = deepcopy(task.preview_json or {})
    metadata = dict(task.summary_json or {})

    task.status = ImportTaskStatus.EXTRACTING_TEXT
    task.error_message = None
    session.flush()

    raw_text_parts: list[str] = []
    for item in task.files:
        try:
            text = extract_text_from_file(ROOT_DIR / item.file_path, item.file_type)
            if not text.strip():
                raise ValueError("未提取到可解析文本；扫描 PDF 暂不支持 OCR，请补充文本文件")
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
        raw_text_parts.append(text)

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

    structured = previous if previous.get("manual_fields") else partial
    structured["revision"] = int(previous.get("revision", 0)) + 1
    structured["human_summary"] = build_human_summary(structured, raw_text=raw_text)

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
        **metadata,
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
    version: int | None = None,
) -> Application:
    task = lock_record(session, ImportTask, task_id)
    if task.status == ImportTaskStatus.CONFIRMED and task.confirmed_application_id:
        application = session.get(Application, task.confirmed_application_id)
        if application is None:
            raise ValueError("已确认任务关联的投递记录不存在")
        return application
    if task.status != ImportTaskStatus.NEEDS_REVIEW or not task.preview_json:
        raise ValueError("请先完成解析预览，再确认入库")
    if version is not None and version != task.preview_json.get("revision", 0):
        raise ValueError("预览已更新，请刷新后确认")

    structured = dict(task.preview_json)
    structured["confirmed"] = True
    candidate_data = structured.get("candidate") or {}
    validate_identity(candidate_data)
    name = candidate_data["name"]
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

    selected_job_id = (task.summary_json or {}).get("job_id")
    new_job_source_id = (task.summary_json or {}).get("new_job_source_id")
    job = session.get(Job, selected_job_id) if selected_job_id else None
    if not selected_job_id and new_job_source_id:
        job = session.scalar(select(Job).where(Job.platform == "file", Job.source_job_id == new_job_source_id))
    if job:
        job_version = next((version for version in reversed(job.versions) if version.is_current), None)
        if job_version is None:
            raise ValueError("所选岗位没有有效 JD 版本")
    else:
        job, job_version, _ = get_or_create_job_version(
        session,
        JobCreate(
            platform="file",
            source_job_id=new_job_source_id or f"local-import-{task.id}",
            title=job_title,
            recruitment_type=(task.summary_json or {}).get("recruitment_type", "autumn"),
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


def supplement_import_task(session: Session, task_id: int, changes: dict[str, Any], *, version: int) -> ImportTask:
    task = lock_record(session, ImportTask, task_id)
    if task.status != ImportTaskStatus.NEEDS_REVIEW or not task.preview_json:
        raise ValueError("请先解析并打开待核对的预览")
    before = deepcopy(task.preview_json)
    if version != int(before.get("revision", 0)):
        raise ValueError("预览已更新，请刷新后再保存")
    updated = deepcopy(before)
    for key in ("name", "phone", "email"):
        if key in changes:
            updated["candidate"][key] = str(changes[key]).strip()
    if "skills" in changes:
        updated["candidate"]["skills"] = [v.strip() for v in re.split(r"[\n,，;；]", changes["skills"]) if v.strip()]
    if "experience" in changes:
        updated["candidate"]["project_experience"] = [{"name": "人工确认经历", "description": v.strip(), "evidence": v.strip()}
                                                       for v in changes["experience"].splitlines() if v.strip()]
    updated["revision"] = version + 1
    updated["manual_fields"] = sorted(set(updated.get("manual_fields", [])) | set(changes))
    missing = [f"candidate.{key}" for key in ("name", "phone", "email") if not updated["candidate"].get(key)]
    updated.setdefault("quality", {})["missing_fields"] = missing
    updated["human_summary"] = build_human_summary(updated)
    task.preview_json = updated
    task.duplicate_json = _duplicate_preview(find_duplicate_candidate(session, name=updated["candidate"].get("name"), phone=updated["candidate"].get("phone")))
    task.summary_json = {**(task.summary_json or {}), "missing_fields": missing, "human_summary": updated["human_summary"]}
    write_audit_log(session, actor="HR", action="import_task.supplemented", entity_type="import_task", entity_id=task.id,
                    before_data=before, after_data=updated)
    session.flush()
    return task


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
        "recruitment_type": (task.summary_json or {}).get("recruitment_type", "autumn"),
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
        from docx.table import Table
        parts = []
        for block in document.iter_inner_content():
            if isinstance(block, Table):
                parts.extend("\t".join(cell.text for cell in row.cells) for row in block.rows)
            else:
                parts.append(block.text)
        return "\n".join(parts).strip()
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
    projects = _experience_lines(raw_text, ("项目", "项目经历", "项目经验", "Project", "Projects"), limit=6)
    internships = _experience_lines(raw_text, ("实习", "实习经历", "Internship", "Internships"), limit=4)
    works = _experience_lines(raw_text, ("工作", "工作经历", "任职", "Work Experience"), limit=4)
    missing = []
    if not name:
        missing.append("candidate.name")
    if not phone:
        missing.append("candidate.phone")
    if not email:
        missing.append("candidate.email")
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
            "other_requirements": _section_lines(jd_text or "", ("要求", "requirement"), limit=10),
        },
        "quality": {
            "confidence": round(confidence, 2),
            "missing_fields": missing,
            "warnings": ["本地规则仅提取明确标签与肯定技能；未识别或有歧义的经历保留在原文，入库前请人工确认"],
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
    # 仅把首个非空行的简单姓名作为待人工核对的猜测，不从项目标题造姓名。
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if re.fullmatch(r"[\u4e00-\u9fff]{2,5}", first) and not re.search(r"姓名|电话|手机|邮箱|简历|项目|技能|教育|个人|信息", first):
        return first
    return None


def _extract_known_skills(text: str) -> list[str]:
    skills = [
        "Python",
        "Java",
        "JavaScript",
        "TypeScript",
        "CSS",
        "React",
        "Vue",
        "FastAPI",
        "Django",
        "Flask",
        "Spring Boot",
        "pytest",
        "JUnit",
        "Playwright",
        "SQL",
        "MySQL",
        "PostgreSQL",
        "SQLite",
        "Redis",
        "Docker",
        "Kubernetes",
        "Linux",
        "Git",
        "PyTorch",
        "TensorFlow",
        "机器学习",
        "深度学习",
        "大模型",
        "RAG",
        "LLM",
        "LangChain",
        "自然语言处理",
        "向量检索",
        "Agent",
        "数据分析",
        "Excel",
    ]
    positive = "\n".join(_positive_fragments(text))
    return [skill for skill in skills if _contains_term(positive, skill)]


def _contains_term(text: str, term: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?:\d+(?:\.\d+)*)?(?![A-Za-z0-9_])", text, re.IGNORECASE))


def _positive_fragments(text: str) -> list[str]:
    # shortcut: 仅处理明确否定、意向和他人归属；复杂语义保留原文交HR核对。
    result = []
    for line in text.splitlines():
        if re.match(r"\s*(?:声明|意向|求职|应聘|岗位|职位|目标|计划)\s*[:：\t]", line):
            continue
        for fragment in re.split(r"[，,；;。!?！？\n]|但是|但|然而|\bbut\b", line, flags=re.IGNORECASE):
            if re.search(r"未|没有|不(?:会|熟|了解|掌握|具备|使用|懂|涉及)|无(?!障碍)|队友|他人|同事|非本人|计划|希望|打算|\b(?:not|no|never|without|teammate)\b", fragment, re.IGNORECASE):
                continue
            if fragment.strip():
                result.append(fragment.strip())
    return result


def _experience_lines(text: str, labels: tuple[str, ...], *, limit: int) -> list[str]:
    label = "|".join(re.escape(value) for value in labels)
    result: list[str] = []
    in_section = False
    for raw in text.splitlines():
        line = raw.strip(" \t-•#")
        match = re.match(rf"^(?:{label})(?:\s*[:：\t]\s*(.*)|\s*)$", line, re.IGNORECASE)
        if match:
            content = (match.group(1) or "").strip()
            in_section = not content
        elif re.match(r"^[\w /]{1,24}[:：\t]", line) or re.fullmatch(r".*(?:经历|经验|技能|教育|意向)", line):
            in_section = False
            continue
        elif in_section:
            content = line
        else:
            continue
        first_clause = re.split(r"[，,；;。]", content)[0]
        if not content or not _positive_fragments(first_clause):
            continue
        if _positive_fragments(content) and line[:500] not in result:
            result.append(line[:500])
        if len(result) >= limit:
            break
    return result


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
