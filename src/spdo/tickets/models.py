"""Модели обращений, справочников, черновиков, решений и истории изменений."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from spdo.db.base import Base, CreatedAtMixin, pg_enum

#: Максимальная длина заголовка обращения (Ф1).
TITLE_MAX = 200
#: Максимальная длина описания обращения (Ф1).
DESCRIPTION_MAX = 4000


class TicketStatus(StrEnum):
    """Статус обращения; допустимые переходы задаёт таблица из ТЗ."""

    NEW = "new"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CLOSED_DUPLICATE = "closed_duplicate"


class ChangeKind(StrEnum):
    """Вид изменения обращения в истории (Ф13)."""

    CREATED = "created"
    STATUS = "status"
    CONFIDENTIALITY = "confidentiality"
    CATEGORY = "category"
    COMPONENT = "component"
    SOLUTION = "solution"
    LINK = "link"


class Category(Base):
    """Справочник категорий обращений.

    Attributes:
        id: Идентификатор.
        name: Наименование, уникальное.
        description: Пояснение для пользователя.
    """

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")


class Component(Base):
    """Справочник компонентов (подсистем), к которым относятся обращения.

    Attributes:
        id: Идентификатор.
        name: Наименование, уникальное.
    """

    __tablename__ = "components"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)


class Ticket(CreatedAtMixin, Base):
    """Зарегистрированное обращение в техническую поддержку.

    Основное обращение для дубликата не хранится здесь: его задаёт
    единственная связь вида «дубликат» в модуле ``relations``. Векторное
    представление хранится в поисковом индексе модуля ``search``.

    Attributes:
        id: Идентификатор; служит и регистрационным номером.
        title: Заголовок, до 200 символов.
        description: Описание, до 4000 символов.
        status: Текущий статус.
        confidential: Не показывать в рекомендациях посторонним.
        author_id: Автор обращения.
        category_id: Категория.
        component_id: Компонент.
        updated_at: Дата последнего изменения.
        category: Категория (объект).
        component: Компонент (объект).
        solution: Опубликованное решение или ``None``.
    """

    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(f"char_length(title) BETWEEN 1 AND {TITLE_MAX}", name="title_len"),
        CheckConstraint(
            f"char_length(description) BETWEEN 1 AND {DESCRIPTION_MAX}", name="description_len"
        ),
        Index("ix_tickets_status", "status"),
        Index("ix_tickets_category_id", "category_id"),
        Index("ix_tickets_component_id", "component_id"),
        Index("ix_tickets_author_id", "author_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(TITLE_MAX))
    description: Mapped[str] = mapped_column(String(DESCRIPTION_MAX))
    status: Mapped[TicketStatus] = mapped_column(
        pg_enum(TicketStatus, "ticket_status"),
        default=TicketStatus.NEW,
        server_default=TicketStatus.NEW.value,
    )
    confidential: Mapped[bool] = mapped_column(default=False, server_default=false())
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    component_id: Mapped[int] = mapped_column(ForeignKey("components.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    category: Mapped[Category] = relationship()
    component: Mapped[Component] = relationship()
    solution: Mapped["Solution | None"] = relationship(
        back_populates="ticket", cascade="all, delete-orphan", passive_deletes=True
    )


class TicketDraft(Base):
    """Черновик обращения, сохраняемый при вводе (Ф2).

    Поля необязательны: черновик хранит текст в любом состоянии, чтобы
    он не терялся при ошибке поиска или обрыве соединения.

    Attributes:
        id: Идентификатор.
        author_id: Владелец черновика.
        title: Введённый заголовок.
        description: Введённое описание.
        category_id: Выбранная категория или ``None``.
        component_id: Выбранный компонент или ``None``.
        updated_at: Дата последнего сохранения.
    """

    __tablename__ = "ticket_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(TITLE_MAX), default="")
    description: Mapped[str] = mapped_column(String(DESCRIPTION_MAX), default="")
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL")
    )
    component_id: Mapped[int | None] = mapped_column(
        ForeignKey("components.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Solution(CreatedAtMixin, Base):
    """Опубликованное решение обращения (Ф12).

    У обращения не более одного решения; повторная публикация заменяет
    текст, прежний сохраняется в истории изменений.

    Attributes:
        id: Идентификатор.
        ticket_id: Решённое обращение.
        text: Текст решения.
        author_id: Оператор, опубликовавший решение.
        ticket: Решённое обращение (объект).
    """

    __tablename__ = "solutions"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), unique=True
    )
    text: Mapped[str] = mapped_column(Text)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))

    ticket: Mapped[Ticket] = relationship(back_populates="solution")


class SolutionRating(Base):
    """Оценка решения «помогло / не помогло» (Ф6).

    Один пользователь оценивает решение не более одного раза; повторная
    оценка заменяет прежнюю.

    Attributes:
        id: Идентификатор.
        solution_id: Оцениваемое решение.
        user_id: Автор оценки.
        helped: ``True`` — помогло, ``False`` — не помогло.
        rated_at: Дата последней оценки.
    """

    __tablename__ = "solution_ratings"
    __table_args__ = (UniqueConstraint("solution_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    solution_id: Mapped[int] = mapped_column(ForeignKey("solutions.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    helped: Mapped[bool]
    rated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class HistoryEntry(CreatedAtMixin, Base):
    """Запись истории изменений обращения (Ф13).

    Attributes:
        id: Идентификатор.
        ticket_id: Изменённое обращение.
        kind: Вид изменения.
        old_value: Значение до изменения или ``None``.
        new_value: Значение после изменения или ``None``.
        comment: Комментарий автора изменения.
        author_id: Автор изменения.
    """

    __tablename__ = "ticket_history"
    __table_args__ = (Index("ix_ticket_history_ticket_id_created_at", "ticket_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"))
    kind: Mapped[ChangeKind] = mapped_column(pg_enum(ChangeKind, "change_kind"))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str] = mapped_column(Text, default="")
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
