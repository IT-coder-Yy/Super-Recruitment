"""在新隔离目录回放12×2样本；默认Mock，真实模型仅使用显式专用配置。"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import socket
import sys
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "tests/fixtures/demo"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-benchmark", action="store_true", help="按已签v2基准检查；差异先保存再以1退出")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-online", action="store_true", help="仅准备24组实际请求内容，禁止联网、不评分")
    mode.add_argument("--online-config", type=Path, help="使用此专用.env执行真实模型24组评估；每组最多一次、不重试")
    args = parser.parse_args()
    output = ROOT / ".test-runs" / f"samples-{uuid.uuid4().hex}"
    output.mkdir(parents=True)
    os.environ.update(APP_DATA_DIR=str(output), APP_RUNTIME_ENV=str(output / "runtime.env"),
        DATABASE_URL=f"sqlite:///{(output / 'demo.db').as_posix()}", APP_SECRET_KEY="synthetic-benchmark-secret",
        LLM_PROVIDER="mock", LLM_BASE_URL="", LLM_API_KEY="", LLM_MODEL="",
        BASE_URL="", API_KEY="", MODEL="", SMTP_HOST="", SMTP_PASSWORD="", WECOM_WEBHOOK_URL="",
        FOLLOWUP_ENABLED="false")

    def deny_network(*args, **kwargs):
        raise AssertionError("此样本回放禁止外呼")
    if not args.online_config:
        socket.create_connection = deny_network
        socket.socket.connect = deny_network
    sys.path.insert(0, str(ROOT))
    from scripts.demo_benchmark import load_benchmark, compare_results
    from scripts.demo_online import online_client, RequestRecorder
    from app.ai.service import AIService
    from app.ai.prompts import build_evaluation_messages, EVALUATION_VERSION
    from app.services.redaction import redact_data
    from app.config import ensure_runtime_dirs
    from app.db import SessionLocal, init_db
    from app.models import Evaluation
    from app.services.imports import store_upload_bytes, create_file_import_task, parse_import_task, supplement_import_task, confirm_import_task
    from app.services.evaluations import evaluate_application_record, matching_input
    from app.services.workflow import get_or_create_job_version

    client = None
    if args.online_config:
        try:
            client = online_client(args.online_config)
        except ValueError as error:
            parser.error(str(error))
    recorder = RequestRecorder(client, output / "requests.json") if client else None
    data = json.loads((SAMPLES / "annotations.json").read_text(encoding="utf-8"))
    baseline = load_benchmark(SAMPLES)
    protected_paths = [ROOT / ".env", ROOT / "README.md", ROOT / "data/runtime.env"]
    for pattern in ("*.db", "*.sqlite", "*.sqlite3"):
        protected_paths.extend((ROOT / "data").glob(pattern))
    if args.online_config:
        protected_paths.append(args.online_config.resolve())

    def protected_digests():
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None for p in protected_paths}
    protected_before = protected_digests()
    for filename, expected in json.loads((SAMPLES / "manifest.json").read_text()).items():
        assert hashlib.sha256((SAMPLES / filename).read_bytes()).hexdigest() == expected, f"样本或标注已变化：{filename}"
    assert data["synthetic"] is True and len(data["samples"]) == 12 and len(data["annotations"]) == 24
    ensure_runtime_dirs()
    init_db()
    results, prepared = [], []
    with SessionLocal() as session:
        for job_data in data["jobs"]:
            job, jd, _ = get_or_create_job_version(session, {"platform": "benchmark", "source_job_id": job_data["id"],
                "title": job_data["title"], "jd_raw": (SAMPLES / f"jd-{job_data['id']}.txt").read_text(encoding="utf-8")})
            session.commit()
            for sample in data["samples"]:
                stored = store_upload_bytes(original_filename=sample["file"], content=(SAMPLES / sample["file"]).read_bytes(), content_type="text/plain")
                task = create_file_import_task(session, uploads=[stored], operator_id=None, job_id=job.id)
                session.commit()
                task = parse_import_task(session, task.id)
                if sample["manual_correction"]:
                    task = supplement_import_task(session, task.id, sample["manual_correction"], version=task.preview_json["revision"])
                app = confirm_import_task(session, task.id, version=task.preview_json["revision"])
                session.commit()
                assert all(getattr(app.candidate, k) == v for k, v in sample["expected_identity"].items())
                input_before = matching_input(app)
                status_before = app.status.value
                candidate = input_before["candidate"]
                assert len(candidate["project_experience"]) == 1
                assert len(candidate["internship_experience"]) == (1 if sample["id"] in {"r01", "r02", "r06", "r12"} else 0)
                assert candidate["work_experience"] == []
                if sample["id"] == "r05": assert "Java" not in candidate["skills"]
                if sample["id"] == "r06": assert "FastAPI" not in candidate["skills"]
                row = {"sample_id": sample["id"], "job_id": job_data["id"], "database_job_id": job.id,
                       "application_id": app.id, "parsed_candidate": candidate, "input_snapshot": input_before,
                       "job_version_id": app.job_version_id, "status_before": status_before}
                if args.prepare_online:
                    prepared.append({**row, "messages": build_evaluation_messages(redact_data(input_before))})
                    continue
                try:
                    if recorder:
                        recorder.pair = f"{sample['id']}-{job_data['id']}"
                        with patch("app.services.evaluations.AIService", return_value=AIService(client)), patch("app.ai.client.httpx.post", recorder.post):
                            result = evaluate_application_record(session, app)
                    else:
                        result = evaluate_application_record(session, app)
                    assert result["provider"] == ("openai_compatible" if client else "mock") and result["fallback"] is False
                    evaluation = session.get(Evaluation, result["evaluation_id"])
                    dimensions = {item["name"]: item for item in evaluation.scores}
                    assert dimensions["domain_match"]["score"] == 0
                    assert dimensions["communication_intent"]["score"] == dimensions["communication_intent"]["weight"] == 0
                    assert dimensions["communication_intent"]["eligibility"]["每周出勤"]["status"] == ("未知" if sample["id"] == "r09" else "自述满足")
                    assert dimensions["domain_match"]["weight"] == dimensions["relevant_experience"]["weight"] == 0
                    assert all(c["score"] in {0, 30, 70, 100} for d in evaluation.scores for c in d["criteria"])
                    if not client:
                        assert "未校准" in result["reason"]
                    row.update(dimensions=evaluation.scores, input_snapshot=evaluation.input_snapshot,
                               model=evaluation.model, prompt_version=evaluation.prompt_version, evaluation_version=evaluation.evaluation_version,
                               missing_information=evaluation.missing_information, **result)
                except (ValueError, AssertionError) as error:
                    if not client:
                        raise
                    row.update(evaluation_error=f"评估失败（{type(error).__name__}）；详见隔离库尝试及requests.json",
                               attempt_id=max((attempt.id for attempt in app.evaluation_attempts), default=None),
                               dimensions=[], total_score=None, model=client.model, provider="openai_compatible", fallback=False)
                row.update(facts_unchanged=matching_input(app) == input_before, status_after=app.status.value,
                           no_hiring_decision=app.status.value == (status_before if row.get("evaluation_error") else "evaluation_review"))
                results.append(row)
                if recorder:
                    (output / "results.partial.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"{len(results)}/24 {recorder.pair}: " + ("failed" if row.get("evaluation_error") else "evaluated"), flush=True)

    if not client and not args.prepare_online:
        by_pair = {(row["sample_id"], row["job_id"]): row for row in results}
        for ranking in data["ranking_draft"]:
            assert by_pair[ranking["higher"], ranking["job_id"]]["total_score"] > by_pair[ranking["lower"], ranking["job_id"]]["total_score"]
        assert by_pair["r03", "backend"]["total_score"] <= by_pair["r01", "backend"]["total_score"]
        assert by_pair["r03", "frontend"]["total_score"] <= by_pair["r02", "frontend"]["total_score"]
    comparison = compare_results(baseline, results) if not args.prepare_online else None
    protected_after = protected_digests()
    load_benchmark(SAMPLES)
    assert protected_before == protected_after
    assert all(row["facts_unchanged"] and row["no_hiring_decision"] for row in results)
    if args.prepare_online:
        (output / "requests.preview.json").write_text(json.dumps(prepared, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {"version": data["version"], "scoring_rules": EVALUATION_VERSION,
              "baseline_version": baseline["version"],
              "run_mode": "prepare_online" if args.prepare_online else ("online" if client else "mock"),
              "engineering_checks": "prepared_only" if args.prepare_online else ("failed" if any(r.get("evaluation_error") for r in results) else "passed"),
              "pair_count": len(prepared) if args.prepare_online else len(results),
              "provider": client.provider if client else ("not_called" if args.prepare_online else "mock"),
              "model": client.model if client else None, "request_count": len(recorder.requests) if recorder else 0,
              "quality_acceptance": comparison["semantic_acceptance"] if comparison else "not_executed",
              "benchmark_check": comparison,
              "isolation": {"before": protected_before, "after": protected_after, "unchanged": protected_before == protected_after},
              "results": results}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output / "report.json")
    if comparison:
        print(json.dumps(comparison["summary"], ensure_ascii=False))
    return 1 if comparison and (args.verify_benchmark or client) and comparison["automated_check"] != "passed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
