"""Demo v1 增量迁移：旧库先备份，只补列／关联，不删除或猜测合并历史。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import uuid
import json
import hashlib
import shutil

from sqlalchemy import Engine, inspect, text


ADDED_COLUMNS = {
    "exam_resources": {"job_id": "INTEGER REFERENCES jobs(id)", "request_key": "VARCHAR(64)"},
    "evaluations": {"input_snapshot": "JSON", "is_stale": "BOOLEAN NOT NULL DEFAULT 0"},
    "offers": {"terms": "JSON"},
    "offer_approvals": {"snapshot": "JSON"},
}


def prepare_upgrade(engine: Engine) -> Path | None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if not tables:
        return None
    pending = [
        (table, column, ddl)
        for table, columns in ADDED_COLUMNS.items() if table in tables
        for column, ddl in columns.items()
        if column not in {item["name"] for item in inspector.get_columns(table)}
    ]
    backup = None
    if (pending or "app_schema_versions" not in tables) and engine.dialect.name == "sqlite" and engine.url.database not in (None, ":memory:"):
        path = Path(engine.url.database).resolve()
        folder = path.parent / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = folder / f"{path.stem}-{stamp}-{uuid.uuid4().hex[:8]}.db"
        with sqlite3.connect(path) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
        assets = backup.with_suffix(".files")
        assets.mkdir()
        for name in ("uploads", "runtime.env"):
            original = path.parent / name
            if original.is_dir():
                shutil.copytree(original, assets / name)
            elif original.is_file():
                shutil.copy2(original, assets / name)
        with sqlite3.connect(backup) as source:
            counts = {name: source.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables if name.replace("_", "").isalnum()}
        manifest = {"database": backup.name, "sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                    "created_at": stamp, "original_database": str(path), "counts": counts,
                    "assets": {str(item.relative_to(assets)): hashlib.sha256(item.read_bytes()).hexdigest() for item in assets.rglob("*") if item.is_file()}}
        backup.with_suffix(".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with engine.begin() as connection:
        for table, column, ddl in pending:
            connection.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}')
    return backup


def finish_upgrade(engine: Engine) -> list[dict]:
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO application_sources (source_id, application_id)
            SELECT a.source_id, a.id FROM applications a
            WHERE a.source_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM application_sources s WHERE s.source_id = a.source_id
            )
        """))
        duplicates = [dict(row) for row in connection.execute(text("""
            SELECT candidate_id, job_id, COUNT(*) AS count FROM applications
            GROUP BY candidate_id, job_id HAVING COUNT(*) > 1
        """)).mappings()]
        if not duplicates:
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_application_candidate_job_idx ON applications(candidate_id, job_id)"))
        connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_exam_resource_request_idx ON exam_resources(request_key)"))
        connection.execute(text("CREATE TABLE IF NOT EXISTS app_schema_versions (version INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO app_schema_versions(version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM app_schema_versions WHERE version=1)"))
        if engine.dialect.name == "sqlite" and connection.exec_driver_sql("PRAGMA foreign_key_check").fetchone():
            raise ValueError("迁移后的外键检查未通过，请保留失败副本并从备份恢复")
    return duplicates
