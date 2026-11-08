"""Страница уведомлений пользователя (Ф18)."""

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from spdo.notifications import service as notifications
from spdo.users.deps import CurrentUser, DbSession, verify_csrf
from spdo.web.templating import templates

router = APIRouter(prefix="/notifications", dependencies=[Depends(verify_csrf)])


@router.get("", response_class=HTMLResponse)
def notifications_page(request: Request, session: DbSession, user: CurrentUser) -> Response:
    """Показывает последние уведомления пользователя.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Страница уведомлений.
    """
    return templates.TemplateResponse(
        request,
        "notifications.html",
        {"user": user, "items": notifications.recent(session, user.id)},
    )


@router.post("/read")
def mark_read(session: DbSession, user: CurrentUser) -> Response:
    """Отмечает все уведомления пользователя прочитанными.

    Args:
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Перенаправление на страницу уведомлений.
    """
    notifications.mark_all_read(session, user.id)
    session.commit()
    return RedirectResponse("/notifications", status.HTTP_303_SEE_OTHER)
