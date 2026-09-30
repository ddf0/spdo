"""Модель пользователя и перечисление ролей."""

from enum import StrEnum

from sqlalchemy import String, true
from sqlalchemy.orm import Mapped, mapped_column

from spdo.db.base import Base, CreatedAtMixin, pg_enum


class Role(StrEnum):
    """Роль пользователя, определяющая его права."""

    USER = "user"
    OPERATOR = "operator"
    ADMIN = "admin"


class User(CreatedAtMixin, Base):
    """Учётная запись пользователя.

    Attributes:
        id: Идентификатор.
        username: Имя для входа, уникальное.
        full_name: Отображаемое имя.
        role: Роль пользователя.
        password_hash: Хеш пароля с солью; открытый пароль не хранится.
        is_active: Признак разрешённого входа.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[Role] = mapped_column(
        pg_enum(Role, "user_role"), default=Role.USER, server_default=Role.USER.value
    )
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
