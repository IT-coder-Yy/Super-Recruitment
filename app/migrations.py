"""旧库先备份；补充账号归属并重建唯一约束，保留历史记录与关联。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import uuid
import json
import hashlib
import shutil

from sqlalchemy import Engine, inspect, literal, select, text
from sqlalchemy.schema import CreateTable

from app import config


ADDED_COLUMNS = {
    "users": {"is_active": "BOOLEAN NOT NULL DEFAULT 1", "is_admin": "BOOLEAN NOT NULL DEFAULT 0",
              "can_approve_offers": "BOOLEAN NOT NULL DEFAULT 0"},
    "interviews": {"meeting_id": "VARCHAR(32)", "meeting_request": "JSON"},
    "jobs": {"recruitment_type": "VARCHAR(20) NOT NULL DEFAULT 'autumn'"},
    "exam_resources": {"job_id": "INTEGER REFERENCES jobs(id)", "request_key": "VARCHAR(64)"},
    "evaluations": {"input_snapshot": "JSON", "is_stale": "BOOLEAN NOT NULL DEFAULT 0"},
    "offers": {"terms": "JSON"},
    "offer_approvals": {"snapshot": "JSON"},
}


def backup_database(engine: Engine) -> Path | None:
    """本机维护与结构迁移共用同一份数据库、附件及运行配置备份。"""
    if engine.dialect.name != "sqlite" or engine.url.database in (None, ":memory:"):
        return None
    path = Path(engine.url.database).resolve()
    if not path.is_file():
        return None
    folder = path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = folder / f"{path.stem}-{stamp}-{uuid.uuid4().hex[:8]}.db"
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    assets = backup.with_suffix(".files")
    assets.mkdir()
    for name, original in (("uploads", config.UPLOAD_DIR), ("runtime.env", config.ENV_FILE), ("users", config.DATA_DIR / "users")):
        if original.is_dir():
            shutil.copytree(original, assets / name)
        elif original.is_file():
            shutil.copy2(original, assets / name)
    with sqlite3.connect(backup) as source:
        tables = [row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        counts = {name: source.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables if name.replace("_", "").isalnum()}
    manifest = {"database": backup.name, "sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                "created_at": stamp, "original_database": str(path), "counts": counts,
                "original_uploads": str(config.UPLOAD_DIR.resolve()),
                "assets": {str(item.relative_to(assets)): hashlib.sha256(item.read_bytes()).hexdigest() for item in assets.rglob("*") if item.is_file()}}
    backup.with_suffix(".json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return backup


def prepare_upgrade(engine: Engine) -> Path | None:
    from app.db import Base
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if not tables:
        return None
    added_columns = {name: dict(columns) for name, columns in ADDED_COLUMNS.items()}
    for table in Base.metadata.tables.values():
        if "owner_user_id" in table.c:
            added_columns.setdefault(table.name, {})["owner_user_id"] = "INTEGER REFERENCES users(id)"
    pending = [
        (table, column, ddl)
        for table, columns in added_columns.items() if table in tables
        for column, ddl in columns.items()
        if column not in {item["name"] for item in inspector.get_columns(table)}
    ]
    backup = None
    with engine.connect() as connection:
        version_pending = ("app_schema_versions" not in tables or
                           connection.scalar(text("SELECT COUNT(*) FROM app_schema_versions WHERE version BETWEEN 1 AND 7")) != 7)
    if (pending or version_pending) and engine.dialect.name == "sqlite" and engine.url.database not in (None, ":memory:"):
        backup = backup_database(engine)
    with engine.begin() as connection:
        for table, column, ddl in pending:
            connection.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}')
    return backup


def upgrade_account_constraints(engine: Engine) -> None:
    """SQLite 的表内 UNIQUE 不能直接删除，按官方重建流程保留全部行及索引。"""
    from app.db import Base
    inspector = inspect(engine)
    tables = [name for name in ("candidates", "candidate_sources", "jobs", "exam_resources")
              if any("owner_user_id" not in item["column_names"] for item in inspector.get_unique_constraints(name))]
    if not tables:
        return
    if engine.dialect.name != "sqlite":
        raise ValueError("旧库账号隔离迁移目前支持 SQLite")
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            for name in tables:
                table = Base.metadata.tables[name]
                old_columns = {item["name"] for item in inspect(connection).get_columns(name)}
                if old_columns - set(table.c.keys()):
                    raise ValueError(f"{name} 含未知字段，停止迁移以保留数据")
                schema_objects = connection.exec_driver_sql(
                    "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (name,),
                ).scalars().all()
                temporary = name + "__account_upgrade"
                ddl = str(CreateTable(table).compile(engine))
                ddl = ddl.replace("CREATE TABLE " + engine.dialect.identifier_preparer.format_table(table), f'CREATE TABLE "{temporary}"', 1)
                connection.exec_driver_sql(ddl)
                columns = ", ".join(f'"{column}"' for column in table.c.keys() if column in old_columns)
                connection.exec_driver_sql(f'INSERT INTO "{temporary}" ({columns}) SELECT {columns} FROM "{name}"')
                connection.exec_driver_sql(f'DROP TABLE "{name}"')
                connection.exec_driver_sql(f'ALTER TABLE "{temporary}" RENAME TO "{name}"')
                for sql in schema_objects:
                    connection.exec_driver_sql(sql)
            if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchone():
                raise ValueError("账号隔离迁移外键检查失败，已回滚")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()


def _repair_legacy_mail_history(connection) -> None:
    from app.models import AuditLog, Notification

    mail = Notification.__table__
    auth = AuditLog.__table__
    transfer = auth.alias("transfer")
    meta = mail.c.provider_response
    recorded = meta["handover_audit_id"].as_integer()
    current = meta["current_handover_audit_id"].as_integer()
    authorized = auth.c.details["handover_audit_id"].as_integer()
    attempts = (select(mail.c.id, meta, authorized)
        .join(transfer, transfer.c.id == recorded)
        .outerjoin(auth, (auth.c.id == meta["resend_authorization_id"].as_integer())
                   & (auth.c.action == "notification.resend_authorized") & (auth.c.entity_type == "notification")
                   & (auth.c.entity_id == meta["retry_of"].as_integer()) & (auth.c.created_at <= mail.c.created_at))
        .where(mail.c.channel == "email",
               transfer.c.action.in_(["account.data_transferred", "account.legacy_assigned"]),
               transfer.c.entity_type == "user", current.is_not(None) | (transfer.c.entity_id == mail.c.owner_user_id)))
    corrections = attempts.where((authorized.is_not(None) & ((recorded != authorized) | current.is_(None))) |
                                 (authorized.is_(None) & (transfer.c.created_at > mail.c.created_at)))
    for mail_id, values, sent_handover in connection.execute(corrections).all():
        # 后来的交接只能证明当前归属；只有本次发送引用的授权能证明当时的编号。
        old_id = values.pop("handover_audit_id")
        values["current_handover_audit_id"] = values.get("current_handover_audit_id") or old_id
        if sent_handover is not None:
            values["handover_audit_id"] = sent_handover
        connection.execute(mail.update().where(mail.c.id == mail_id).values(
            provider_response=values, updated_at=mail.c.updated_at))

    # 旧版只在首次确认后重发的那一条记录上保存授权 ID，后续失败尝试通过双向重试关联核对。
    awaiting_verification = mail.c.status.in_(["unknown", "sending"]) & meta["retry_id"].as_integer().is_(None)
    retries = (attempts.add_columns(auth.c.after_data, mail.c.owner_user_id, mail.c.target_type,
                                    mail.c.target_id, mail.c.template, mail.c.recipient)
        .where((mail.c.status == "failed") | awaiting_verification, meta["retry_of"].as_integer().is_not(None),
               transfer.c.entity_id == mail.c.owner_user_id, transfer.c.created_at <= mail.c.created_at,
               current.is_(None) | (current == recorded)).order_by(mail.c.id))
    confirmed_links = {}
    for mail_id, values, authorized_id, authorized_sender, *business_record in connection.execute(retries):
        handover_id = values["handover_audit_id"]
        sender = {key: values[key] for key in ("smtp_source", "smtp_owner_id", "smtp_from", "smtp_username") if key in values}
        context = (handover_id, business_record, sender)
        previous = confirmed_links.pop((values["retry_of"], mail_id), None)
        if values.get("resend_authorization_id"):
            if authorized_id != handover_id or authorized_sender != sender:
                continue
        elif previous != context:
            continue
        if values.get("current_handover_audit_id") is None:
            connection.execute(mail.update().where(mail.c.id == mail_id).values(
                provider_response={**values, "current_handover_audit_id": handover_id}, updated_at=mail.c.updated_at))
        if next_id := values.get("retry_id"):
            confirmed_links[mail_id, next_id] = context


def finish_upgrade(engine: Engine) -> list[dict]:
    from app.db import Base
    upgrade_account_constraints(engine)
    with engine.begin() as connection:
        for table in Base.metadata.tables.values():
            for index in table.indexes:
                if "owner_user_id" in index.columns:
                    index.create(connection, checkfirst=True)
        connection.execute(text("""
            INSERT INTO application_sources (source_id, application_id, owner_user_id)
            SELECT a.source_id, a.id, a.owner_user_id FROM applications a
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
        existing = inspect(connection).get_indexes("exam_resources")
        if any(item["name"] == "uq_exam_resource_request_idx" and item["column_names"] == ["request_key"] for item in existing):
            connection.execute(text("DROP INDEX uq_exam_resource_request_idx"))
        connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_exam_resource_request_idx ON exam_resources(owner_user_id, request_key)"))
        connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_interview_meeting_id_idx ON interviews(meeting_id)"))
        connection.execute(text("CREATE TABLE IF NOT EXISTS app_schema_versions (version INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO app_schema_versions(version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM app_schema_versions WHERE version=1)"))
        connection.execute(text("INSERT INTO app_schema_versions(version) SELECT 2 WHERE NOT EXISTS (SELECT 1 FROM app_schema_versions WHERE version=2)"))
        missing_mail_versions = {3, 5, 6, 7} - set(connection.scalars(text("SELECT version FROM app_schema_versions")))
        if missing_mail_versions:
            _repair_legacy_mail_history(connection)
            for version in sorted(missing_mail_versions):
                connection.execute(text("INSERT INTO app_schema_versions(version) VALUES (:version)"), {"version": version})
        if not connection.scalar(text("SELECT 1 FROM app_schema_versions WHERE version=4")):
            users, audit = Base.metadata.tables["users"], Base.metadata.tables["audit_logs"]
            granted = users.c.can_approve_offers.is_(True)
            changes = select(
                users.c.id, literal("旧账号权限迁移"), literal("account.permissions_updated"), literal("user"), users.c.id,
                literal({"can_approve_offers": True}, audit.c.before_data.type),
                literal({"can_approve_offers": False}, audit.c.after_data.type),
            ).where(granted)
            connection.execute(audit.insert().from_select(
                ["owner_user_id", "actor", "action", "entity_type", "entity_id", "before_data", "after_data"], changes))
            connection.execute(users.update().where(granted).values(can_approve_offers=False))
            connection.execute(text("INSERT INTO app_schema_versions(version) VALUES (4)"))
        if engine.dialect.name == "sqlite" and connection.exec_driver_sql("PRAGMA foreign_key_check").fetchone():
            raise ValueError("迁移后的外键检查未通过，请保留失败副本并从备份恢复")
    return duplicates
