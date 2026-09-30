"""Декларативная база моделей SQLAlchemy.

Соглашение об именах ограничений нужно Alembic: без него автогенерация
миграций не может надёжно удалять и переименовывать ограничения.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Шаблоны имён индексов и ограничений.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Базовый класс всех моделей СПДО."""

    #: Метаданные схемы с соглашением об именах ограничений.
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class CreatedAtMixin:
    """Примесь с датой создания записи.

    Attributes:
        created_at: Дата и время создания, заполняется сервером БД.
    """

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def pg_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    """Создаёт тип перечисления PostgreSQL, хранящий значения, а не имена.

    Args:
        enum_cls: Перечисление Python.
        name: Имя типа в PostgreSQL.

    Returns:
        Тип столбца SQLAlchemy.
    """
    return Enum(
        enum_cls,
        name=name,
        values_callable=lambda members: [m.value for m in members],
        validate_strings=True,
    )
