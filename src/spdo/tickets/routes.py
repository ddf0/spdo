"""Маршруты API черновиков обращений (Ф2)."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from spdo.tickets import drafts
from spdo.tickets.access import TicketNotFoundError, TicketValidationError
from spdo.tickets.models import DESCRIPTION_MAX, TITLE_MAX, TicketDraft
from spdo.users.deps import CurrentUser, DbSession, verify_csrf

router = APIRouter(prefix="/api/drafts", dependencies=[Depends(verify_csrf)])


class DraftIn(BaseModel):
    """Текущее содержимое формы регистрации.

    Attributes:
        title: Заголовок в любом состоянии.
        description: Описание в любом состоянии.
        category_id: Выбранная категория или ``None``.
        component_id: Выбранный компонент или ``None``.
    """

    title: str = Field(default="", max_length=TITLE_MAX)
    description: str = Field(default="", max_length=DESCRIPTION_MAX)
    category_id: int | None = None
    component_id: int | None = None


class DraftOut(DraftIn):
    """Сохранённый черновик.

    Attributes:
        id: Номер черновика.
        updated_at: Дата последнего сохранения.
    """

    id: int
    updated_at: datetime


def _out(draft: TicketDraft) -> DraftOut:
    """Черновик в виде ответа API."""
    return DraftOut(
        id=draft.id,
        title=draft.title,
        description=draft.description,
        category_id=draft.category_id,
        component_id=draft.component_id,
        updated_at=draft.updated_at,
    )


def _save(session: DbSession, user: CurrentUser, draft_id: int | None, body: DraftIn) -> DraftOut:
    """Сохраняет черновик и переводит ошибки предметной области в HTTP."""
    try:
        draft = drafts.save_draft(session, user, draft_id=draft_id, **body.model_dump())
    except TicketNotFoundError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except TicketValidationError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    session.commit()
    session.refresh(draft)
    return _out(draft)


@router.post("", status_code=status.HTTP_201_CREATED)
def create(body: DraftIn, session: DbSession, user: CurrentUser) -> DraftOut:
    """Создаёт черновик.

    Args:
        body: Содержимое формы.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Сохранённый черновик.

    Raises:
        HTTPException: 400 — неизвестное значение справочника; 422 — текст
            длиннее допустимого (проверка схемы запроса).
    """
    return _save(session, user, None, body)


@router.put("/{draft_id}")
def update(draft_id: int, body: DraftIn, session: DbSession, user: CurrentUser) -> DraftOut:
    """Обновляет свой черновик.

    Args:
        draft_id: Номер черновика.
        body: Содержимое формы.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Сохранённый черновик.

    Raises:
        HTTPException: 400 — неизвестное значение справочника, 404 —
            черновик не существует или чужой, 422 — текст длиннее
            допустимого.
    """
    return _save(session, user, draft_id, body)


@router.delete("/{draft_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete(draft_id: int, session: DbSession, user: CurrentUser) -> Response:
    """Удаляет свой черновик.

    Args:
        draft_id: Номер черновика.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Пустой ответ 204.

    Raises:
        HTTPException: 404 — черновик не существует или чужой.
    """
    try:
        drafts.delete_draft(session, user, draft_id)
    except TicketNotFoundError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
