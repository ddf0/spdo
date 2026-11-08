"""Страницы администратора: пользователи и справочники.

По ТЗ администратор настраивает справочники и учётные записи. Действия
выполняются HTML-формами по схеме «POST → перенаправление → GET»; при
ошибке страница показывается заново с её текстом. Доступ проверяется для
каждого запроса: пользователю без роли администратора — страница 403.
"""

import logging
import math
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.exc import DataError
from sqlalchemy.orm import Session

from spdo.config import SearchMode, settings
from spdo.search import params
from spdo.tickets import dictionaries
from spdo.tickets.access import PermissionDeniedError, TicketNotFoundError, TicketValidationError
from spdo.users import service as users
from spdo.users.deps import CurrentUser, DbSession, verify_csrf
from spdo.users.models import FULL_NAME_MAX, Role
from spdo.users.security import PasswordPolicyError
from spdo.web.templating import ROLE_LABELS, templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", dependencies=[Depends(verify_csrf)])

#: Сообщения после успешного действия (параметр ``done``).
DONE_MESSAGES = {
    "user-created": "Пользователь создан.",
    "user-updated": "Учётная запись изменена.",
    "password": "Пароль заменён.",
    "category": "Справочник категорий изменён.",
    "component": "Справочник компонентов изменён.",
    "search": "Параметры поиска сохранены и применены.",
    "search-reset": "Восстановлены параметры поиска из окружения.",
}
#: Ошибки действий и коды ответа.
_ERROR_CODES: list[tuple[type[Exception], int]] = [
    (PermissionDeniedError, status.HTTP_403_FORBIDDEN),
    (TicketNotFoundError, status.HTTP_404_NOT_FOUND),
    (users.UserNotFoundError, status.HTTP_404_NOT_FOUND),
    (users.UsernameTakenError, status.HTTP_409_CONFLICT),
    (users.LastAdminError, status.HTTP_409_CONFLICT),
    (users.InvalidUsernameError, status.HTTP_400_BAD_REQUEST),
    (PasswordPolicyError, status.HTTP_400_BAD_REQUEST),
    (TicketValidationError, status.HTTP_400_BAD_REQUEST),
    (params.SearchParamsError, status.HTTP_400_BAD_REQUEST),
    # ввод, который БД не может сохранить (например, символ с кодом 0)
    (DataError, status.HTTP_400_BAD_REQUEST),
]


def _forbidden(request: Request, user: CurrentUser) -> Response:
    """Страница 403 для пользователя без роли администратора."""
    return templates.TemplateResponse(
        request,
        "forbidden.html",
        {"user": user, "message": "Раздел доступен администратору."},
        status_code=status.HTTP_403_FORBIDDEN,
    )


def _role(value: str) -> Role:
    """Роль из поля формы.

    Raises:
        TicketValidationError: Роль неизвестна.
    """
    try:
        return Role(value)
    except ValueError as exc:
        raise TicketValidationError("Неизвестная роль") from exc


def _active(value: str) -> bool | None:
    """Признак активности из формы: ``true`` / ``false``; иначе — не менять."""
    return {"true": True, "false": False}.get(value)


def _name(value: str) -> str:
    """Отображаемое имя из формы.

    Raises:
        TicketValidationError: Имя длиннее допустимого.
    """
    if len(value.strip()) > FULL_NAME_MAX:
        raise TicketValidationError(f"Отображаемое имя длиннее {FULL_NAME_MAX} символов")
    return value


def _int(value: str, what: str) -> int:
    """Целое из поля формы.

    Raises:
        TicketValidationError: Значение не число.
    """
    try:
        return int(value)
    except ValueError as exc:
        raise TicketValidationError(f"Неверный номер: {what}") from exc


