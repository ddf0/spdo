"""Маршруты страниц веб-интерфейса."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from spdo.users.deps import CurrentUser
from spdo.web.templating import templates

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
