"""Сервисные функции модуля пользователей.

Функции не фиксируют транзакцию: это делает вызывающий код (обработчик
запроса или команда CLI), чтобы несколько изменений попадали в одну
транзакцию.
"""

import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from spdo.users.models import Role, User
from spdo.users.security import DUMMY_HASH, hash_password, verify_password

#: Допустимое имя для входа: латиница в нижнем регистре, цифры, ``._-``.
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,64}$")


class UserError(ValueError):
    """Базовая ошибка операций с пользователями."""


class InvalidUsernameError(UserError):
    """Имя для входа не соответствует допустимому формату."""


class UsernameTakenError(UserError):
    """Имя для входа уже занято."""


class UserNotFoundError(UserError):
    """Пользователь не найден."""


class LastAdminError(UserError):
    """Операция оставила бы систему без активного администратора."""


def normalize_username(username: str) -> str:
    """Приводит имя для входа к каноническому виду.

    Args:
        username: Введённое имя.

    Returns:
        Имя без пробелов по краям в нижнем регистре.
    """
    return username.strip().lower()


def create_user(
    session: Session, *, username: str, full_name: str, password: str, role: Role = Role.USER
) -> User:
    """Создаёт учётную запись.

    Args:
        session: Сессия БД.
        username: Имя для входа.
        full_name: Отображаемое имя.
        password: Открытый пароль; сохраняется только его хеш.
        role: Роль пользователя.

    Returns:
        Созданный пользователь.

    Raises:
        InvalidUsernameError: Имя не соответствует формату.
        UsernameTakenError: Имя уже занято.
        PasswordPolicyError: Пароль не удовлетворяет требованиям.
    """
    name = normalize_username(username)
    if not USERNAME_RE.fullmatch(name):
        raise InvalidUsernameError("Имя: 3–64 символа, латиница, цифры, «.», «_», «-»")
    if session.scalar(select(User.id).where(User.username == name)) is not None:
        raise UsernameTakenError(f"Имя «{name}» уже занято")
    user = User(
        username=name,
        full_name=full_name.strip() or name,
        password_hash=hash_password(password),
        role=role,
    )
    try:
        with session.begin_nested():
            session.add(user)
    except IntegrityError as exc:
        # Параллельный запрос успел занять имя между проверкой и вставкой.
        raise UsernameTakenError(f"Имя «{name}» уже занято") from exc
    return user


def authenticate(session: Session, username: str, password: str) -> User | None:
    """Проверяет имя и пароль.

    Для несуществующего или заблокированного пользователя пароль всё
    равно сверяется с фиктивным хешем, чтобы по времени ответа нельзя
    было узнать, существует ли учётная запись.

    Args:
        session: Сессия БД.
        username: Имя для входа.
        password: Открытый пароль.

    Returns:
        Пользователь при успешной проверке, иначе ``None``.
    """
    user = session.scalar(select(User).where(User.username == normalize_username(username)))
    if user is None or not user.is_active:
        verify_password(password, DUMMY_HASH)
        return None
    return user if verify_password(password, user.password_hash) else None


def get_active_user(session: Session, user_id: int) -> User | None:
    """Возвращает активного пользователя по идентификатору.

    Args:
        session: Сессия БД.
        user_id: Идентификатор.

    Returns:
        Пользователь или ``None``, если он не найден или заблокирован.
    """
    user = session.get(User, user_id)
    return user if user is not None and user.is_active else None


def list_users(session: Session) -> list[User]:
    """Возвращает всех пользователей, упорядоченных по имени для входа.

    Args:
        session: Сессия БД.

    Returns:
        Список пользователей.
    """
    return list(session.scalars(select(User).order_by(User.username)))


def display_names(session: Session, user_ids: set[int]) -> dict[int, str]:
    """Возвращает отображаемые имена пользователей по идентификаторам.

    Args:
        session: Сессия БД.
        user_ids: Идентификаторы пользователей.

    Returns:
        Идентификатор → отображаемое имя; отсутствующие пропускаются.
    """
    if not user_ids:
        return {}
    rows = session.execute(select(User.id, User.full_name).where(User.id.in_(user_ids)))
    return {uid: name for uid, name in rows}


def _active_admin_count(session: Session) -> int:
    """Считает активных администраторов, блокируя их записи до конца транзакции.

    Блокировка не даёт двум параллельным запросам одновременно снять роль
    с двух последних администраторов.
    """
    stmt = (
        select(User.id).where(User.role == Role.ADMIN, User.is_active.is_(True)).with_for_update()
    )
    return len(session.scalars(stmt).all())


def update_user(
    session: Session,
    user_id: int,
    *,
    full_name: str | None = None,
    role: Role | None = None,
    is_active: bool | None = None,
) -> User:
    """Изменяет имя, роль или признак активности пользователя.

    Args:
        session: Сессия БД.
        user_id: Идентификатор изменяемого пользователя.
        full_name: Новое отображаемое имя или ``None`` — не менять.
        role: Новая роль или ``None`` — не менять.
        is_active: Новый признак активности или ``None`` — не менять.

    Returns:
        Изменённый пользователь.

    Raises:
        UserNotFoundError: Пользователь не найден.
        LastAdminError: Снимается роль или блокируется последний
            активный администратор.
    """
    user = session.get(User, user_id)
    if user is None:
        raise UserNotFoundError(f"Пользователь {user_id} не найден")
    if _loses_admin(user, role, is_active) and _active_admin_count(session) <= 1:
        raise LastAdminError("Нельзя оставить систему без активного администратора")
    _apply_changes(user, full_name, role, is_active)
    session.flush()
    return user


def _apply_changes(
    user: User, full_name: str | None, role: Role | None, is_active: bool | None
) -> None:
    """Записывает в пользователя заданные (не ``None``) значения полей."""
    if full_name is not None and full_name.strip():
        user.full_name = full_name.strip()
    if role is not None:
        user.role = role
    if is_active is not None:
        user.is_active = is_active


def _loses_admin(user: User, role: Role | None, is_active: bool | None) -> bool:
    """Перестаёт ли активный администратор быть им после изменения."""
    if user.role is not Role.ADMIN or not user.is_active:
        return False
    return (role is not None and role is not Role.ADMIN) or is_active is False


def set_password(session: Session, user_id: int, password: str) -> None:
    """Заменяет пароль пользователя.

    Args:
        session: Сессия БД.
        user_id: Идентификатор пользователя.
        password: Новый открытый пароль.

    Raises:
        UserNotFoundError: Пользователь не найден.
        PasswordPolicyError: Пароль не удовлетворяет требованиям.
    """
    user = session.get(User, user_id)
    if user is None:
        raise UserNotFoundError(f"Пользователь {user_id} не найден")
    user.password_hash = hash_password(password)
    session.flush()