def _users_page(
    request: Request,
    session: Session,
    user: CurrentUser,
    *,
    error: str | None = None,
    code: int = status.HTTP_200_OK,
) -> Response:
    """Страница пользователей."""
    done = None if error else DONE_MESSAGES.get(request.query_params.get("done", ""))
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "user": user,
            "users": users.list_users(session),
            "roles": ROLE_LABELS,
            "error": error,
            "done": done,
        },
        status_code=code,
    )


def _dictionaries_page(
    request: Request,
    session: Session,
    user: CurrentUser,
    *,
    error: str | None = None,
    code: int = status.HTTP_200_OK,
) -> Response:
    """Страница справочников."""
    done = None if error else DONE_MESSAGES.get(request.query_params.get("done", ""))
    return templates.TemplateResponse(
        request,
        "admin_dictionaries.html",
        {
            "user": user,
            "categories": dictionaries.list_categories(session),
            "components": dictionaries.list_components(session),
            "error": error,
            "done": done,
        },
        status_code=code,
    )


def _act(
    request: Request,
    session: Session,
    user: CurrentUser,
    page: Callable[..., Response],
    url: str,
    action: Callable[[], object],
    after: Callable[[], object] | None = None,
) -> Response:
    """Выполняет действие администратора: успех — перенаправление, ошибка — страница.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.
        page: Построение страницы раздела (для показа ошибки).
        url: Адрес перенаправления после успеха.
        action: Действие в транзакции запроса.
        after: Действие после фиксации транзакции (например, применение
            параметров к процессу); его ошибка показывается на странице.

    Returns:
        Перенаправление, страница с ошибкой или 403.
    """
    if user.role is not Role.ADMIN:
        return _forbidden(request, user)
    try:
        action()
    except tuple(e for e, _ in _ERROR_CODES) as exc:
        session.rollback()
        code = next(c for e, c in _ERROR_CODES if isinstance(exc, e))
        return page(request, session, user, error=str(exc), code=code)
    session.commit()
    if after is not None:
        try:
            after()
        except Exception:  # noqa: BLE001 — запись сохранена, сообщаем о сбое применения
            logger.exception("Действие после сохранения не выполнено")
            error = "Изменение сохранено, но не применено; оно вступит в силу после перезапуска."
            return page(request, session, user, error=error, code=500)
    return RedirectResponse(url, status.HTTP_303_SEE_OTHER)


@router.get("", response_class=HTMLResponse)
def admin_index(request: Request, user: CurrentUser) -> Response:
    """Перенаправляет на страницу пользователей.

    Args:
        request: Текущий запрос.
        user: Текущий пользователь.

    Returns:
        Перенаправление или страница 403.
    """
    if user.role is not Role.ADMIN:
        return _forbidden(request, user)
    return RedirectResponse("/admin/users", status.HTTP_303_SEE_OTHER)


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, session: DbSession, user: CurrentUser) -> Response:
    """Показывает учётные записи и формы управления ими.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Страница пользователей или 403.
    """
    if user.role is not Role.ADMIN:
        return _forbidden(request, user)
    return _users_page(request, session, user)


@router.post("/users")
def create_user(  # noqa: PLR0913 — поля формы
    request: Request,
    session: DbSession,
    user: CurrentUser,
    username: Annotated[str, Form()] = "",
    full_name: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = Role.USER.value,
) -> Response:
    """Создаёт учётную запись.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.
        username: Имя для входа.
        full_name: Отображаемое имя.
        password: Начальный пароль.
        role: Роль.

    Returns:
        Перенаправление или страница с ошибкой.
    """
    return _act(
        request,
        session,
        user,
        _users_page,
        "/admin/users?done=user-created",
        lambda: users.create_user(
            session,
            username=username,
            full_name=_name(full_name),
            password=password,
            role=_role(role),
        ),
    )


