"""恢复旧附件路径时必须只读取备份副本，不能回到原目录。"""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from app.services.imports import build_rule_parse, extract_text_from_file
from scripts.restore_backup import restore


def backup_with_attachments(tmp_path):
    origin = tmp_path / "old-project"
    uploads = origin / "data" / "uploads"
    uploads.mkdir(parents=True)
    text = "姓名：原始备份人\n手机号：13800139991\n邮箱：backup@example.test\n项目：Python API"
    for name in ("relative.txt", "absolute.txt"):
        (uploads / name).write_text(text, encoding="utf-8")
    database = uploads.parent / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE import_files (id INTEGER PRIMARY KEY, file_path TEXT)")
        connection.execute("CREATE TABLE exam_resources (id INTEGER PRIMARY KEY, file_path TEXT)")
        connection.execute("CREATE TABLE applications (id INTEGER PRIMARY KEY, status TEXT)")
        connection.execute("INSERT INTO import_files VALUES (1, 'data/uploads/relative.txt')")
        connection.execute("INSERT INTO exam_resources VALUES (1, ?)", (str(uploads / "absolute.txt"),))
        connection.execute("INSERT INTO exam_resources VALUES (2, NULL)")
        connection.execute("INSERT INTO applications VALUES (1, 'next_round')")
    backup = tmp_path / "backup.db"
    shutil.copy2(database, backup)
    assets = backup.with_suffix(".files")
    shutil.copytree(uploads, assets / "uploads")
    manifest = {
        "original_database": str(database),
        "sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        "counts": {"import_files": 1, "exam_resources": 2, "applications": 1},
        "assets": {str(path.relative_to(assets)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in assets.rglob("*") if path.is_file()},
    }
    backup.with_suffix(".json").write_text(json.dumps(manifest), encoding="utf-8")
    return backup, origin, manifest


@pytest.mark.parametrize("original_action", ["replace", "remove"])
def test_restore_regressions_read_archived_relative_and_absolute_files(tmp_path, original_action):
    backup, origin, _ = backup_with_attachments(tmp_path)
    original_database = (origin / "data" / "legacy.db").read_bytes()
    for path in (origin / "data" / "uploads").iterdir():
        if original_action == "replace":
            path.write_text("姓名：备份后替换人\n邮箱：changed@example.test", encoding="utf-8")
        else:
            path.unlink()
    restored = restore(backup, tmp_path / "restored")
    with sqlite3.connect(restored) as connection:
        for table in ("import_files", "exam_resources"):
            value = connection.execute(f"SELECT file_path FROM {table} WHERE id=1").fetchone()[0]
            assert Path(value).is_absolute() and Path(value).is_relative_to(restored.parent / "uploads")
            # 与导入解析器相同的路径拼接；不能误读原目录，或依赖其文件仍存在。
            parsed = build_rule_parse(extract_text_from_file(origin / value, "txt"))
            assert parsed["candidate"]["name"] == "原始备份人"
            assert parsed["candidate"]["email"] == "backup@example.test"
        assert connection.execute("SELECT status FROM applications").fetchone()[0] == "next_round"
        assert connection.execute("SELECT file_path FROM exam_resources WHERE id=2").fetchone()[0] is None
    assert (origin / "data" / "legacy.db").read_bytes() == original_database


@pytest.mark.parametrize("invalid", ["unknown_relative", "outside_absolute", "traversal", "missing_manifest", "missing_asset"])
def test_restore_regressions_reject_unmapped_or_unarchived_paths(tmp_path, invalid):
    backup, _, manifest = backup_with_attachments(tmp_path)
    value = {
        "unknown_relative": "uploads/relative.txt",
        "outside_absolute": str(tmp_path / "unrelated" / "relative.txt"),
        "traversal": "data/uploads/../outside.txt",
        "missing_manifest": "data/uploads/relative.txt",
        "missing_asset": "data/uploads/relative.txt",
    }[invalid]
    with sqlite3.connect(backup) as connection:
        connection.execute("UPDATE import_files SET file_path=? WHERE id=1", (value,))
    if invalid == "missing_manifest":
        manifest["assets"].pop(str(Path("uploads") / "relative.txt"))
    if invalid == "missing_asset":
        (backup.with_suffix(".files") / "uploads" / "relative.txt").unlink()
    manifest["sha256"] = hashlib.sha256(backup.read_bytes()).hexdigest()
    backup.with_suffix(".json").write_text(json.dumps(manifest), encoding="utf-8")
    destination = tmp_path / "rejected-restore"
    with pytest.raises(ValueError, match="附件"):
        restore(backup, destination)
    assert not destination.exists()


@pytest.mark.parametrize("in_memory", [False, True], ids=["disk", "memory"])
def test_first_account_reports_backup_status(tmp_path, monkeypatch, in_memory):
    import subprocess
    import sys

    database = tmp_path / "first.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:" if in_memory else f"sqlite:///{database.as_posix()}")
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_RUNTIME_ENV", str(tmp_path / "runtime.env"))
    command = "from app import accounts; accounts._password = lambda: 'synthetic-password'; accounts.main()"
    result = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", command, "create", "first-hr"],
                            capture_output=True, encoding="utf-8", check=True, timeout=45)
    assert ("内存库无需备份" if in_memory else "没有旧 SQLite 数据库文件可备份") in result.stdout
    assert database.exists() == (not in_memory)


