"""仅在服务器本机运行的账号管理及历史数据归属工具。"""
from __future__ import annotations

import argparse
from getpass import getpass

from app.config import ensure_runtime_dirs, reload_env

ensure_runtime_dirs()
reload_env()

from sqlalchemy import select, update
from app.db import Base, SessionLocal, engine, init_db
from app.migrations import backup_database
from app.models import AuditLog, Notification, NotificationStatus, User
from app.ownership import account_scope
from app.services.auth import hash_password, normalize_username


def create_account(session, username: str, password: str, *, admin=False, approve_offers=False) -> User:
    username = username.strip()
    if not 3 <= len(username) <= 100:
        raise ValueError("用户名必须为 3–100 个字符")
    if len(password) < 12:
        raise ValueError("正式账号密码至少需要 12 个字符")
    if session.scalar(select(User.id).where(User.normalized_username == normalize_username(username))):
        raise ValueError("账号已存在")
    user = User(username=username, normalized_username=normalize_username(username), password_hash=hash_password(password),
                is_active=True, is_admin=admin, can_approve_offers=approve_offers)
    session.add(user)
    session.flush()
    session.add(AuditLog(owner_user_id=user.id, actor="本机账号管理", action="account.created",
                         entity_type="user", entity_id=user.id, after_data={"is_admin": admin, "can_approve_offers": user.can_approve_offers}))
    return user


def assign_data(session, target: User, *, source: User | None = None,
                confirmed_mail_ids: list[int] | None = None) -> dict[str, int]:
    """不合并跨账号重名记录；唯一约束冲突时整个交接事务回滚。"""
    if source and source.id == target.id:
        raise ValueError("来源与接收账号不能相同")
    confirmed_mail_ids = sorted(set(confirmed_mail_ids or []))
    mails = [] if confirmed_mail_ids else session.execute(select(Notification.id, Notification.provider_response).where(
        Notification.owner_user_id == (source.id if source else None), Notification.channel == "email",
    )).all()
    for notification_id in confirmed_mail_ids:
        mail = session.get(Notification, notification_id)
        if (source or not mail or mail.owner_user_id != target.id or mail.channel != "email"
                or mail.status != NotificationStatus.FAILED or mail.current_handover_audit_id):
            raise ValueError("仅可补录已分配给目标账号、尚无交接标记的失败邮件")
        mails.append((mail.id, mail.provider_response))
    counts = {}
    for table in Base.metadata.sorted_tables:
        if confirmed_mail_ids or "owner_user_id" not in table.c:
            continue
        condition = table.c.owner_user_id == source.id if source else table.c.owner_user_id.is_(None)
        values = {"owner_user_id": target.id}
        if "updated_at" in table.c:
            values["updated_at"] = table.c.updated_at
        result = session.execute(update(table).where(condition).values(**values))
        counts[table.name] = result.rowcount
    audit = AuditLog(owner_user_id=target.id, actor="本机账号管理", action="account.data_transferred" if source else "account.legacy_assigned",
                         entity_type="user", entity_id=target.id,
                         after_data={"source_user_id": source.id if source else None, "counts": counts,
                                     "confirmed_notification_ids": confirmed_mail_ids})
    session.add(audit)
    session.flush()
    for notification_id, metadata in mails:
        session.execute(update(Notification).where(Notification.id == notification_id).values(
            provider_response={**(metadata or {}), "current_handover_audit_id": audit.id}, updated_at=Notification.updated_at,
        ))
    return counts


def _password() -> str:
    first = getpass("新密码（至少 12 个字符，不回显）：")
    if len(first) < 12 or first != getpass("再次输入密码："):
        raise ValueError("密码长度不足或两次输入不一致")
    return first


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="创建受控账号；首个管理员使用 --admin")
    create.add_argument("username")
    create.add_argument("--admin", action="store_true")
    create.add_argument("--approve-offers", action="store_true")
    change = commands.add_parser("update", help="修改权限或停用账号，立即作用于已有会话")
    change.add_argument("username")
    for flag in ("active", "admin", "approve-offers"):
        change.add_argument("--" + flag, choices=["yes", "no"])
    reset = commands.add_parser("reset-password", help="重置密码并启用账号")
    reset.add_argument("username")
    legacy = commands.add_parser("assign-legacy", help="将尚无归属的历史数据交给指定账号")
    legacy.add_argument("username")
    legacy.add_argument("--confirm-assigned-mail", type=int, action="append", default=[],
                        help="仅补录指定已分配失败邮件的分配审计，可重复指定；不分配其他资料、不发信")
    transfer = commands.add_parser("transfer", help="将一个账号的全部招聘数据交接给另一账号")
    transfer.add_argument("source_username")
    transfer.add_argument("username")
    args = parser.parse_args()
    password = _password() if args.command in {"create", "reset-password"} else None
    backup = backup_database(engine)
    init_db()
    counts = None
    with account_scope(None), SessionLocal.begin() as session:
        if args.command == "create":
            create_account(session, args.username, password, admin=args.admin, approve_offers=args.approve_offers)
        else:
            user = session.scalar(select(User).where(User.normalized_username == normalize_username(args.username)))
            if user is None:
                raise ValueError("指定账号不存在")
            if args.command in {"assign-legacy", "transfer"}:
                source = None
                if args.command == "transfer":
                    source = session.scalar(select(User).where(User.normalized_username == normalize_username(args.source_username)))
                    if source is None:
                        raise ValueError("来源账号不存在")
                counts = assign_data(session, user, source=source,
                                     confirmed_mail_ids=args.confirm_assigned_mail if args.command == "assign-legacy" else None)
            elif args.command == "reset-password":
                user.password_hash, user.is_active = hash_password(password), True
                session.add(AuditLog(owner_user_id=user.id, actor="本机账号管理", action="account.password_reset", entity_type="user", entity_id=user.id))
            else:
                changes = {key: value == "yes" for key, value in {"is_active": args.active, "is_admin": args.admin,
                           "can_approve_offers": args.approve_offers}.items() if value is not None}
                before = {key: getattr(user, key) for key in changes}
                for key, value in changes.items():
                    setattr(user, key, value)
                session.add(AuditLog(owner_user_id=user.id, actor="本机账号管理", action="account.permissions_updated",
                                     entity_type="user", entity_id=user.id, before_data=before, after_data=changes))
    if counts is not None:
        print("迁移行数：", counts)
    if args.command == "assign-legacy" and args.confirm_assigned_mail:
        print("已补录邮件：", args.confirm_assigned_mail)
    print("已完成。备份：", backup or "内存库无需备份")


if __name__ == "__main__":
    main()
