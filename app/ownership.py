"""每次请求和后台任务绑定一个账号；None 仅供本机维护命令使用。"""
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import ForeignKey, event, inspect, select
from sqlalchemy.orm import Mapped, Session, mapped_column, with_loader_criteria


current_account_id: ContextVar[int | None] = ContextVar("current_account_id", default=None)


class AccountOwned:
    # 旧数据暂存 NULL，只有明确指定归属后才对账号开放。
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True,
    )


class AccountAccessDenied(PermissionError):
    pass


def session_account(session) -> int | None:
    account_id = current_account_id.get()
    return account_id if account_id is not None else session.info.get("account_id")


@contextmanager
def account_scope(user_id: int | None):
    token = current_account_id.set(user_id)
    try:
        yield
    finally:
        current_account_id.reset(token)


@event.listens_for(Session, "do_orm_execute")
def restrict_account_queries(state):
    account_id = session_account(state.session)
    if account_id is not None and (state.is_select or state.is_update or state.is_delete):
        state.statement = state.statement.options(with_loader_criteria(
            AccountOwned, lambda row: row.owner_user_id == account_id, include_aliases=True,
        ))


@event.listens_for(Session, "before_flush")
def bind_and_check_ownership(session, flush_context, instances):
    account_id = session_account(session)
    if account_id is None:
        return
    records = [row for row in session.new | session.dirty | session.deleted if isinstance(row, AccountOwned)]
    for row in records:
        if row in session.new and row.owner_user_id is None:
            row.owner_user_id = account_id
        if account_id <= 0 or row.owner_user_id != account_id:
            raise AccountAccessDenied("无权访问其他账号的数据")
        history = inspect(row).attrs.owner_user_id.history
        if history.deleted and history.deleted[0] != account_id:
            raise AccountAccessDenied("不能通过业务请求转移数据归属")
    # 防止通过表单中的关联 ID，把本账号的新记录连到其他账号的记录上。
    for row in records:
        if row in session.deleted:
            continue
        for column in inspect(row).mapper.columns:
            value = getattr(row, column.key)
            if value is None:
                continue
            for fk in column.foreign_keys:
                target = fk.column.table
                if "owner_user_id" in target.c:
                    owner = session.connection().scalar(select(target.c.owner_user_id).where(fk.column == value))
                    if owner != account_id:
                        raise AccountAccessDenied("关联记录不属于当前账号")
