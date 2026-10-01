"""Зависимости FastAPI: текущий пользователь, проверка ролей и CSRF.

Аутентификация — по подписанной cookie сессии (``SessionMiddleware``).
Права проверяются на сервере в каждой защищённой операции независимо от
клиентской части (требование ТУ).
"""

import hmac
import secrets
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from spdo.db.session import get_session
from spdo.users.models import Role, User
from spdo.users.service import get_active_user

#: Ключ идентификатора пользователя в сессии.
SESSION_USER_KEY = "uid"
#: Ключ CSRF-токена в сессии.
SESSION_CSRF_KEY = "csrf"
#: Заголовок, в котором клиент передаёт CSRF-токен.
CSRF_HEADER = "X-CSRF-Token"
#: Поле формы с CSRF-токеном.
CSRF_FORM_FIELD = "csrf_token"
#: Методы HTTP, не изменяющие состояние и не требующие CSRF-токена.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


#: Сессия БД как зависимость FastAPI.
DbSession = Annotated[Session, Depends(get_session)]


class NotAuthenticatedError(Exception):
    """Запрос требует входа в систему.

    Обработчик в приложении превращает ошибку в перенаправление на
    страницу входа для браузера или в ответ 401 для API.
    """


def login_session(request: Request, user: User) -> None:
    """Начинает сессию пользователя.

    Прежнее содержимое сессии удаляется, CSRF-токен создаётся заново —
    это исключает фиксацию сессии.

    Args:
        request: Текущий запрос.
        user: Аутентифицированный пользователь.
    """
    request.session.clear()
    request.session[SESSION_USER_KEY] = user.id
    request.session[SESSION_CSRF_KEY] = secrets.token_urlsafe(32)


def logout_session(request: Request) -> None:
    """Завершает сессию пользователя.

    Args:
        request: Текущий запрос.
    """
    request.session.clear()


def csrf_token(request: Request) -> str:
    """Возвращает CSRF-токен сессии, создавая его при необходимости.

    Args:
        request: Текущий запрос.

    Returns:
        Токен для вставки в форму или заголовок.
    """
    token = request.session.get(SESSION_CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[SESSION_CSRF_KEY] = token
    return token


async def verify_csrf(request: Request) -> None:
    """Проверяет CSRF-токен в изменяющих запросах.

    Токен берётся из заголовка ``X-CSRF-Token`` или из поля формы
    ``csrf_token`` (только ``application/x-www-form-urlencoded``, чтобы
    не разбирать загрузку файлов до проверки прав) и сравнивается с
    токеном сессии за постоянное время.

    Args:
        request: Текущий запрос.

    Raises:
        HTTPException: 403, если токен отсутствует или не совпадает.
    """
    if request.method in SAFE_METHODS:
        return
    expected = request.session.get(SESSION_CSRF_KEY)
    sent = request.headers.get(CSRF_HEADER)
    content_type = request.headers.get("content-type", "")
    if sent is None and content_type.startswith("application/x-www-form-urlencoded"):
        value = (await request.form()).get(CSRF_FORM_FIELD)
        sent = value if isinstance(value, str) else None
    if not expected or not sent or not hmac.compare_digest(expected.encode(), sent.encode()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Неверный CSRF-токен")


def get_optional_user(request: Request, session: DbSession) -> User | None:
    """Возвращает пользователя текущей сессии, если он вошёл.

    Если пользователь удалён или заблокирован после входа, сессия
    очищается.

    Args:
        request: Текущий запрос.
        session: Сессия БД.

    Returns:
        Активный пользователь или ``None``.
    """
    user_id = request.session.get(SESSION_USER_KEY)
    if user_id is None:
        return None
    user = get_active_user(session, int(user_id))
    if user is None:
        logout_session(request)
    return user


def get_current_user(user: Annotated[User | None, Depends(get_optional_user)]) -> User:
    """Возвращает пользователя текущей сессии.

    Args:
        user: Пользователь сессии или ``None``.

    Returns:
        Активный пользователь.

    Raises:
        NotAuthenticatedError: Пользователь не вошёл в систему.
    """
    if user is None:
        raise NotAuthenticatedError
    return user


#: Текущий пользователь как зависимость FastAPI.
CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: Role) -> Callable[[User], User]:
    """Создаёт зависимость, допускающую только указанные роли.

    Args:
        *roles: Разрешённые роли.

    Returns:
        Зависимость FastAPI, возвращающая текущего пользователя.
    """
    allowed = frozenset(roles)

    def dependency(user: CurrentUser) -> User:
        """Пропускает пользователя с разрешённой ролью.

        Raises:
            HTTPException: 403, если роль не разрешена.
        """
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Недостаточно прав")
        return user

    return dependency


#: Зависимость: только оператор или администратор.
require_operator = require_roles(Role.OPERATOR, Role.ADMIN)
#: Зависимость: только администратор.
require_admin = require_roles(Role.ADMIN)
