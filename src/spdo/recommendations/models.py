"""Модели сеанса поиска и рекомендации."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from spdo.db.base import Base, CreatedAtMixin, pg_enum
from spdo.search.base import LinkKind


class Verdict(StrEnum):
    """Решение человека по рекомендации."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class SearchSession(CreatedAtMixin, Base):
    """Однократный запуск подбора похожих обращений (Ф3).

    Хранит всё, что нужно для воспроизведения выдачи: режим, версию
    модели и снимок параметров поиска на момент запуска.

    Attributes:
        id: Идентификатор.
        user_id: Кто запустил подбор.
        ticket_id: Обращение, зарегистрированное по итогам, или ``None``.
        query_text: Текст запроса «заголовок + описание».
        category_id: Категория запроса или ``None``.
        component_id: Компонент запроса или ``None``.
        mode: Режим поиска (``lexical`` или ``hybrid``).
        model_version: Версия модели или алгоритма.
        params: Пороги P1, P2, веса w1–w4 и N.
        candidates_count: Число возвращённых кандидатов.
        failed: Подбор не выполнен (поиск недоступен).
        recommendations: Рекомендации сеанса по позициям.
    """

    __tablename__ = "search_sessions"
    __table_args__ = (Index("ix_search_sessions_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ticket_id: Mapped[int | None] = mapped_column(
        ForeignKey("tickets.id", ondelete="SET NULL"), index=True
    )
    query_text: Mapped[str] = mapped_column(Text)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    component_id: Mapped[int | None] = mapped_column(ForeignKey("components.id"))
    mode: Mapped[str] = mapped_column(String(32))
    model_version: Mapped[str] = mapped_column(String(200))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    candidates_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    failed: Mapped[bool] = mapped_column(default=False, server_default=false())

    recommendations: Mapped[list["Recommendation"]] = relationship(
        back_populates="session",
        order_by="Recommendation.position",
        cascade="all, delete-orphan",
    )


class Recommendation(Base):
    """Предложение системы связать запрос с найденным обращением (Ф4, Ф9).

    Attributes:
        id: Идентификатор.
        session_id: Сеанс поиска.
        ticket_id: Обращение-кандидат.
        position: Позиция в выдаче, от 1 до 10; верхняя граница — предел N
            из ТЗ, конкретное N задаётся конфигурацией.
        score: Итоговый коэффициент сходства S.
        s_sem: Семантическая составляющая.
        s_lex: Лексическая составляющая.
        s_cat: Совпадение категории.
        s_comp: Совпадение компонента.
        suggested_kind: Предложенный вид связи.
        explanation: Объяснение сходства для пользователя.
        verdict: Решение человека.
        verdict_at: Дата вердикта; пуста тогда и только тогда, когда
            вердикт «не рассмотрена».
        verdict_by: Автор вердикта; пуст при том же условии.
        session: Сеанс поиска (объект).
    """

    __tablename__ = "recommendations"
    __table_args__ = (
        UniqueConstraint("session_id", "position"),
        UniqueConstraint("session_id", "ticket_id"),
        CheckConstraint("position BETWEEN 1 AND 10", name="position_range"),
        CheckConstraint("score BETWEEN 0 AND 1", name="score_range"),
        CheckConstraint(
            "(verdict = 'pending') = (verdict_at IS NULL AND verdict_by IS NULL)",
            name="verdict_consistent",
        ),
        Index("ix_recommendations_ticket_id", "ticket_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("search_sessions.id", ondelete="CASCADE"))
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(SmallInteger)
    score: Mapped[float] = mapped_column(Float)
    s_sem: Mapped[float] = mapped_column(Float, default=0.0)
    s_lex: Mapped[float] = mapped_column(Float, default=0.0)
    s_cat: Mapped[float] = mapped_column(Float, default=0.0)
    s_comp: Mapped[float] = mapped_column(Float, default=0.0)
    suggested_kind: Mapped[LinkKind] = mapped_column(pg_enum(LinkKind, "link_kind"))
    explanation: Mapped[str] = mapped_column(Text, default="")
    verdict: Mapped[Verdict] = mapped_column(
        pg_enum(Verdict, "verdict"),
        default=Verdict.PENDING,
        server_default=Verdict.PENDING.value,
    )
    verdict_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verdict_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    session: Mapped[SearchSession] = relationship(back_populates="recommendations")
