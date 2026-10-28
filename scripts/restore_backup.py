"""将迁移备份恢复到全新目录，永不覆盖运行中的数据库。"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3


def restore(backup: Path, destination: Path) -> Path:
    backup, destination = backup.resolve(), destination.resolve()
    manifest = json.loads(backup.with_suffix(".json").read_text(encoding="utf-8"))
    if hashlib.sha256(backup.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("数据库备份哈希不匹配")
    assets = backup.with_suffix(".files")
    for relative, expected in manifest["assets"].items():
        path = (assets / relative).resolve()
        if not path.is_relative_to(assets.resolve()) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("附件备份哈希不匹配或路径越界")
    # 先核对所有附件引用，避免失败时留下仍指向原目录的“恢复库”。
    original_uploads = Path(manifest["original_database"]).parent / "uploads"
    archived_paths = {Path(value).as_posix() for value in manifest["assets"]}
    relocations = []
    with sqlite3.connect(backup) as source:
        tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("import_files", "exam_resources"):
            if table not in tables:
                continue
            for record_id, value in source.execute(f'SELECT id, file_path FROM "{table}"').fetchall():
                if not value:
                    continue
                path = Path(value)
                prefix = original_uploads if path.is_absolute() else Path("data") / "uploads"
                if not path.is_relative_to(prefix):
                    raise ValueError(f"附件路径无法映射到备份：{table}#{record_id}")
                relative = path.relative_to(prefix)
                replacement = (destination / "uploads" / relative).resolve()
                if (".." in relative.parts or not replacement.is_relative_to(destination / "uploads")
                        or (Path("uploads") / relative).as_posix() not in archived_paths):
                    raise ValueError(f"附件未列入备份清单或路径越界：{table}#{record_id}")
                relocations.append((table, record_id, str(replacement)))
    destination.mkdir(parents=True, exist_ok=False)
    restored = destination / Path(manifest["original_database"]).name
    with sqlite3.connect(backup) as source, sqlite3.connect(restored) as target:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("恢复后完整性检查失败")
        for name, count in manifest["counts"].items():
            if not name.replace("_", "").isalnum() or target.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] != count:
                raise ValueError("恢复后行数不一致")
    for item in assets.iterdir():
        if item.is_dir():
            shutil.copytree(item, destination / item.name)
        else:
            shutil.copy2(item, destination / item.name)
    # 只重定位备份所含上传文件；不修改业务状态、条款和审计证据。
    with sqlite3.connect(restored) as target:
        for table, record_id, replacement in relocations:
            target.execute(f'UPDATE "{table}" SET file_path=? WHERE id=?', (replacement, record_id))
    return restored


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("destination", type=Path, help="不存在的新目录")
    args = parser.parse_args()
    print(restore(args.backup.resolve(), args.destination.resolve()))
