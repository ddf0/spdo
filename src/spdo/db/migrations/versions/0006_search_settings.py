"""Параметры поиска.

Ревизия: 0006
Предыдущая: 0005
Создана: 2026-11-09 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Создаёт таблицу истории параметров поиска (Ф16)."""
    op.create_table(
        "search_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["author_id"], ["users.id"], name=op.f("fk_search_settings_author_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_settings")),
    )


def downgrade() -> None:
    """Откатывает изменения схемы."""
    op.drop_table("search_settings")
