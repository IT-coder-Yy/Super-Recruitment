"""在新隔离目录回放 12×2 程序解析与显式 Mock 匹配；不自批人工基准。"""
from pathlib import Path
import hashlib
import json
import os
import socket
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
SAMPLES=ROOT/"tests/fixtures/demo"
OUTPUT=ROOT/".test-runs"/f"samples-{uuid.uuid4().hex}"
OUTPUT.mkdir(parents=True)
os.environ.update(APP_DATA_DIR=str(OUTPUT), APP_RUNTIME_ENV=str(OUTPUT/"runtime.env"),
    DATABASE_URL=f"sqlite:///{(OUTPUT/'demo.db').as_posix()}", APP_SECRET_KEY="synthetic-benchmark-secret",
    LLM_PROVIDER="mock", LLM_BASE_URL="", LLM_API_KEY="", LLM_MODEL="", SMTP_HOST="", SMTP_PASSWORD="", WECOM_WEBHOOK_URL="")
def deny_network(*args, **kwargs):
    raise AssertionError("此样本回放禁止外呼")
socket.create_connection=deny_network
socket.socket.connect=deny_network
sys.path.insert(0,str(ROOT))
from app.config import ensure_runtime_dirs
from app.db import SessionLocal, init_db
from app.models import Evaluation
from app.services.imports import store_upload_bytes, create_file_import_task, parse_import_task, supplement_import_task, confirm_import_task
from app.services.evaluations import evaluate_application_record
from app.services.workflow import get_or_create_job_version

data=json.loads((SAMPLES/"annotations.json").read_text(encoding="utf-8"))
for filename, expected in json.loads((SAMPLES/"manifest.json").read_text()).items():
    assert hashlib.sha256((SAMPLES/filename).read_bytes()).hexdigest()==expected, f"样本或标注已变化，请审核版本并更新 manifest：{filename}"
assert len(data["samples"])==12 and len(data["annotations"])==24
ensure_runtime_dirs(); init_db()
results=[]
with SessionLocal() as session:
    for job_data in data["jobs"]:
        job, jd, _=get_or_create_job_version(session,{"platform":"benchmark", "source_job_id":job_data["id"],
            "title":job_data["title"],"jd_raw":(SAMPLES/f"jd-{job_data['id']}.txt").read_text(encoding="utf-8")})
        session.commit()
        for sample in data["samples"]:
            stored=store_upload_bytes(original_filename=sample["file"],content=(SAMPLES/sample["file"]).read_bytes(),content_type="text/plain")
            task=create_file_import_task(session,uploads=[stored],operator_id=None,job_id=job.id)
            session.commit(); task=parse_import_task(session,task.id)
            if sample["manual_correction"]:
                task=supplement_import_task(session,task.id,sample["manual_correction"],version=task.preview_json["revision"])
            app=confirm_import_task(session,task.id,version=task.preview_json["revision"])
            session.commit()
            assert all(getattr(app.candidate,k)==v for k,v in sample["expected_identity"].items())
            result=evaluate_application_record(session,app)
            assert result["provider"]=="mock" and result["fallback"] is False
            evaluation=session.get(Evaluation,result["evaluation_id"])
            candidate=evaluation.input_snapshot["candidate"]
            dimensions={item["name"]:item for item in evaluation.scores}
            assert len(candidate["project_experience"])==1
            assert len(candidate["internship_experience"])==(1 if sample["id"] in {"r01","r02","r06","r12"} else 0)
            assert candidate["work_experience"]==[]
            if sample["id"]=="r05": assert "Java" not in candidate["skills"]
            if sample["id"]=="r06": assert "FastAPI" not in candidate["skills"]
            assert dimensions["domain_match"]["score"]==0
            assert dimensions["communication_intent"]["score"]==(0 if sample["id"]=="r09" else 100)
            assert "未校准" in result["reason"]
            results.append({"sample_id":sample["id"],"job_id":job_data["id"],"database_job_id":job.id,"application_id":app.id,
                            "parsed_candidate":candidate,"dimensions":evaluation.scores,
                            "missing_information":evaluation.missing_information,**result})
by_pair={(row["sample_id"],row["job_id"]):row for row in results}
for ranking in data["ranking_draft"]:
    assert by_pair[(ranking["higher"],ranking["job_id"])]["total_score"] > by_pair[(ranking["lower"],ranking["job_id"])]["total_score"]
assert by_pair[("r03","backend")]["total_score"] <= by_pair[("r01","backend")]["total_score"]
assert by_pair[("r03","frontend")]["total_score"] <= by_pair[("r02","frontend")]["total_score"]
report={"version":data["version"],"engineering_checks":"passed","pair_count":len(results),"provider":"mock",
        "regression_checks":"passed: 技能否定/词边界、经历分类、缺证零分、已知/未知出勤及重复计数倒挂反例",
        "quality_acceptance":"NOT_EXECUTED: 人工标注未确认，真实模型未调用；Mock 分数不用于质量验收", "results":results}
(OUTPUT/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print(OUTPUT/"report.json")
