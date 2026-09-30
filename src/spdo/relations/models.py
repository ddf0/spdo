"""Модель связи между обращениями."""

from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from spdo.db.base import Base, CreatedAtMixin, pg_enum


class RelationKind(StrEnum):
    """Вид связи между обращениями."""

    DUPLICATE = "duplicate"
    RELATED = "related"


class Link(CreatedAtMixin, Base):
    """Связь, установленная пользователем или оператором (Ф7, Ф8).

    Для вида «дубликат» источник — дубликат, цель — основное обращение.
    Ограничения схемы:

    * связь обращения с самим собой запрещена;
    * между двумя обращениями не более одной связи любого вида и
      направления;
    * у обращения не более одного основного, поэтому связи «дубликат»
      образуют лес, а запрет циклов сводится к обходу цепочки основных
      (проверяется сервисом).

    Attributes:
        id: Идентификатор.
        source_id: Обращение-источник.
        target_id: Обращение-цель.
        kind: Вид связи.
        author_id: Кто установил связь.
        recommendation_id: Рекомендация, из которой создана связь, или ``None``.
    """

    __tablename__ = "links"
    __table_args__ = (
        CheckConstraint("source_id <> target_id", name="not_self"),
        Index(
            "uq_links_pair",
            func.least(text("source_id"), text("target_id")),
            func.greatest(text("source_id"), text("target_id")),
            unique=True,
        ),
        Index(
            "uq_links_one_main",
            "source_id",
            unique=True,
            postgresql_where=text("kind = 'duplicate'"),
        ),
        Index("ix_links_source_id", "source_id"),
        Index("ix_links_target_id", "target_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    target_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    kind: Mapped[RelationKind] = mapped_column(pg_enum(RelationKind, "relation_kind"))
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    recommendation_id: Mapped[int | None] = mapped_column(
        ForeignKey("recommendations.id", ondelete="SET NULL"), unique=True
    )
