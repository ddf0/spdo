"""Модель уведомления внутри приложения."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from spdo.db.base import Base, CreatedAtMixin


class Notification(CreatedAtMixin, Base):
    """Уведомление пользователя (Ф18).

    Внешние ключи заданы строками: модуль не импортирует модели других
    модулей.

    Attributes:
        id: Идентификатор.
        user_id: Получатель.
        ticket_id: Обращение получателя, к которому относится уведомление.
        text: Текст уведомления.
        read_at: Дата прочтения или ``None``.
    """

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user_id_read_at", "user_id", "read_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
