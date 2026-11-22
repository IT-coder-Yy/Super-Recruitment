"""仅在服务器本机运行的账号管理工具。"""
from __future__ import annotations
import argparse
from getpass import getpass
from app.config import ensure_runtime_dirs, reload_env
ensure_runtime_dirs()
reload_env()
from sqlalchemy import select
from app.db import SessionLocal, engine, init_db
from app.migrations import backup_database
from app.models import AuditLog, User
from app.services.auth import hash_password, normalize_username


def create_account(session, username: str, password: str) -> User:
    username = username.strip()
    if not 3 <= len(username) <= 100:
        raise ValueError("用户名必须为 3–100 个字符")
    if len(password) < 12:
        raise ValueError("正式账号密码至少需要 12 个字符")
    if session.scalar(select(User.id).where(User.normalized_username == normalize_username(username))):
        raise ValueError("账号已存在")
    user = User(username=username, normalized_username=normalize_username(username), password_hash=hash_password(password))
    session.add(user)
    session.flush()
    session.add(AuditLog(actor="本机账号管理", action="account.created", entity_type="user", entity_id=user.id))
    return user


def _password() -> str:
    first = getpass("新密码（至少 12 个字符，不回显）：")
    if len(first) < 12 or first != getpass("再次输入密码："):
        raise ValueError("密码长度不足或两次输入不一致")
    return first


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="创建受控账号")
    create.add_argument("username")
    args = parser.parse_args()
    password = _password()
    backup = backup_database(engine)
    init_db()
    with SessionLocal.begin() as session:
        create_account(session, args.username, password)
    in_memory = engine.dialect.name == "sqlite" and engine.url.database in (None, ":memory:")
    print("已完成。备份：", backup or ("内存库无需备份" if in_memory else "没有旧 SQLite 数据库文件可备份"))


if __name__ == "__main__":
    main()
