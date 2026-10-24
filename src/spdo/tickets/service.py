"""Сервисные функции обращений: регистрация, статусы, решения, история.

Каждое изменение статуса или поля записывается в историю обращения
(Ф13), а смена статуса — ещё и в журнал действий. Функции не фиксируют
транзакцию: изменение, история и журнал попадают в БД одним коммитом
вызывающего кода.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.sql import Subquery

from spdo.analytics.journal import record_action
from spdo.analytics.models import ActionKind
from spdo.search.base import IndexedTicket
from spdo.search.factory import get_searcher
from spdo.tickets.access import (
    Actor,
    PermissionDeniedError,
    TicketNotFoundError,
    TicketValidationError,
    can_reference,
    can_view,
    require_admin,
    require_staff,
)
from spdo.tickets.models import (
    DESCRIPTION_MAX,
    TITLE_MAX,
    Category,
    ChangeKind,
    Component,
    HistoryEntry,
    Solution,
    SolutionRating,
    Ticket,
    TicketDraft,
    TicketStatus,
)
from spdo.tickets.transitions import Trigger, check_transition

logger = logging.getLogger(__name__)

#: Режим блокировки строки обращения при изменении: ``FOR NO KEY UPDATE``.
ROW_LOCK = {"key_share": True}


def _clean_text(value: str, field: str, limit: int) -> str:
    """Обрезает пробелы по краям и проверяет длину текста.

    Args:
        value: Введённый текст.
        field: Название поля для сообщения об ошибке.
        limit: Наибольшая длина в символах.

    Returns:
        Текст без пробелов по краям.

    Raises:
        TicketValidationError: Текст пуст или длиннее ``limit``.
    """
    text = value.strip()
    if not text:
        raise TicketValidationError(f"Поле «{field}» не заполнено")
    if len(text) > limit:
        raise TicketValidationError(f"Поле «{field}» длиннее {limit} символов")
    return text


def _add_history(
    session: Session,
    ticket: Ticket,
    actor: Actor,
    kind: ChangeKind,
    old: str | None,
    new: str | None,
    comment: str = "",
) -> None:
    """Добавляет запись в историю изменений обращения."""
    session.add(
        HistoryEntry(
            ticket_id=ticket.id,
            kind=kind,
            old_value=old,
            new_value=new,
            comment=comment.strip(),
            author_id=actor.id,
        )
    )


def check_dictionaries(session: Session, category_id: int | None, component_id: int | None) -> None:
    """Проверяет, что указанные значения справочников существуют.

    Args:
        session: Сессия БД.
        category_id: Категория или ``None`` — не проверяется.
        component_id: Компонент или ``None`` — не проверяется.

    Raises:
        TicketValidationError: Значение не найдено в справочнике.
    """
    if category_id is not None and session.get(Category, category_id) is None:
        raise TicketValidationError("Категория не найдена в справочнике")
    if component_id is not None and session.get(Component, component_id) is None:
        raise TicketValidationError("Компонент не найден в справочнике")


def create_ticket(
    session: Session,
    actor: Actor,
    *,
    title: str,
    description: str,
    category_id: int,
    component_id: int,
    draft_id: int | None = None,
) -> Ticket:
    """Регистрирует обращение со статусом «Новое» (Ф1).

    Если указан черновик, он удаляется после регистрации.

    Args:
        session: Сессия БД.
        actor: Автор обращения.
        title: Заголовок, до 200 символов.
        description: Описание, до 4000 символов.
        category_id: Категория из справочника.
        component_id: Компонент из справочника.
        draft_id: Черновик, из которого создано обращение, или ``None``.

    Returns:
        Зарегистрированное обращение; его идентификатор — номер.

    Raises:
        TicketValidationError: Поля не заполнены, слишком длинные или
            ссылаются на несуществующие значения справочников.
    """
    ticket = Ticket(
        title=_clean_text(title, "Заголовок", TITLE_MAX),
        description=_clean_text(description, "Описание", DESCRIPTION_MAX),
        category_id=category_id,
        component_id=component_id,
        author_id=actor.id,
        status=TicketStatus.NEW,
    )
    check_dictionaries(session, category_id, component_id)
    session.add(ticket)
    session.flush()
    _add_history(session, ticket, actor, ChangeKind.CREATED, None, TicketStatus.NEW.value)
    _index_softly(session, ticket)
    if draft_id is not None:
        # Чужой или уже удалённый черновик не мешает регистрации.
        draft = session.get(TicketDraft, draft_id)
        if draft is not None and draft.author_id == actor.id:
            session.delete(draft)
    session.flush()
    return ticket


def _index(session: Session, ticket: Ticket) -> None:
    """Передаёт обращение в поисковый индекс в текущей транзакции.

    Raises:
        Exception: Поисковик недоступен или запись индекса не удалась.
    """
    get_searcher().index(
        session,
        IndexedTicket(
            ticket_id=ticket.id,
            title=ticket.title,
            description=ticket.description,
            category_id=ticket.category_id,
            component_id=ticket.component_id,
            author_id=ticket.author_id,
            confidential=ticket.confidential,
        ),
    )


def _index_softly(session: Session, ticket: Ticket) -> None:
    """Индексирует обращение; сбой поиска не отменяет регистрацию.

    ТЗ: недоступность поиска не блокирует регистрацию. Запись индекса
    выполняется в точке сохранения, чтобы её сбой не прерывал транзакцию;
    пропущенное обращение позже добавит ``spdo reindex``.
    """
    try:
        with session.begin_nested():
            _index(session, ticket)
    except Exception:  # noqa: BLE001 — любой сбой поиска не должен мешать регистрации
        logger.exception("Обращение %s не добавлено в поисковый индекс", ticket.id)


def reindex_all(session: Session, actor: Actor) -> int:
    """Перестраивает поисковый индекс по всем обращениям реестра.

    Нужна после смены модели или режима поиска и для обращений,
    зарегистрированных до появления индекса.

    Args:
        session: Сессия БД.
        actor: Администратор.

    Returns:
        Число проиндексированных обращений.

    Raises:
        PermissionDeniedError: Пользователь не администратор.
    """
    require_admin(actor)
    count = 0
    for ticket in session.scalars(select(Ticket).order_by(Ticket.id)):
        _index(session, ticket)
        count += 1
    session.flush()
    return count


def get_ticket(session: Session, actor: Actor, ticket_id: int, *, lock: bool = False) -> Ticket:
    """Возвращает обращение, доступное пользователю.

    Args:
        session: Сессия БД.
        actor: Пользователь.
        ticket_id: Номер обращения.
        lock: Заблокировать строку до конца транзакции и перечитать её
            (для изменений).

    Returns:
        Обращение.

    Raises:
        TicketNotFoundError: Обращение не существует или чужое.
    """
    # FOR NO KEY UPDATE не конфликтует с KEY SHARE, который берут внешние
    # ключи при вставке связей и истории, — встречные операции не блокируют
    # друг друга. populate_existing: после ожидания блокировки перечитать
    # строку, а не доверять объекту, загруженному до чужого изменения.
    for_update = ROW_LOCK if lock else None
    ticket = session.get(Ticket, ticket_id, with_for_update=for_update, populate_existing=lock)
    if ticket is None or not can_view(actor, ticket):
        raise TicketNotFoundError(f"Обращение {ticket_id} не найдено")
    return ticket


def get_link_target(session: Session, actor: Actor, ticket_id: int) -> Ticket:
    """Возвращает обращение, на которое пользователь может сослаться связью.

    Args:
        session: Сессия БД.
        actor: Пользователь, устанавливающий связь.
        ticket_id: Номер обращения — цели связи.

    Returns:
        Обращение.

    Raises:
        TicketNotFoundError: Обращение не существует или чужое
            конфиденциальное.
    """
    ticket = session.get(Ticket, ticket_id)
    if ticket is None or not can_reference(actor, ticket):
        raise TicketNotFoundError(f"Обращение {ticket_id} не найдено")
    return ticket


def record_link(
    session: Session, actor: Actor, ticket_id: int, other_id: int, kind: str, comment: str = ""
) -> None:
    """Записывает в историю обращения установление связи с другим обращением.

    Вызывается модулем ``relations`` в транзакции создания связи для
    каждого из двух связанных обращений.

    Args:
        session: Сессия БД.
        actor: Пользователь, установивший связь.
        ticket_id: Обращение, в историю которого пишется запись.
        other_id: Второе обращение связи.
        kind: Вид связи — значение ``RelationKind`` модуля ``relations``.
        comment: Пояснение.

    Raises:
        TicketNotFoundError: Обращение не существует.
    """
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise TicketNotFoundError(f"Обращение {ticket_id} не найдено")
    _add_history(session, ticket, actor, ChangeKind.LINK, None, f"{kind}:{other_id}", comment)


def get_history(session: Session, actor: Actor, ticket_id: int) -> list[HistoryEntry]:
    """Возвращает историю изменений обращения в хронологическом порядке (Ф13).

    Порядок задаётся идентификатором записи, а не датой: дата равна
    началу транзакции, и записи параллельных транзакций по ней могут
    перепутаться, а идентификаторы выдаются под блокировкой обращения.

    Args:
        session: Сессия БД.
        actor: Пользователь.
        ticket_id: Номер обращения.

    Returns:
        Записи истории от ранних к поздним.

    Raises:
        TicketNotFoundError: Обращение не существует или чужое.
    """
    get_ticket(session, actor, ticket_id)
    stmt = select(HistoryEntry).where(HistoryEntry.ticket_id == ticket_id).order_by(HistoryEntry.id)
    return list(session.scalars(stmt))


def _apply_status(
    session: Session,
    ticket: Ticket,
    actor: Actor,
    target: TicketStatus,
    trigger: Trigger,
    comment: str = "",
) -> None:
    """Проверяет и применяет переход статуса, записывая историю и журнал."""
    check_transition(ticket.status, target, trigger, comment)
    old = ticket.status
    ticket.status = target
    _add_history(session, ticket, actor, ChangeKind.STATUS, old.value, target.value, comment)
    record_action(
        session,
        user_id=actor.id,
        action=ActionKind.STATUS_CHANGED,
        ticket_id=ticket.id,
        details=f"{old.value} -> {target.value}",
    )
    session.flush()


def change_status(
    session: Session, actor: Actor, ticket_id: int, target: TicketStatus, comment: str = ""
) -> Ticket:
    """Меняет статус обращения действием оператора (Ф12).

    Так выполняются: принятие в работу, закрытие без решения и повторное
    открытие. Переходы «Решено» и «Закрыто как дубликат» здесь запрещены:
    они выполняются публикацией решения и установкой связи.

    Args:
        session: Сессия БД.
        actor: Оператор или администратор.
        ticket_id: Номер обращения.
        target: Новый статус.
        comment: Комментарий; обязателен для закрытия без решения и
            повторного открытия.

    Returns:
        Изменённое обращение.

    Raises:
        PermissionDeniedError: Пользователь не оператор.
        TicketNotFoundError: Обращение не найдено.
        TransitionError: Переход запрещён таблицей.
    """
    require_staff(actor)
    ticket = get_ticket(session, actor, ticket_id, lock=True)
    _apply_status(session, ticket, actor, target, Trigger.OPERATOR, comment)
    return ticket


def publish_solution(session: Session, actor: Actor, ticket_id: int, text: str) -> Solution:
    """Публикует решение и переводит обращение в «Решено» (Ф12).

    Если решение уже было (обращение решали, затем открыли повторно),
    текст заменяется, а прежний сохраняется в истории.

    Args:
        session: Сессия БД.
        actor: Оператор или администратор.
        ticket_id: Номер обращения в статусе «В работе».
        text: Текст решения.

    Returns:
        Опубликованное решение.

    Raises:
        PermissionDeniedError: Пользователь не оператор.
        TicketNotFoundError: Обращение не найдено.
        TicketValidationError: Текст решения пуст.
        TransitionError: Обращение не в статусе «В работе».
    """
    require_staff(actor)
    ticket = get_ticket(session, actor, ticket_id, lock=True)
    body = text.strip()
    if not body:
        raise TicketValidationError("Текст решения не заполнен")
    check_transition(ticket.status, TicketStatus.RESOLVED, Trigger.SOLUTION)
    solution = ticket.solution
    old_text = solution.text if solution is not None else None
    if solution is None:
        # Присваивание через связь обновляет обе стороны в памяти сессии.
        solution = Solution(text=body, author_id=actor.id)
        ticket.solution = solution
    else:
        solution.text, solution.author_id, solution.created_at = body, actor.id, func.now()
    _add_history(session, ticket, actor, ChangeKind.SOLUTION, old_text, body)
    _apply_status(session, ticket, actor, TicketStatus.RESOLVED, Trigger.SOLUTION)
    session.refresh(solution)
    return solution


def confirm_resolution(session: Session, actor: Actor, ticket_id: int) -> Ticket:
    """Закрывает решённое обращение по подтверждению автора.

    Args:
        session: Сессия БД.
        actor: Автор обращения.
        ticket_id: Номер обращения в статусе «Решено».

    Returns:
        Закрытое обращение.

    Raises:
        TicketNotFoundError: Обращение не найдено.
        PermissionDeniedError: Подтверждает не автор.
        TransitionError: Обращение не в статусе «Решено».
    """
    if get_ticket(session, actor, ticket_id).author_id != actor.id:
        raise PermissionDeniedError("Решение подтверждает автор обращения")
    ticket = get_ticket(session, actor, ticket_id, lock=True)
    _apply_status(
        session,
        ticket,
        actor,
        TicketStatus.CLOSED,
        Trigger.AUTHOR_CONFIRMATION,
        "Подтверждено автором",
    )
    return ticket


def close_as_duplicate(
    session: Session, actor: Actor, ticket_id: int, main_ticket_id: int
) -> Ticket:
    """Переводит обращение в «Закрыто как дубликат».

    Вызывается только модулем ``relations`` в той же транзакции, в
    которой создаётся связь «дубликат». Существование основного
    обращения и отсутствие циклов проверяет он. Право — как на просмотр:
    по ТЗ дубликатом своё обращение помечает автор при регистрации, чужое —
    оператор.

    Args:
        session: Сессия БД.
        actor: Пользователь, установивший связь.
        ticket_id: Номер обращения-дубликата.
        main_ticket_id: Номер основного обращения.

    Returns:
        Закрытое обращение.

    Raises:
        TicketNotFoundError: Обращение не найдено или недоступно.
        TicketValidationError: Обращение указано дубликатом самого себя.
        TransitionError: Обращение уже решено или закрыто.
    """
    if ticket_id == main_ticket_id:
        raise TicketValidationError("Обращение не может быть дубликатом самого себя")
    ticket = get_ticket(session, actor, ticket_id, lock=True)
    _apply_status(
        session,
        ticket,
        actor,
        TicketStatus.CLOSED_DUPLICATE,
        Trigger.DUPLICATE_LINK,
        f"Дубликат обращения {main_ticket_id}",
    )
    return ticket


def set_confidential(
    session: Session, actor: Actor, ticket_id: int, value: bool, comment: str = ""
) -> Ticket:
    """Устанавливает или снимает признак конфиденциальности (Ф15).

    Args:
        session: Сессия БД.
        actor: Оператор или администратор.
        ticket_id: Номер обращения.
        value: ``True`` — не показывать в рекомендациях посторонним.
        comment: Причина изменения.

    Returns:
        Обращение.

    Raises:
        PermissionDeniedError: Пользователь не оператор.
        TicketNotFoundError: Обращение не найдено.
        Exception: Не удалось обновить поисковый индекс. В отличие от
            регистрации, здесь сбой индексации отменяет операцию: иначе
            скрытое обращение осталось бы видимым в рекомендациях.
    """
    require_staff(actor)
    ticket = get_ticket(session, actor, ticket_id, lock=True)
    if ticket.confidential != value:
        old = str(ticket.confidential).lower()
        ticket.confidential = value
        _add_history(
            session, ticket, actor, ChangeKind.CONFIDENTIALITY, old, str(value).lower(), comment
        )
        _index(session, ticket)
        session.flush()
    return ticket


def _resolved_at_subquery() -> Subquery:
    """Подзапрос: время последнего перехода каждого обращения в «Решено».

    Последний переход определяется по наибольшему идентификатору записи
    истории, а не по дате (см. :func:`get_history`).
    """
    last = (
        select(HistoryEntry.ticket_id, func.max(HistoryEntry.id).label("entry_id"))
        .where(
            HistoryEntry.kind == ChangeKind.STATUS,
            HistoryEntry.new_value == TicketStatus.RESOLVED.value,
        )
        .group_by(HistoryEntry.ticket_id)
        .subquery()
    )
    return (
        select(last.c.ticket_id, HistoryEntry.created_at.label("resolved_at"))
        .join(HistoryEntry, HistoryEntry.id == last.c.entry_id)
        .subquery()
    )


def _expired_stmt(cutoff: datetime) -> Select[tuple[Ticket]]:
    """Запрос решённых обращений, решённых раньше ``cutoff``."""
    resolved = _resolved_at_subquery()
    return (
        select(Ticket)
        .join(resolved, resolved.c.ticket_id == Ticket.id)
        .where(Ticket.status == TicketStatus.RESOLVED, resolved.c.resolved_at < cutoff)
        .order_by(Ticket.id)
    )


def find_expired(session: Session, days: int, now: datetime) -> list[Ticket]:
    """Находит решённые обращения, не подтверждённые автором в срок.

    Args:
        session: Сессия БД.
        days: Срок подтверждения в днях.
        now: Текущий момент.

    Returns:
        Обращения в статусе «Решено», решённые раньше ``now - days``.
    """
    return list(session.scalars(_expired_stmt(now - timedelta(days=days))))


def close_expired(session: Session, actor: Actor, days: int, now: datetime) -> list[int]:
    """Закрывает решённые обращения, срок подтверждения которых истёк.

    ТЗ исключает автоматическое закрытие без участия человека, поэтому
    операцию запускает администратор, и она записывается от его имени.

    Args:
        session: Сессия БД.
        actor: Администратор.
        days: Срок подтверждения в днях.
        now: Текущий момент.

    Returns:
        Номера закрытых обращений.

    Raises:
        PermissionDeniedError: Пользователь не администратор.
    """
    require_admin(actor)
    cutoff = now - timedelta(days=days)
    closed: list[int] = []
    for ticket in find_expired(session, days, now):
        session.refresh(ticket, with_for_update=ROW_LOCK)
        # Пока ждали блокировку, обращение могли открыть и решить заново.
        still_expired = session.scalar(_expired_stmt(cutoff).where(Ticket.id == ticket.id))
        if still_expired is None:
            continue
        comment = f"Срок подтверждения решения ({days} дн.) истёк"
        _apply_status(session, ticket, actor, TicketStatus.CLOSED, Trigger.EXPIRY, comment)
        closed.append(ticket.id)
    return closed


@dataclass(frozen=True, slots=True)
class TicketCard:
    """Карточка обращения для показа пользователю (Ф5).

    Чужое неконфиденциальное обращение показывается в сокращённом виде:
    заголовок, статус и опубликованное решение (ТЗ, раздел «Роли»).

    Attributes:
        ticket_id: Номер обращения.
        title: Заголовок.
        status: Статус.
        category: Наименование категории.
        component: Наименование компонента или ``None`` в сокращённом виде.
        full: Показана ли карточка полностью (своё обращение или оператор).
        description: Описание или ``None`` в сокращённом виде.
        solution_id: Номер опубликованного решения или ``None``.
        solution_text: Текст опубликованного решения или ``None``.
        my_rating: Оценка решения текущим пользователем или ``None``.
    """

    ticket_id: int
    title: str
    status: TicketStatus
    category: str
    component: str | None
    full: bool
    description: str | None
    solution_id: int | None
    solution_text: str | None
    my_rating: bool | None


def _visible_ticket(session: Session, actor: Actor, ticket_id: int, shown: bool) -> Ticket:
    """Обращение, которое пользователь может видеть хотя бы в сокращённом виде.

    Чужое обращение доступно, только если оно неконфиденциальное и было
    показано пользователю в рекомендациях (``shown``): по ТЗ чужие
    обращения видны только в рекомендациях.
    """
    ticket = get_link_target(session, actor, ticket_id)
    if not can_view(actor, ticket) and not shown:
        raise TicketNotFoundError(f"Обращение {ticket_id} не найдено")
    return ticket


def solution_ticket_id(session: Session, solution_id: int) -> int | None:
    """Возвращает номер обращения, которому принадлежит решение.

    Args:
        session: Сессия БД.
        solution_id: Номер решения.

    Returns:
        Номер обращения или ``None``, если решения нет.
    """
    solution = session.get(Solution, solution_id)
    return solution.ticket_id if solution else None


def get_card(session: Session, actor: Actor, ticket_id: int, *, shown: bool = False) -> TicketCard:
    """Возвращает карточку обращения с учётом прав пользователя (Ф5).

    Args:
        session: Сессия БД.
        actor: Пользователь.
        ticket_id: Номер обращения.
        shown: Обращение показывалось пользователю в рекомендациях —
            условие доступа к чужому обращению (проверяет модуль
            ``recommendations``).

    Returns:
        Карточка: полная для своего обращения и оператора, сокращённая —
        для чужого неконфиденциального показанного (без описания и
        компонента; категория нужна в выдаче по Ф4).

    Raises:
        TicketNotFoundError: Обращение недоступно пользователю.
    """
    ticket = _visible_ticket(session, actor, ticket_id, shown)
    full = can_view(actor, ticket)
    solution = ticket.solution
    rating = None
    if solution is not None:
        rating = session.scalar(
            select(SolutionRating.helped).where(
                SolutionRating.solution_id == solution.id, SolutionRating.user_id == actor.id
            )
        )
    return TicketCard(
        ticket_id=ticket.id,
        title=ticket.title,
        status=ticket.status,
        category=ticket.category.name,
        component=ticket.component.name if full else None,
        full=full,
        description=ticket.description if full else None,
        solution_id=solution.id if solution else None,
        solution_text=solution.text if solution else None,
        my_rating=rating,
    )


def rate_solution(
    session: Session, actor: Actor, solution_id: int, helped: bool, *, shown: bool = False
) -> None:
    """Сохраняет оценку решения «помогло / не помогло» (Ф6).

    Повторная оценка того же пользователя заменяет прежнюю.

    Args:
        session: Сессия БД.
        actor: Пользователь, которому доступна карточка обращения.
        solution_id: Номер решения.
        helped: ``True`` — помогло, ``False`` — не помогло.
        shown: Обращение решения показывалось пользователю в рекомендациях.

    Raises:
        TicketNotFoundError: Решение не существует или его обращение недоступно.
    """
    solution = session.get(Solution, solution_id)
    if solution is None:
        raise TicketNotFoundError(f"Решение {solution_id} не найдено")
    _visible_ticket(session, actor, solution.ticket_id, shown)
    stmt = insert(SolutionRating).values(solution_id=solution_id, user_id=actor.id, helped=helped)
    stmt = stmt.on_conflict_do_update(
        index_elements=[SolutionRating.solution_id, SolutionRating.user_id],
        set_={"helped": helped, "rated_at": func.clock_timestamp()},
    )
    session.execute(stmt)
