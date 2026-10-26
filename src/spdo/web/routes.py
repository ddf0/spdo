"""Маршруты страниц веб-интерфейса."""

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, Response

from spdo.config import settings
from spdo.recommendations.routes import AUTOMATIC_NOTICE
from spdo.tickets import dictionaries, drafts
from spdo.tickets import service as tickets
from spdo.tickets.access import TicketNotFoundError
from spdo.tickets.models import TicketDraft
from spdo.users.deps import CurrentUser, DbSession
from spdo.web.templating import KIND_LABELS, STATUS_LABELS, templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(request: Request, user: CurrentUser) -> Response:
    """Показывает главную страницу вошедшему пользователю.

    Args:
        request: Текущий запрос.
        user: Текущий пользователь.

    Returns:
        Главная страница.
    """
    return templates.TemplateResponse(request, "index.html", {"user": user})


def _draft_data(draft: TicketDraft | None) -> dict[str, object] | None:
    """Последний черновик пользователя для заполнения формы."""
    if draft is None:
        return None
    return {
        "id": draft.id,
        "title": draft.title,
        "description": draft.description,
        "category_id": draft.category_id,
        "component_id": draft.component_id,
        "updated_at": draft.updated_at.isoformat(),
    }


@router.get("/tickets/new", response_class=HTMLResponse)
def new_ticket(request: Request, session: DbSession, user: CurrentUser) -> Response:
    """Показывает страницу регистрации обращения с подбором похожих (ПЦ1).

    Форма заполняется последним черновиком пользователя (Ф2).

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Страница регистрации.
    """
    latest = next(iter(drafts.list_drafts(session, user)), None)
    page = {
        "user_id": user.id,
        "draft": _draft_data(latest),
        "statuses": {s.value: label for s, label in STATUS_LABELS.items()},
        "kinds": KIND_LABELS,
    }
    return templates.TemplateResponse(
        request,
        "new_ticket.html",
        {
            "user": user,
            "categories": dictionaries.list_categories(session),
            "components": dictionaries.list_components(session),
            "top_n": settings.top_n,
            "automatic_notice": AUTOMATIC_NOTICE,
            "page": page,
        },
    )


@router.get("/tickets/{ticket_id}", response_class=HTMLResponse)
def ticket_page(
    ticket_id: int, request: Request, session: DbSession, user: CurrentUser
) -> Response:
    """Показывает своё обращение (оператору — любое): номер, дата, статус, решение.

    Args:
        ticket_id: Номер обращения.
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Страница обращения или страница 404, если обращение не существует
        или чужое.
    """
    try:
        ticket = tickets.get_ticket(session, user, ticket_id)
    except TicketNotFoundError:
        return templates.TemplateResponse(
            request,
            "not_found.html",
            {"user": user, "message": f"Обращение № {ticket_id} не найдено или недоступно."},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return templates.TemplateResponse(
        request, "ticket.html", {"user": user, "ticket": ticket, "status_labels": STATUS_LABELS}
    )
