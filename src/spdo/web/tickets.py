"""Страницы реестра и карточки обращения с действиями (Ф11–Ф13, Ф7, Ф8).

Действия выполняются HTML-формами без JavaScript по схеме
«POST → перенаправление → GET»: после успеха карточка открывается заново с
сообщением, при ошибке показывается с текстом ошибки и кодом ответа.
Права проверяют сервисные функции; страница лишь не показывает
недоступные действия.
"""

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from spdo.notifications import service as notifications
from spdo.relations import service as relations
from spdo.relations.models import Link, RelationKind
from spdo.relations.service import RelationError
from spdo.tickets import dictionaries, registry
from spdo.tickets import service as tickets
from spdo.tickets.access import (
    PermissionDeniedError,
    TicketNotFoundError,
    TicketValidationError,
    is_staff,
)
from spdo.tickets.models import ChangeKind, HistoryEntry, Ticket, TicketStatus
from spdo.tickets.transitions import TRANSITIONS, TransitionError, Trigger, allowed_targets
from spdo.users import service as users
from spdo.users.deps import CurrentUser, DbSession, verify_csrf
from spdo.web.templating import STATUS_LABELS, templates

logger = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(verify_csrf)])

#: Названия видов изменения в истории (Ф13).
CHANGE_LABELS = {
    ChangeKind.CREATED: "Регистрация",
    ChangeKind.STATUS: "Статус",
    ChangeKind.CONFIDENTIALITY: "Конфиденциальность",
    ChangeKind.CATEGORY: "Категория",
    ChangeKind.COMPONENT: "Компонент",
    ChangeKind.SOLUTION: "Решение",
    ChangeKind.LINK: "Связь",
}
#: Названия видов связи.
RELATION_LABELS = {RelationKind.DUPLICATE: "дубликат", RelationKind.RELATED: "связанное"}
#: Сообщения после успешного действия (параметр ``done``).
DONE_MESSAGES = {
    "status": "Статус изменён.",
    "solution": "Решение опубликовано, обращение переведено в «Решено».",
    "confidential": "Признак конфиденциальности изменён.",
    "link": "Связь установлена.",
    "confirm": "Решение подтверждено, обращение закрыто.",
}


def _int_or_none(value: str | None) -> int | None:
    """Число из параметра запроса; пустое или нечисловое значение — ``None``."""
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _status_or_none(value: str | None) -> TicketStatus | None:
    """Статус из параметра запроса; неизвестное значение — ``None``."""
    try:
        return TicketStatus(value) if value else None
    except ValueError:
        return None


def _parse_filter(params: Mapping[str, str]) -> registry.RegistryFilter:
    """Условия реестра из параметров запроса; неверные значения отбрасываются."""
    return registry.RegistryFilter(
        status=_status_or_none(params.get("status")),
        category_id=_int_or_none(params.get("category_id")),
        component_id=_int_or_none(params.get("component_id")),
        query=params.get("q", ""),
    )


@router.get("/tickets", response_class=HTMLResponse)
def registry_page(request: Request, session: DbSession, user: CurrentUser) -> Response:
    """Показывает реестр обращений с фильтрами и поиском (Ф11).

    Параметры запроса: ``status``, ``category_id``, ``component_id``,
    ``q`` (полнотекстовый поиск), ``page``. Пустые и неверные значения
    игнорируются.

    Args:
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Страница реестра; при слишком длинной строке поиска — с ошибкой.
    """
    params = request.query_params
    flt = _parse_filter(params)
    error, page = None, None
    try:
        page = registry.search_registry(session, user, flt, _int_or_none(params.get("page")) or 1)
    except TicketValidationError as exc:
        error = str(exc)
    base = flt.as_params()
    return templates.TemplateResponse(
        request,
        "registry.html",
        {
            "user": user,
            "flt": flt,
            "page": page,
            "error": error,
            "categories": dictionaries.list_categories(session),
            "components": dictionaries.list_components(session),
            "status_labels": STATUS_LABELS,
            "authors": users.display_names(session, {t.author_id for t in page.items})
            if page
            else {},
            "page_url": lambda n: "/tickets?" + urlencode({**base, "page": n}),
        },
        status_code=status.HTTP_400_BAD_REQUEST if error else status.HTTP_200_OK,
    )


@dataclass(frozen=True, slots=True)
class LinkView:
    """Связь обращения для показа в карточке.

    Attributes:
        text: Описание связи относительно текущего обращения.
        other_id: Номер второго обращения.
        other_title: Заголовок второго обращения, если он доступен.
    """

    text: str
    other_id: int
    other_title: str | None


