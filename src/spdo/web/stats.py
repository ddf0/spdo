"""Страница статистики качества рекомендаций (Ф14, ПЦ9)."""

from datetime import datetime

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, Response

from spdo.analytics import stats
from spdo.tickets.access import TicketValidationError, is_staff
from spdo.users.deps import CurrentUser, DbSession
from spdo.web.templating import DISPLAY_TZ, templates

router = APIRouter()


@router.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, session: DbSession, user: CurrentUser) -> Response:
    """Показывает отчёт о качестве рекомендаций за период.

    Параметры запроса ``from`` и ``to`` — первый и последний день периода
    (``ГГГГ-ММ-ДД``); по умолчанию — последние 30 дней.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Страница отчёта; 403 — пользователю без роли оператора или
        администратора; 400 — при неверном периоде (с отчётом за период по
        умолчанию).
    """
    if not is_staff(user):
        return templates.TemplateResponse(
            request,
            "forbidden.html",
            {"user": user, "message": "Статистика доступна оператору и администратору."},
            status_code=status.HTTP_403_FORBIDDEN,
        )
    today = datetime.now(DISPLAY_TZ).date()
    error, code = None, status.HTTP_200_OK
    try:
        period = stats.Period.parse(
            request.query_params.get("from"), request.query_params.get("to"), today
        )
    except TicketValidationError as exc:
        error, code = str(exc), status.HTTP_400_BAD_REQUEST
        period = stats.Period.parse(None, None, today)
    report = stats.build_report(session, user, period)
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "user": user,
            "report": report,
            "error": error,
            "recall_target": stats.RECALL_TARGET,
            "top": stats.rec_stats.TOP,
        },
        status_code=code,
    )
