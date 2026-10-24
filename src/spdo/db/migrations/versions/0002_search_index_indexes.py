"""Индексы поискового индекса.

Ревизия: 0002
Предыдущая: 0001
Создана: 2026-10-24 16:29:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Применяет изменения схемы.

    Индексы строятся без CONCURRENTLY: на время построения запись в
    ``search_index`` блокируется. Для пустой или небольшой таблицы при
    развёртывании это допустимо; на больших данных миграцию выполняют в
    окно обслуживания (память построения HNSW — ``maintenance_work_mem``).
    """
    op.create_index(
        "ix_search_index_embedding_hnsw",
        "search_index",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_search_index_updated_at", "search_index", ["updated_at"], unique=False)


def downgrade() -> None:
    """Откатывает изменения схемы."""
    op.drop_index("ix_search_index_updated_at", table_name="search_index")
    op.drop_index(
        "ix_search_index_embedding_hnsw",
        table_name="search_index",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