def _other_end(link: Link, ticket_id: int) -> int:
    """Второе обращение связи относительно ``ticket_id``."""
    return link.target_id if link.source_id == ticket_id else link.source_id


def _hidden_ids(session: Session, user: CurrentUser, ticket_id: int, links: list[Link]) -> set[int]:
    """Связанные обращения, номера которых пользователю показывать нельзя.

    Пользователь видит в реестре только свои обращения, а конфиденциальные
    чужие не должны раскрываться даже номером. Связь показывается, если на
    второе обращение пользователь может сослаться (своё или
    неконфиденциальное); оператор видит все.
    """
    if is_staff(user):
        return set()
    hidden = set()
    for link in links:
        other = _other_end(link, ticket_id)
        try:
            tickets.get_link_target(session, user, other)
        except TicketNotFoundError:
            hidden.add(other)
    return hidden


def _link_view(session: Session, user: CurrentUser, ticket_id: int, link: Link) -> LinkView:
    """Описание связи со стороны обращения ``ticket_id``."""
    outgoing = link.source_id == ticket_id
    other = _other_end(link, ticket_id)
    if link.kind is RelationKind.RELATED:
        text = "связано с обращением"
    else:
        text = "дубликат обращения" if outgoing else "основное для дубликата"
    try:
        title = tickets.get_ticket(session, user, other).title
    except TicketNotFoundError:
        title = None  # чужое обращение: только номер
    return LinkView(text, other, title)


def _status_label(value: str) -> str:
    """Название статуса по значению; неизвестное значение — как есть."""
    try:
        return STATUS_LABELS[TicketStatus(value)]
    except ValueError:
        return value


def _yes_no(value: str) -> str:
    """«да» / «нет» для логического значения из истории."""
    return "да" if value == "true" else "нет"


def _link_label(value: str) -> str:
    """Связь из истории (``вид:номер``) в читаемом виде."""
    kind, _, other = value.partition(":")
    try:
        return f"{RELATION_LABELS[RelationKind(kind)]} № {other}"
    except ValueError:
        return value


#: Преобразование значений «было» / «стало» по виду изменения.
_VALUE_FORMAT: dict[ChangeKind, Callable[[str], str]] = {
    ChangeKind.CREATED: _status_label,  # при регистрации записывается статус «Новое»
    ChangeKind.STATUS: _status_label,
    ChangeKind.CONFIDENTIALITY: _yes_no,
    ChangeKind.LINK: _link_label,
}


def _history_row(entry: HistoryEntry) -> tuple[str, str, str]:
    """Вид изменения, «было» и «стало» в читаемом виде."""
    fmt = _VALUE_FORMAT.get(entry.kind, str)
    old, new = (fmt(v) if v else "" for v in (entry.old_value, entry.new_value))
    return CHANGE_LABELS.get(entry.kind, entry.kind.value), old, new


#: Замена скрытого номера обращения в комментариях истории.
HIDDEN = "[скрыто]"


def _mask(text: str, hidden: set[int]) -> str:
    """Заменяет в тексте скрытые номера обращений."""
    if not hidden:
        return text
    return re.sub(r"\d+", lambda m: HIDDEN if int(m.group()) in hidden else m.group(), text)


def _history_rows(
    history: list[HistoryEntry], hidden: set[int], staff: bool, names: dict[int, str]
) -> list[tuple]:
    """Строки истории для показа: без связей со скрытыми обращениями.

    Пользователю не показываются служебные комментарии к связям (в них
    может упоминаться основное обращение группы) и номера скрытых обращений.
    """
    rows = []
    for e in history:
        if e.kind is ChangeKind.LINK and not staff:
            other = _int_or_none((e.new_value or "").partition(":")[2])
            if other in hidden:
                continue
            comment = ""
        else:
            comment = _mask(e.comment, hidden)
        rows.append((e, *_history_row(e), comment, names.get(e.author_id, "")))
    return rows


def _link_kinds(ticket: Ticket) -> list[tuple[str, str]]:
    """Виды связи, которые можно установить от обращения."""
    kinds = [("related", "связанное")]
    if allowed_targets(ticket.status, Trigger.DUPLICATE_LINK):
        kinds.insert(0, ("duplicate", "это обращение — дубликат указанного"))
    return kinds


def _main_solution(session: Session, user: CurrentUser, ticket: Ticket) -> str | None:
    """Решение основного обращения для дубликата (Ф18), если основное доступно.

    Автор дубликата видит решение основного обращения, на которое ему можно
    ссылаться (своё или неконфиденциальное); конфиденциальное чужое не
    раскрывается.
    """
    if ticket.status is not TicketStatus.CLOSED_DUPLICATE:
        return None
    main_id = relations.root_main(session, ticket.id)
    try:
        main = tickets.get_link_target(session, user, main_id)
    except TicketNotFoundError:
        return None
    return main.solution.text if main.solution else None


