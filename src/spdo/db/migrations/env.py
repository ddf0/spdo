"""Окружение Alembic: подключение к БД и метаданные моделей СПДО."""

from logging.config import fileConfig
from typing import Any

from alembic import context
from alembic.autogenerate.api import AutogenContext
from pgvector.sqlalchemy import Vector
from sqlalchemy import engine_from_config, pool

from spdo.config import settings
from spdo.db import models  # noqa: F401  регистрирует таблицы всех модулей
from spdo.db.base import Base

config = context.config
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | bool:
    """Отображает тип ``Vector`` в миграции вместе с его импортом.

    Без этого автогенерация ссылается на внутренний путь pgvector и не
    добавляет импорт, и миграция не запускается.

    Args:
        type_: Вид отображаемого элемента.
        obj: Отображаемый объект.
        autogen_context: Контекст автогенерации.

    Returns:
        Текст выражения для ``Vector`` или ``False`` — отображение по умолчанию.
    """
    if type_ == "type" and isinstance(obj, Vector):
        autogen_context.imports.add("from pgvector.sqlalchemy import Vector")
        return f"Vector({obj.dim})"
    return False


def run_migrations_offline() -> None:
    """Формирует SQL-скрипт миграции без подключения к БД."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Применяет миграции к БД в одной транзакции."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
