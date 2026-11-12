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


def reset_password(session, username: str, password: str) -> None:
    if len(password) < 12:
        raise ValueError("正式账号密码至少需要 12 个字符")
    user = session.scalar(select(User).where(User.normalized_username == normalize_username(username)))
    if user is None:
        raise ValueError("指定账号不存在")
    user.password_hash, user.is_active = hash_password(password), True
    session.add(AuditLog(actor="本机账号管理", action="account.password_reset", entity_type="user", entity_id=user.id))


def _password() -> str:
    first = getpass("新密码（至少 12 个字符，不回显）：")
    if len(first) < 12 or first != getpass("再次输入密码："):
        raise ValueError("密码长度不足或两次输入不一致")
    return first


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    reset = commands.add_parser("reset-password", help="重置密码并启用账号")
    reset.add_argument("username")
    args = parser.parse_args()
    password = _password()
    backup = backup_database(engine)
    init_db()
    with SessionLocal.begin() as session:
        reset_password(session, args.username, password)
    print("已完成。备份：", backup or "内存库无需备份")


if __name__ == "__main__":
    main()