def _actions(user: CurrentUser, ticket: Ticket) -> dict[str, object]:
    """Действия, доступные пользователю над обращением."""
    staff = is_staff(user)
    targets = allowed_targets(ticket.status, Trigger.OPERATOR) if staff else []
    return {
        "statuses": [
            (s, STATUS_LABELS[s], TRANSITIONS[(ticket.status, s)].comment_required) for s in targets
        ],
        "solution": staff and ticket.status is TicketStatus.IN_PROGRESS,
        "confidential": staff,
        "link": _link_kinds(ticket) if staff else [],
        "confirm": ticket.author_id == user.id and ticket.status is TicketStatus.RESOLVED,
    }


def _render_card(
    request: Request,
    session: Session,
    user: CurrentUser,
    ticket_id: int,
    *,
    error: str | None = None,
    code: int = status.HTTP_200_OK,
) -> Response:
    """Карточка обращения или страница 404, если оно недоступно."""
    try:
        ticket = tickets.get_ticket(session, user, ticket_id)
    except TicketNotFoundError:
        return templates.TemplateResponse(
            request,
            "not_found.html",
            {"user": user, "message": f"Обращение № {ticket_id} не найдено или недоступно."},
            status_code=status.HTTP_404_NOT_FOUND,
        )
    history = tickets.get_history(session, user, ticket_id)
    links = relations.list_links(session, user, ticket_id)
    hidden = _hidden_ids(session, user, ticket_id, links)
    names = users.display_names(session, {ticket.author_id, *(e.author_id for e in history)})
    done = DONE_MESSAGES.get(request.query_params.get("done", ""))
    return templates.TemplateResponse(
        request,
        "ticket.html",
        {
            "user": user,
            "ticket": ticket,
            "status_labels": STATUS_LABELS,
            "author": names.get(ticket.author_id, ""),
            "history": _history_rows(history, hidden, is_staff(user), names),
            "links": [
                _link_view(session, user, ticket_id, link)
                for link in links
                if _other_end(link, ticket_id) not in hidden
            ],
            "actions": _actions(user, ticket),
            "main_solution": _main_solution(session, user, ticket),
            "error": error,
            "done": None if error else done,
        },
        status_code=code,
    )


@router.get("/tickets/{ticket_id:int}", response_class=HTMLResponse)
def ticket_page(
    ticket_id: int, request: Request, session: DbSession, user: CurrentUser
) -> Response:
    """Показывает карточку обращения: данные, решение, связи, историю (Ф13).

    Своё обращение видит автор, любое — оператор и администратор.

    Args:
        ticket_id: Номер обращения.
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Карточка или страница 404, если обращение не существует или чужое.
    """
    return _render_card(request, session, user, ticket_id)


#: Ошибки действий и коды ответа.
_ERROR_CODES: list[tuple[type[Exception], int]] = [
    (PermissionDeniedError, status.HTTP_403_FORBIDDEN),
    (TicketNotFoundError, status.HTTP_404_NOT_FOUND),
    (TicketValidationError, status.HTTP_400_BAD_REQUEST),
    (TransitionError, status.HTTP_409_CONFLICT),
    (RelationError, status.HTTP_409_CONFLICT),
]


class ActionUnavailableError(Exception):
    """Действие не выполнено из-за временной недоступности (например, индекса)."""


_ERROR_CODES.append((ActionUnavailableError, status.HTTP_503_SERVICE_UNAVAILABLE))


def _act(
    request: Request,
    session: Session,
    user: CurrentUser,
    ticket_id: int,
    done: str,
    action: Callable[[], object],
) -> Response:
    """Выполняет действие: при успехе — фиксация и перенаправление на карточку.

    При ошибке карточка показывается в ответе на POST с текстом ошибки:
    введённые данные теряются, но повторная отправка формы (обновление
    страницы) безопасна — действие снова будет проверено сервисом.
    """
    try:
        action()
    except tuple(e for e, _ in _ERROR_CODES) as exc:
        session.rollback()
        code = next(c for e, c in _ERROR_CODES if isinstance(exc, e))
        return _render_card(request, session, user, ticket_id, error=str(exc), code=code)
    session.commit()
    return RedirectResponse(f"/tickets/{ticket_id}?done={done}", status.HTTP_303_SEE_OTHER)