@pytest.mark.parametrize("source", ["dotenv", "process"])
def test_maintenance_backup_uses_deployment_paths(tmp_path, monkeypatch, source):
    import subprocess
    import sys
    from sqlalchemy.orm import Session
    from app.db import create_db_engine, init_db
    from app.models import ExamResource, User

    shutil.copytree(Path(__file__).resolve().parents[1] / "app", tmp_path / "app",
                    ignore=shutil.ignore_patterns("__pycache__", "static", "templates"))
    data = tmp_path / "recruitment data"
    database = data / "sqlite" / "recruitment.db"
    runtime = tmp_path / "deployed settings.env"
    files = {"uploads/resume.txt": data / "uploads" / "resume.txt", "runtime.env": runtime,
             "users/1/smtp.runtime.env": data / "users" / "1" / "smtp.runtime.env"}
    for name, path in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("LLM_PROVIDER=mock\n" if path == runtime else name, encoding="utf-8")
    engine = create_db_engine(f"sqlite:///{database.as_posix()}")
    init_db(engine)
    with Session(engine) as session:
        session.add(User(username="existing-hr", normalized_username="existing-hr", password_hash="synthetic"))
        session.add(ExamResource(kind="assessment", title="虚构试卷", url="https://example.test/paper",
                                 file_path=str(files["uploads/resume.txt"])))
        session.commit()
    engine.dispose()
    settings = {"APP_DATA_DIR": data.as_posix(), "APP_RUNTIME_ENV": runtime.as_posix(),
                "DATABASE_URL": f"sqlite:///{database.as_posix()}"}
    (tmp_path / ".env").write_text("".join(f"{k}='{v}'\n" for k, v in settings.items()), encoding="utf-8")
    for name, value in settings.items():
        if source == "process":
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    command = "from app import accounts; accounts._password = lambda: 'synthetic-password'; accounts.main()"
    subprocess.run([sys.executable, "-B", "-c", command, "assign-legacy", "existing-hr"], cwd=tmp_path, check=True, capture_output=True, timeout=45)
    backup, = (database.parent / "backups").glob("*.db")
    restored = restore(backup, tmp_path / "restored")
    for name, original in files.items():
        assert (restored.parent / name).read_bytes() == original.read_bytes()
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT file_path FROM exam_resources").fetchone()[0] == str(restored.parent / "uploads/resume.txt")
