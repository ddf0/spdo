"""Страница массовых однотипных обращений (Ф20)."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from spdo.config import settings
from spdo.incidents import service as incidents
from spdo.search.base import SearchUnavailableError
from spdo.tickets.access import (
    PermissionDeniedError,
    TicketNotFoundError,
    TicketValidationError,
    is_staff,
)
from spdo.users import service as users
from spdo.users.deps import CurrentUser, DbSession, verify_csrf
from spdo.web.templating import STATUS_LABELS, templates

router = APIRouter(prefix="/incidents", dependencies=[Depends(verify_csrf)])


def _page(
    request: Request,
    session: Session,
    user: CurrentUser,
    *,
    result: incidents.MergeResult | None = None,
    error: str | None = None,
    code: int = status.HTTP_200_OK,
) -> Response:
    """Страница групп; при недоступном поиске — с предупреждением.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.
        result: Итог объединения для показа.
        error: Текст ошибки.
        code: Код ответа.

    Returns:
        Страница или 403.
    """
    if not is_staff(user):
        return templates.TemplateResponse(
            request,
            "forbidden.html",
            {"user": user, "message": "Раздел доступен оператору и администратору."},
            status_code=status.HTTP_403_FORBIDDEN,
        )
    groups, unavailable = [], False
    try:
        groups = incidents.detect(session, user, datetime.now(UTC))
    except SearchUnavailableError:
        unavailable = True
    authors = {t.author_id for g in groups for t in g.tickets}
    return templates.TemplateResponse(
        request,
        "incidents.html",
        {
            "user": user,
            "groups": groups,
            "unavailable": unavailable,
            "names": users.display_names(session, authors),
            "status_labels": STATUS_LABELS,
            "window": settings.incident_window_hours,
            "min_size": settings.incident_min_size,
            "result": result,
            "error": error,
        },
        status_code=code,
    )


@router.get("", response_class=HTMLResponse)
def incidents_page(request: Request, session: DbSession, user: CurrentUser) -> Response:
    """Показывает группы однотипных обращений за окно (Ф20).

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Страница групп или 403.
    """
    return _page(request, session, user)


@router.post("/merge", response_class=HTMLResponse)
def merge(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    main_id: Annotated[str, Form()] = "",
    ticket_ids: Annotated[list[str], Form()] = [],  # noqa: B006 — значение FastAPI по умолчанию
) -> Response:
    """Объединяет выбранные обращения в инцидент с основным обращением.

    Итог показывается сразу: повторная отправка формы безопасна — уже
    объединённые обращения попадут в пропущенные.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.
        main_id: Основное обращение.
        ticket_ids: Обращения группы.

    Returns:
        Страница с итогом или с ошибкой.
    """
    if not is_staff(user):
        return _page(request, session, user)
    try:
        ids = [int(v) for v in ticket_ids]
        result = incidents.merge(session, user, int(main_id), ids)
    except ValueError:
        session.rollback()
        return _page(request, session, user, error="Неверный номер обращения", code=400)
    except (TicketValidationError, TicketNotFoundError, PermissionDeniedError) as exc:
        session.rollback()
        return _page(request, session, user, error=str(exc), code=400)
    session.commit()
    return _page(request, session, user, result=result)
