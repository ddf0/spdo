"""Схемы запросов и ответов API пользователей."""

from pydantic import BaseModel, ConfigDict, Field

from spdo.users.models import Role


class UserOut(BaseModel):
    """Пользователь в ответе API; хеш пароля не передаётся.

    Attributes:
        id: Идентификатор.
        username: Имя для входа.
        full_name: Отображаемое имя.
        role: Роль.
        is_active: Признак разрешённого входа.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    full_name: str
    role: Role
    is_active: bool


class UserCreate(BaseModel):
    """Запрос на создание пользователя администратором.

    Attributes:
        username: Имя для входа.
        full_name: Отображаемое имя.
        password: Начальный пароль.
        role: Роль.
    """

    username: str = Field(min_length=3, max_length=64)
    full_name: str = Field(default="", max_length=200)
    password: str = Field(max_length=256, repr=False)
    role: Role = Role.USER


class UserUpdate(BaseModel):
    """Запрос на изменение пользователя; ``None`` — поле не меняется.

    Attributes:
        full_name: Отображаемое имя.
        role: Роль.
        is_active: Признак разрешённого входа.
    """

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    role: Role | None = None
    is_active: bool | None = None


class PasswordChange(BaseModel):
    """Запрос на замену пароля.

    Attributes:
        password: Новый пароль.
    """

    password: str = Field(max_length=256, repr=False)
