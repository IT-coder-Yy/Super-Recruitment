"""旧库升级必须保留全部记录、附件和原唯一性语义，并明确分配账号。"""
import json
import sqlite3
import pytest

from sqlalchemy import MetaData, UniqueConstraint, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import IntegrityError

from app.accounts import assign_data, create_account
from app.db import Base, create_db_engine, init_db
from app.models import Candidate, User
from app.ownership import account_scope
from app.services.candidates import get_or_create_candidate
from app.services.auth import hash_password


def legacy_schema(engine):
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(metadata)
    for table in metadata.tables.values():
        if "owner_user_id" in table.c:
            owner = table.c.owner_user_id
            for constraint in list(table.constraints):
                if "owner_user_id" in constraint.columns:
                    columns = [column.name for column in constraint.columns if column is not owner]
                    table.constraints.remove(constraint)
                    if isinstance(constraint, UniqueConstraint):
                        table.append_constraint(UniqueConstraint(*columns, name=constraint.name))
            table.indexes = {index for index in table.indexes if "owner_user_id" not in index.columns}
            table._columns.remove(owner)
    for name in ("is_active", "is_admin", "can_approve_offers"):
        metadata.tables["users"]._columns.remove(metadata.tables["users"].c[name])
    metadata.create_all(engine)
    return metadata


def test_upgrade_preserves_legacy_data_and_account_dedup(tmp_path):
    path = tmp_path / "legacy.db"
    engine = create_db_engine(f"sqlite:///{path.as_posix()}")
    old = legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(old.tables["users"].insert(), {"id": 1, "username": "legacy-owner", "normalized_username": "legacy-owner", "password_hash": hash_password("synthetic-password-123")})
        connection.execute(old.tables["candidates"].insert(), {"id": 1, "name": "历史虚构人", "normalized_name": "历史虚构人", "phone": "13900007771", "normalized_phone": "13900007771", "email": "legacy@example.test"})
        connection.execute(old.tables["jobs"].insert(), {"id": 1, "platform": "file", "source_job_id": "legacy-job", "title": "历史岗位"})
        connection.execute(old.tables["job_versions"].insert(), {"id": 1, "job_id": 1, "version_number": 1, "version_hash": "synthetic-hash", "jd_raw": "Python"})
        connection.execute(old.tables["candidate_sources"].insert(), {"id": 1, "candidate_id": 1, "platform": "file", "source_application_id": "legacy-source", "raw_resume": "历史原始简历"})
        connection.execute(old.tables["applications"].insert(), {"id": 1, "candidate_id": 1, "job_id": 1, "job_version_id": 1, "source_id": 1})
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "resume.txt").write_text("历史原始简历", encoding="utf-8")
    (tmp_path / "runtime.env").write_text("LLM_PROVIDER=mock\n", encoding="utf-8")
    with sqlite3.connect(path) as connection:
        original = connection.execute("SELECT * FROM candidates WHERE id=1").fetchone()
    init_db(engine)
    backups = list((tmp_path / "backups").glob("*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as old_db:
        assert "owner_user_id" not in {row[1] for row in old_db.execute("PRAGMA table_info(candidates)")}
        assert old_db.execute("SELECT raw_resume FROM candidate_sources").fetchone()[0] == "历史原始简历"
    assert (backups[0].with_suffix(".files") / "uploads" / "resume.txt").read_text(encoding="utf-8") == "历史原始简历"
    manifest = json.loads(backups[0].with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["counts"]["applications"] == 1
    factory = sessionmaker(engine, expire_on_commit=False)
    with account_scope(1), factory() as session:
        assert session.scalar(select(Candidate)) is None
    with factory.begin() as session:
        target = session.get(User, 1)
        assert not target.is_admin and not target.can_approve_offers
        counts = assign_data(session, target)
        other = create_account(session, "another-owner", "synthetic-password-123")
        other_id = other.id
    assert counts["candidates"] == counts["applications"] == 1
    with sqlite3.connect(path) as connection:
        columns = ", ".join('"' + column.name + '"' for column in old.tables["candidates"].c)
        assert connection.execute(f"SELECT {columns} FROM candidates WHERE id=1").fetchone() == original
    with account_scope(1), factory() as session:
        original = session.scalar(select(Candidate))
        assert original.sources[0].raw_resume == "历史原始简历"
        assert original.applications[0].job.title == "历史岗位"
        assert original.applications[0].source_links[0].owner_user_id == 1
    with account_scope(other_id), factory.begin() as session:
        own, created = get_or_create_candidate(session, {"name": "历史虚构人", "phone": "13900007771"})
        assert created and own.id != 1
    init_db(engine)
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1
    with engine.connect() as connection:
        assert not connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    for name in ("candidates", "candidate_sources", "jobs", "exam_resources"):
        assert all("owner_user_id" in item["column_names"] for item in inspect(engine).get_unique_constraints(name))
    engine.dispose()


def test_account_transfer_rolls_back_instead_of_merging_same_person(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'transfer.db').as_posix()}")
    init_db(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        first = create_account(session, "account-one", "synthetic-password-123")
        second = create_account(session, "account-two", "synthetic-password-123")
    for owner in (first.id, second.id):
        with account_scope(owner), factory.begin() as session:
            get_or_create_candidate(session, {"name": "相同虚构人", "phone": "13900006661"})
    with pytest.raises(IntegrityError), factory.begin() as session:
        assign_data(session, session.get(User, first.id), source=session.get(User, second.id))
    with factory() as session:
        assert set(session.scalars(select(Candidate.owner_user_id))) == {first.id, second.id}
    engine.dispose()
