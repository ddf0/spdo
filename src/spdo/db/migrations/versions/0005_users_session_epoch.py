"""Версия сессий пользователя.

Ревизия: 0005
Предыдущая: 0004
Создана: 2026-11-08 20:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавляет версию сессий пользователя (сброс сессий при смене пароля)."""
    op.add_column(
        "users", sa.Column("session_epoch", sa.Integer(), server_default="0", nullable=False)
    )


def downgrade() -> None:
    """Откатывает изменения схемы."""
    op.drop_column("users", "session_epoch")
