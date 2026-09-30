"""Модель журнала действий пользователей."""

from enum import StrEnum

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from spdo.db.base import Base, CreatedAtMixin, pg_enum


class ActionKind(StrEnum):
    """Вид действия, фиксируемого в журнале."""

    LINK_CREATED = "link_created"
    RECOMMENDATION_REJECTED = "recommendation_rejected"
    STATUS_CHANGED = "status_changed"


class ActionLogEntry(CreatedAtMixin, Base):
    """Запись журнала действий по связям, рекомендациям и статусам.

    ТЗ требует журнал действий пользователей по установлению и отклонению
    связей и изменению статусов. В отличие от истории изменений одного
    обращения, журнал сквозной и служит для отчётов и разбора инцидентов.
    Удаление обращения, упомянутого в журнале, запрещено схемой.

    Attributes:
        id: Идентификатор.
        user_id: Кто выполнил действие.
        action: Вид действия.
        ticket_id: Обращение, над которым выполнено действие.
        other_ticket_id: Второе обращение (цель связи, кандидат) или ``None``.
        details: Дополнительные сведения в свободной форме.
    """

    __tablename__ = "action_log"
    __table_args__ = (
        Index("ix_action_log_created_at", "created_at"),
        Index("ix_action_log_ticket_id", "ticket_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    action: Mapped[ActionKind] = mapped_column(pg_enum(ActionKind, "action_kind"))
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    other_ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"))
    details: Mapped[str] = mapped_column(Text, default="")
