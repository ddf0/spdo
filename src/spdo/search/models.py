"""Модель поискового индекса.

Индекс принадлежит модулю ``search`` и дублирует нужные для подбора поля
обращения, чтобы поиск не зависел от бизнес-модулей. Внешние ключи
заданы строками, модели других модулей не импортируются.
"""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, String, Text, false, func
from sqlalchemy.orm import Mapped, mapped_column

from spdo.config import settings
from spdo.db.base import Base


class SearchIndexEntry(Base):
    """Индексированное обращение, среди которых ищутся похожие.

    Attributes:
        ticket_id: Обращение; первичный ключ записи индекса.
        text: Текст «заголовок + описание».
        category_id: Категория.
        component_id: Компонент.
        author_id: Автор; свои обращения видны автору всегда.
        confidential: Не показывать посторонним пользователям.
        embedding: Векторное представление текста или ``None``, если
            ещё не вычислено.
        model_version: Модель, которой вычислен вектор, или ``None``.
        updated_at: Дата последнего обновления записи.
    """

    __tablename__ = "search_index"

    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True
    )
    text: Mapped[str] = mapped_column(Text)
    category_id: Mapped[int | None]
    component_id: Mapped[int | None]
    author_id: Mapped[int]
    confidential: Mapped[bool] = mapped_column(default=False, server_default=false())
    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.embedding_dim))
    model_version: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