@router.post("/tickets/{ticket_id:int}/status")
def change_status(
    ticket_id: int,
    request: Request,
    session: DbSession,
    user: CurrentUser,
    target: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
) -> Response:
    """Меняет статус обращения действием оператора (Ф12).

    Args:
        ticket_id: Номер обращения.
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.
        target: Новый статус.
        comment: Комментарий (обязателен для закрытия и повторного открытия).

    Returns:
        Перенаправление на карточку или карточка с ошибкой.
    """

    def action() -> None:
        """Смена статуса."""
        new = _status_or_none(target)
        if new is None:
            raise TicketValidationError("Неизвестный статус")
        tickets.change_status(session, user, ticket_id, new, comment)

    return _act(request, session, user, ticket_id, "status", action)


@router.post("/tickets/{ticket_id:int}/solution")
def publish_solution(
    ticket_id: int,
    request: Request,
    session: DbSession,
    user: CurrentUser,
    text: Annotated[str, Form()] = "",
) -> Response:
    """Публикует решение и переводит обращение в «Решено» (Ф12).

    Args:
        ticket_id: Номер обращения.
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.
        text: Текст решения.

    Returns:
        Перенаправление на карточку или карточка с ошибкой.
    """
    return _act(
        request,
        session,
        user,
        ticket_id,
        "solution",
        lambda: (
            tickets.publish_solution(session, user, ticket_id, text),
            notifications.solution_published(session, user, ticket_id),  # Ф18
        ),
    )


@router.post("/tickets/{ticket_id:int}/confidential")
def set_confidential(
    ticket_id: int,
    request: Request,
    session: DbSession,
    user: CurrentUser,
    value: Annotated[str, Form()] = "",
    comment: Annotated[str, Form()] = "",
) -> Response:
    """Устанавливает или снимает признак конфиденциальности (Ф15).

    Args:
        ticket_id: Номер обращения.
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.
        value: Новый признак: ``true`` или ``false``.
        comment: Причина изменения.

    Returns:
        Перенаправление на карточку или карточка с ошибкой; 503, если не
        удалось обновить поисковый индекс (изменение тогда не сохраняется).
    """

    def action() -> None:
        """Смена признака; сбой индексации отменяет её (см. сервис)."""
        if value not in {"true", "false"}:
            raise TicketValidationError("Не указано значение признака")
        try:
            tickets.set_confidential(session, user, ticket_id, value == "true", comment)
        except tuple(e for e, _ in _ERROR_CODES):
            raise
        except Exception as exc:  # noqa: BLE001 — сервис сообщает о сбое индекса так
            logger.exception("Не удалось обновить поисковый индекс обращения %s", ticket_id)
            raise ActionUnavailableError(
                "Поисковый индекс недоступен, признак не изменён. Повторите позже."
            ) from exc

    return _act(request, session, user, ticket_id, "confidential", action)


@router.post("/tickets/{ticket_id:int}/links")
def create_link(
    ticket_id: int,
    request: Request,
    session: DbSession,
    user: CurrentUser,
    target_id: Annotated[str, Form()] = "",
    kind: Annotated[str, Form()] = "",
) -> Response:
    """Связывает обращение с другим: «дубликат» (Ф7) или «связанное» (Ф8).

    Обращение карточки — источник связи; для «дубликата» оно закрывается
    как дубликат указанного.

    Args:
        ticket_id: Номер обращения карточки.
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.
        target_id: Номер второго обращения.
        kind: Вид связи: ``duplicate`` или ``related``.

    Returns:
        Перенаправление на карточку или карточка с ошибкой.
    """

    def action() -> None:
        """Установление связи оператором."""
        tickets.get_ticket(session, user, ticket_id)  # своё обращение — до прав
        other = _int_or_none(target_id.strip())
        if other is None or kind not in {k.value for k in RelationKind}:
            raise TicketValidationError("Укажите номер обращения и вид связи")
        if not is_staff(user):
            raise PermissionDeniedError("Связи вне регистрации устанавливает оператор")
        link = relations.create_link(
            session, user, source_id=ticket_id, target_id=other, kind=RelationKind(kind)
        )
        if link.kind is RelationKind.DUPLICATE:
            notifications.linked_to_resolved(session, user, ticket_id, link.target_id)

    return _act(request, session, user, ticket_id, "link", action)


@router.post("/tickets/{ticket_id:int}/confirm")
def confirm_resolution(
    ticket_id: int, request: Request, session: DbSession, user: CurrentUser
) -> Response:
    """Подтверждает решение автором и закрывает обращение.

    Args:
        ticket_id: Номер обращения.
        request: Текущий запрос.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Перенаправление на карточку или карточка с ошибкой.
    """
    return _act(
        request,
        session,
        user,
        ticket_id,
        "confirm",
        lambda: tickets.confirm_resolution(session, user, ticket_id),
    )
