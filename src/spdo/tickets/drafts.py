"""Черновики обращений (Ф2).

Черновик сохраняется по мере ввода, поэтому текст не теряется при ошибке
поиска похожих обращений или обрыве соединения. Поля черновика не
проверяются на заполненность: это делает регистрация.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from spdo.tickets.access import Actor, TicketNotFoundError, TicketValidationError
from spdo.tickets.models import DESCRIPTION_MAX, TITLE_MAX, Category, Component, TicketDraft


def _get_own(session: Session, actor: Actor, draft_id: int) -> TicketDraft:
    """Возвращает черновик пользователя.

    Raises:
        TicketNotFoundError: Черновик не существует или чужой.
    """
    draft = session.get(TicketDraft, draft_id)
    if draft is None or draft.author_id != actor.id:
        raise TicketNotFoundError(f"Черновик {draft_id} не найден")
    return draft


def save_draft(
    session: Session,
    actor: Actor,
    *,
    draft_id: int | None = None,
    title: str = "",
    description: str = "",
    category_id: int | None = None,
    component_id: int | None = None,
) -> TicketDraft:
    """Создаёт или обновляет черновик пользователя.

    Args:
        session: Сессия БД.
        actor: Автор черновика.
        draft_id: Обновляемый черновик или ``None`` — создать новый.
        title: Заголовок в текущем виде.
        description: Описание в текущем виде.
        category_id: Выбранная категория или ``None``.
        component_id: Выбранный компонент или ``None``.

    Returns:
        Сохранённый черновик.

    Raises:
        TicketNotFoundError: Черновик не существует или чужой.
        TicketValidationError: Текст длиннее допустимого или значение
            справочника не найдено.
    """
    if len(title) > TITLE_MAX or len(description) > DESCRIPTION_MAX:
        raise TicketValidationError("Текст черновика длиннее допустимого")
    if category_id is not None and session.get(Category, category_id) is None:
        raise TicketValidationError("Категория не найдена в справочнике")
    if component_id is not None and session.get(Component, component_id) is None:
        raise TicketValidationError("Компонент не найден в справочнике")
    if draft_id is None:
        draft = TicketDraft(author_id=actor.id)
        session.add(draft)
    else:
        draft = _get_own(session, actor, draft_id)
    draft.title, draft.description = title, description
    draft.category_id, draft.component_id = category_id, component_id
    session.flush()
    return draft


def get_draft(session: Session, actor: Actor, draft_id: int) -> TicketDraft:
    """Возвращает черновик пользователя.

    Args:
        session: Сессия БД.
        actor: Автор черновика.
        draft_id: Идентификатор черновика.

    Returns:
        Черновик.

    Raises:
        TicketNotFoundError: Черновик не существует или чужой.
    """
    return _get_own(session, actor, draft_id)


def list_drafts(session: Session, actor: Actor) -> list[TicketDraft]:
    """Возвращает черновики пользователя, начиная с последнего изменённого.

    Args:
        session: Сессия БД.
        actor: Автор черновиков.

    Returns:
        Черновики пользователя.
    """
    stmt = (
        select(TicketDraft)
        .where(TicketDraft.author_id == actor.id)
        .order_by(TicketDraft.updated_at.desc(), TicketDraft.id.desc())
    )
    return list(session.scalars(stmt))


def delete_draft(session: Session, actor: Actor, draft_id: int) -> None:
    """Удаляет черновик пользователя.

    Args:
        session: Сессия БД.
        actor: Автор черновика.
        draft_id: Идентификатор черновика.

    Raises:
        TicketNotFoundError: Черновик не существует или чужой.
    """
    session.delete(_get_own(session, actor, draft_id))
    session.flush()
