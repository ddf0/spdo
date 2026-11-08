"""Маршруты входа, выхода и администрирования пользователей."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from spdo.config import settings
from spdo.users import service
from spdo.users.deps import (
    CurrentUser,
    DbSession,
    login_session,
    logout_session,
    require_admin,
    verify_csrf,
)
from spdo.users.models import User
from spdo.users.schemas import PasswordChange, UserCreate, UserOut, UserUpdate
from spdo.users.security import PasswordPolicyError
from spdo.users.throttle import LoginThrottle
from spdo.web.templating import templates

router = APIRouter()

#: Учёт неудачных входов процесса приложения.
throttle = LoginThrottle(settings.login_window_seconds)

#: Администратор как зависимость маршрута.
AdminUser = Annotated[User, Depends(require_admin)]


def safe_next(target: str | None) -> str:
    """Возвращает безопасный адрес перехода после входа.

    Допускаются только относительные пути этого сайта, иначе открытое
    перенаправление позволило бы увести пользователя на чужой сайт.

    Args:
        target: Запрошенный адрес.

    Returns:
        Тот же путь или ``/``.
    """
    if target and target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return "/"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next_url: Annotated[str, Query(alias="next")] = "/") -> Response:
    """Показывает форму входа.

    Args:
        request: Текущий запрос.
        next_url: Адрес перехода после входа (параметр ``next``).

    Returns:
        Страница входа.
    """
    context = {"next": safe_next(next_url), "username": "", "error": None, "user": None}
    return templates.TemplateResponse(request, "login.html", context)


def _too_many(wait: float) -> str:
    """Сообщение о временном запрете попыток."""
    return f"Слишком много неудачных попыток. Повторите через {max(1, round(wait / 60))} мин."


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(
    request: Request,
    session: DbSession,
    username: Annotated[str, Form(max_length=64)],
    password: Annotated[str, Form(max_length=256)],
    next_url: Annotated[str, Form(alias="next")] = "/",
) -> Response:
    """Проверяет имя и пароль и начинает сессию.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        username: Имя для входа.
        password: Пароль.
        next_url: Адрес перехода после входа (поле ``next``).

    Returns:
        Перенаправление после успешного входа; форма с ошибкой — 401 при
        неверных данных, 429 при превышении числа неудачных попыток.
    """
    user_key = "user:" + service.normalize_username(username)
    ip_key = "ip:" + (request.client.host if request.client else "")
    limits = {user_key: settings.login_max_failures, ip_key: settings.login_max_failures_ip}
    wait = throttle.reserve(limits)  # попытка учтена до проверки пароля
    user = None if wait is not None else service.authenticate(session, username, password)
    if user is None:
        if wait is not None:
            error, code = _too_many(wait), status.HTTP_429_TOO_MANY_REQUESTS
        else:
            error, code = "Неверное имя пользователя или пароль", status.HTTP_401_UNAUTHORIZED
        context = {"next": safe_next(next_url), "username": username, "error": error, "user": None}
        return templates.TemplateResponse(request, "login.html", context, status_code=code)
    throttle.release(user_key, [ip_key])
    login_session(request, user)
    return RedirectResponse(safe_next(next_url), status_code=status.HTTP_303_SEE_OTHER)


def _password_error(
    request: Request, user: User, error: str, code: int = status.HTTP_400_BAD_REQUEST
) -> Response:
    """Форма смены пароля с ошибкой."""
    return templates.TemplateResponse(
        request,
        "account_password.html",
        {"user": user, "error": error, "done": False},
        status_code=code,
    )


@router.get("/account/password", response_class=HTMLResponse)
def password_page(request: Request, user: CurrentUser) -> Response:
    """Показывает форму смены собственного пароля.

    Args:
        request: Текущий запрос.
        user: Текущий пользователь.

    Returns:
        Страница смены пароля.
    """
    done = request.query_params.get("done") == "1"
    return templates.TemplateResponse(
        request, "account_password.html", {"user": user, "error": None, "done": done}
    )


@router.post("/account/password", dependencies=[Depends(verify_csrf)])
def change_own_password(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    current: Annotated[str, Form(max_length=256)] = "",
    new: Annotated[str, Form(max_length=256)] = "",
    confirm: Annotated[str, Form(max_length=256)] = "",
) -> Response:
    """Меняет собственный пароль пользователя.

    Остальные открытые сессии пользователя завершаются; текущая сессия
    открывается заново и продолжает работать.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.
        current: Текущий пароль.
        new: Новый пароль.
        confirm: Повтор нового пароля.

    Returns:
        Перенаправление; форма с ошибкой — 400, или 429 после
        :attr:`~spdo.config.Settings.login_max_failures` неверных текущих
        паролей за окно.
    """
    key = f"password:{user.id}"
    if new != confirm:
        return _password_error(request, user, "Новый пароль и его повтор не совпадают")
    wait = throttle.reserve({key: settings.login_max_failures})
    if wait is not None:
        return _password_error(request, user, _too_many(wait), status.HTTP_429_TOO_MANY_REQUESTS)
    try:
        epoch = service.change_own_password(session, user.id, current, new)
    except service.WrongPasswordError as exc:  # попытка остаётся учтённой
        session.rollback()
        return _password_error(request, user, str(exc))
    except PasswordPolicyError as exc:
        session.rollback()
        throttle.release(key, [])
        return _password_error(request, user, str(exc))
    session.commit()
    throttle.release(key, [])
    login_session(request, user, epoch)
    return RedirectResponse("/account/password?done=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request) -> Response:
    """Завершает сессию.

    Args:
        request: Текущий запрос.

    Returns:
        Перенаправление на страницу входа.
    """
    logout_session(request)
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/api/me")
def me(user: CurrentUser) -> UserOut:
    """Возвращает текущего пользователя.

    Args:
        user: Текущий пользователь.

    Returns:
        Данные пользователя.
    """
    return UserOut.model_validate(user)


#: Сначала проверяются права (401/403), затем CSRF-токен.
admin = APIRouter(
    prefix="/api/admin/users", dependencies=[Depends(require_admin), Depends(verify_csrf)]
)


def _bad_request(exc: Exception) -> HTTPException:
    """Превращает ошибку проверки данных в ответ 400."""
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


@admin.get("")
def list_users(session: DbSession, _: AdminUser) -> list[UserOut]:
    """Возвращает всех пользователей.

    Args:
        session: Сессия БД.
        _: Текущий администратор.

    Returns:
        Список пользователей.
    """
    return [UserOut.model_validate(u) for u in service.list_users(session)]


@admin.post("", status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, session: DbSession, _: AdminUser) -> UserOut:
    """Создаёт пользователя.

    Args:
        body: Данные нового пользователя.
        session: Сессия БД.
        _: Текущий администратор.

    Returns:
        Созданный пользователь.

    Raises:
        HTTPException: 400 — неверное имя или пароль, 409 — имя занято.
    """
    try:
        user = service.create_user(
            session,
            username=body.username,
            full_name=body.full_name,
            password=body.password,
            role=body.role,
        )
    except service.UsernameTakenError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (service.InvalidUsernameError, PasswordPolicyError) as exc:
        raise _bad_request(exc) from exc
    session.commit()
    return UserOut.model_validate(user)


@admin.patch("/{user_id}")
def update_user(user_id: int, body: UserUpdate, session: DbSession, _: AdminUser) -> UserOut:
    """Изменяет имя, роль или активность пользователя.

    Args:
        user_id: Идентификатор пользователя.
        body: Изменяемые поля.
        session: Сессия БД.
        _: Текущий администратор.

    Returns:
        Изменённый пользователь.

    Raises:
        HTTPException: 404 — пользователь не найден, 409 — последний
            администратор.
    """
    try:
        user = service.update_user(
            session, user_id, full_name=body.full_name, role=body.role, is_active=body.is_active
        )
    except service.UserNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except service.LastAdminError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    session.commit()
    return UserOut.model_validate(user)


@admin.put("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    user_id: int, body: PasswordChange, session: DbSession, _: AdminUser
) -> Response:
    """Заменяет пароль пользователя.

    Args:
        user_id: Идентификатор пользователя.
        body: Новый пароль.
        session: Сессия БД.
        _: Текущий администратор.

    Returns:
        Пустой ответ 204.

    Raises:
        HTTPException: 404 — пользователь не найден, 400 — пароль не
            удовлетворяет требованиям.
    """
    try:
        service.set_password(session, user_id, body.password)
    except service.UserNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except PasswordPolicyError as exc:
        raise _bad_request(exc) from exc
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


router.include_router(admin)
