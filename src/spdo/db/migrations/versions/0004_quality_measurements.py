"""Замеры качества подбора.

Ревизия: 0004
Предыдущая: 0003
Создана: 2026-11-08 18:37:14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Создаёт таблицу замеров качества подбора (ТТ.К2)."""
    op.create_table(
        "quality_measurements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("release", sa.String(length=50), nullable=False),
        sa.Column("dataset", sa.String(length=500), nullable=False),
        sa.Column("split", sa.String(length=16), nullable=False),
        sa.Column("holdout_checksum", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("model_version", sa.String(length=200), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("queries", sa.Integer(), nullable=False),
        sa.Column("recall_at_3", sa.Float(), nullable=False),
        sa.Column("recall_at_3_shown", sa.Float(), nullable=False),
        sa.Column("hit_at_3", sa.Float(), nullable=False),
        sa.Column("mrr", sa.Float(), nullable=False),
        sa.Column("false_duplicate_rate", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "split IN ('holdout', 'tune')", name=op.f("ck_quality_measurements_split")
        ),
        sa.CheckConstraint(
            "recall_at_3 BETWEEN 0 AND 1", name=op.f("ck_quality_measurements_recall_range")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quality_measurements")),
    )
    op.create_index(
        "ix_quality_measurements_split_created_at",
        "quality_measurements",
        ["split", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Откатывает изменения схемы."""
    op.drop_index("ix_quality_measurements_split_created_at", table_name="quality_measurements")
    op.drop_table("quality_measurements")