@router.post("/users/{user_id:int}")
def update_user(  # noqa: PLR0913 — поля формы
    user_id: int,
    request: Request,
    session: DbSession,
    user: CurrentUser,
    full_name: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    is_active: Annotated[str, Form()] = "",
) -> Response:
    """Меняет имя, роль и признак активности учётной записи.

    Последнего активного администратора нельзя лишить роли или заблокировать.

    Args:
        user_id: Изменяемый пользователь.
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.
        full_name: Новое имя; пусто — не менять.
        role: Новая роль.
        is_active: ``true`` — активна, ``false`` — заблокирована, иначе — не
            менять (форма передаёт скрытое ``false`` перед флажком).

    Returns:
        Перенаправление или страница с ошибкой.
    """
    return _act(
        request,
        session,
        user,
        _users_page,
        "/admin/users?done=user-updated",
        lambda: users.update_user(
            session,
            user_id,
            full_name=_name(full_name) or None,
            role=_role(role),
            is_active=_active(is_active),
        ),
    )


@router.post("/users/{user_id:int}/password")
def reset_password(
    user_id: int,
    request: Request,
    session: DbSession,
    user: CurrentUser,
    password: Annotated[str, Form()] = "",
) -> Response:
    """Задаёт пользователю новый пароль.

    Args:
        user_id: Пользователь.
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.
        password: Новый пароль.

    Returns:
        Перенаправление или страница с ошибкой.
    """
    return _act(
        request,
        session,
        user,
        _users_page,
        "/admin/users?done=password",
        lambda: users.set_password(session, user_id, password),
    )


@router.get("/dictionaries", response_class=HTMLResponse)
def dictionaries_page(request: Request, session: DbSession, user: CurrentUser) -> Response:
    """Показывает справочники категорий и компонентов.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Страница справочников или 403.
    """
    if user.role is not Role.ADMIN:
        return _forbidden(request, user)
    return _dictionaries_page(request, session, user)


@router.post("/categories")
def save_category(  # noqa: PLR0913 — поля формы
    request: Request,
    session: DbSession,
    user: CurrentUser,
    item_id: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
) -> Response:
    """Добавляет категорию или переименовывает существующую.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.
        item_id: Номер категории; пусто — добавить новую.
        name: Наименование.
        description: Пояснение.

    Returns:
        Перенаправление или страница с ошибкой.
    """

    def action() -> None:
        """Добавление или переименование."""
        if item_id:
            category = _int(item_id, "категория")
            dictionaries.rename_category(session, user, category, name, description)
        else:
            dictionaries.create_category(session, user, name, description)

    return _act(
        request, session, user, _dictionaries_page, "/admin/dictionaries?done=category", action
    )


@router.post("/components")
def save_component(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    item_id: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
) -> Response:
    """Добавляет компонент или переименовывает существующий.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.
        item_id: Номер компонента; пусто — добавить новый.
        name: Наименование.

    Returns:
        Перенаправление или страница с ошибкой.
    """

    def action() -> None:
        """Добавление или переименование."""
        if item_id:
            dictionaries.rename_component(session, user, _int(item_id, "компонент"), name)
        else:
            dictionaries.create_component(session, user, name)

    return _act(
        request, session, user, _dictionaries_page, "/admin/dictionaries?done=component", action
    )


#: Подписи настраиваемых параметров поиска (Ф16).
PARAM_LABELS = {
    "top_n": "Число рекомендаций N (1–10)",
    "threshold_duplicate": "P1 — порог «возможный дубликат»",
    "threshold_related": "P2 — порог показа («возможно связанное»)",
    "w_semantic": "w1 — смысловое сходство",
    "w_lexical": "w2 — лексическое сходство",
    "w_category": "w3 — совпадение категории",
    "w_component": "w4 — совпадение компонента",
    "lexical_threshold_duplicate": "P1 — порог «возможный дубликат»",
    "lexical_threshold_related": "P2 — порог показа",
    "lexical_w_lexical": "Вес лексического сходства",
    "lexical_w_category": "Вес совпадения категории",
    "lexical_w_component": "Вес совпадения компонента",
}


