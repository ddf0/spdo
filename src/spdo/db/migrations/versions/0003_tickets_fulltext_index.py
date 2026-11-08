"""Индекс полнотекстового поиска обращений.

Ревизия: 0003
Предыдущая: 0002
Создана: 2026-11-08 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Выражение документа; должно совпадать с ``spdo.tickets.registry.fts_document``.
FTS_EXPRESSION = "to_tsvector('russian'::regconfig, title || ' ' || description)"


def upgrade() -> None:
    """Применяет изменения схемы.

    GIN-индекс по выражению строится без CONCURRENTLY: на время построения
    запись в ``tickets`` блокируется; на больших данных миграцию выполняют в
    окно обслуживания.
    """
    op.create_index("ix_tickets_fts", "tickets", [sa.text(FTS_EXPRESSION)], postgresql_using="gin")


def downgrade() -> None:
    """Откатывает изменения схемы."""
    op.drop_index("ix_tickets_fts", table_name="tickets", postgresql_using="gin")
