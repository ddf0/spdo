"""Модель журнала действий пользователей."""

from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
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


class QualityMeasurement(CreatedAtMixin, Base):
    """Замер качества подбора на эталонном наборе (ТЗ, п. 8.2; ТТ.К2).

    Записывается командой ``spdo eval --save``; последний замер на
    проверочной части показывается в статистике (Ф14). Вместе с
    показателями хранятся версия модели, параметры и контрольная сумма
    проверочной части, чтобы замеры разных релизов были сопоставимы.

    Attributes:
        id: Идентификатор.
        release: Версия приложения, в которой выполнен замер.
        dataset: Абсолютный путь к каталогу набора.
        split: Часть набора: ``holdout`` или ``tune``.
        holdout_checksum: SHA-256 проверочной части набора.
        mode: Режим поиска.
        model_version: Версия модели и алгоритма.
        params: Пороги и веса.
        queries: Число запросов с дубликатами.
        recall_at_3: recall@3 по ранжированию.
        recall_at_3_shown: recall@3 среди показанных (S ≥ P2).
        hit_at_3: Доля запросов с дубликатом в первых трёх.
        mrr: Средняя обратная позиция первого дубликата.
        false_duplicate_rate: Доля запросов с ложным «дубликатом».
    """

    __tablename__ = "quality_measurements"
    __table_args__ = (
        CheckConstraint("split IN ('holdout', 'tune')", name="split"),
        CheckConstraint("recall_at_3 BETWEEN 0 AND 1", name="recall_range"),
        Index("ix_quality_measurements_split_created_at", "split", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    release: Mapped[str] = mapped_column(String(50))
    dataset: Mapped[str] = mapped_column(String(500))
    split: Mapped[str] = mapped_column(String(16))
    holdout_checksum: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(32))
    model_version: Mapped[str] = mapped_column(String(200))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    queries: Mapped[int] = mapped_column(Integer)
    recall_at_3: Mapped[float] = mapped_column(Float)
    recall_at_3_shown: Mapped[float] = mapped_column(Float)
    hit_at_3: Mapped[float] = mapped_column(Float)
    mrr: Mapped[float] = mapped_column(Float)
    false_duplicate_rate: Mapped[float] = mapped_column(Float)