def _parse_params(form: dict[str, str]) -> dict[str, object]:
    """Значения параметров из формы: числа с точкой или запятой.

    Сохраняются только значения, отличные от параметров окружения: так
    параметр, который администратор не менял, следует за окружением.

    Args:
        form: Поля формы.

    Returns:
        Перекрытые параметры.

    Raises:
        TicketValidationError: Значение не конечное число или режим неизвестен.
    """
    values: dict[str, object] = {}
    try:
        values["search_mode"] = SearchMode(form.get("search_mode", ""))
    except ValueError as exc:
        raise TicketValidationError("Неизвестный режим поиска") from exc
    for key, label in PARAM_LABELS.items():
        raw = form.get(key, "").strip().replace(",", ".")
        try:
            values[key] = int(raw) if key == "top_n" else float(raw)
        except ValueError as exc:
            raise TicketValidationError(f"«{label}»: нужно число") from exc
        if not math.isfinite(values[key]):
            raise TicketValidationError(f"«{label}»: нужно конечное число")
    return {k: v for k, v in values.items() if v != params.ENV_DEFAULTS[k]}


def _search_page(
    request: Request,
    session: Session,
    user: CurrentUser,
    *,
    error: str | None = None,
    code: int = status.HTTP_200_OK,
) -> Response:
    """Страница параметров поиска.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.
        error: Текст ошибки.
        code: Код ответа.

    Returns:
        Страница.
    """
    done = None if error else DONE_MESSAGES.get(request.query_params.get("done", ""))
    history = params.history(session)
    saved = params.current_values(session)
    return templates.TemplateResponse(
        request,
        "admin_search.html",
        {
            "user": user,
            "config": settings,
            "env": params.ENV_DEFAULTS,
            "overridden": saved,
            "stale": saved != params.applied(),
            "labels": PARAM_LABELS,
            "modes": list(SearchMode),
            "history": history,
            "names": users.display_names(session, {r.author_id for r in history}),
            "error": error,
            "done": done,
        },
        status_code=code,
    )


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, session: DbSession, user: CurrentUser) -> Response:
    """Показывает действующие параметры поиска и форму их изменения (Ф16).

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Страница параметров или 403.
    """
    if user.role is not Role.ADMIN:
        return _forbidden(request, user)
    return _search_page(request, session, user)


async def _admin_form(request: Request, user: CurrentUser) -> dict[str, str]:
    """Поля формы; тело разбирается только для администратора.

    Args:
        request: Текущий запрос.
        user: Текущий пользователь.

    Returns:
        Текстовые поля формы или пустой словарь.
    """
    if user.role is not Role.ADMIN:
        return {}
    return {k: v for k, v in (await request.form()).items() if isinstance(v, str)}


@router.post("/search")
def save_search(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    form: Annotated[dict[str, str], Depends(_admin_form)],
) -> Response:
    """Сохраняет и сразу применяет параметры поиска.

    Значения проверяются так же, как параметры окружения; при ошибке
    ничего не сохраняется и не применяется.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.
        form: Поля формы.

    Returns:
        Перенаправление или страница с ошибкой.
    """

    def action() -> None:
        """Проверка и сохранение."""
        params.save(session, user.id, _parse_params(form), form.get("comment", ""))

    return _act(
        request,
        session,
        user,
        _search_page,
        "/admin/search?done=search",
        action,
        after=lambda: params.apply_latest(session),
    )


@router.post("/search/reset")
def reset_search(request: Request, session: DbSession, user: CurrentUser) -> Response:
    """Возвращает параметры поиска из окружения.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Перенаправление или страница с ошибкой.
    """

    def action() -> None:
        """Пустая запись истории."""
        params.save(session, user.id, {}, "Возврат к параметрам окружения")

    return _act(
        request,
        session,
        user,
        _search_page,
        "/admin/search?done=search-reset",
        action,
        after=lambda: params.apply_latest(session),
    )
