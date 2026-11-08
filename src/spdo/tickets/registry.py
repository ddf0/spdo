"""Реестр обращений: совместные фильтры и полнотекстовый поиск (Ф11).

Пользователь видит в реестре только свои обращения, оператор и
администратор — все. Полнотекстовый поиск выполняется средствами
PostgreSQL по заголовку и описанию с русской морфологией (конфигурация
``russian``); запрос пользователя разбирается функцией
``websearch_to_tsquery``, поэтому допускаются кавычки для фраз и минус для
исключения слов. Выражение документа совпадает с выражением GIN-индекса
``ix_tickets_fts``, иначе индекс не используется.
"""

from dataclasses import dataclass

from sqlalchemy import ColumnElement, Select, func, literal_column, select
from sqlalchemy.orm import Session, selectinload

from spdo.tickets.access import Actor, TicketValidationError, is_staff
from spdo.tickets.models import Ticket, TicketStatus

#: Число обращений на странице реестра.
PAGE_SIZE = 25
#: Наибольшая длина строки полнотекстового поиска.
QUERY_MAX = 200
#: Конфигурация текстового поиска PostgreSQL.
_CONFIG = literal_column("'russian'::regconfig")


def fts_document() -> ColumnElement:
    """Документ полнотекстового поиска — то же выражение, что в индексе.

    Returns:
        Выражение ``to_tsvector('russian', title || ' ' || description)``.
    """
    text = Ticket.title.op("||")(literal_column("' '")).op("||")(Ticket.description)
    return func.to_tsvector(_CONFIG, text)


@dataclass(frozen=True, slots=True)
class RegistryFilter:
    """Условия отбора реестра; ``None`` и пустая строка — без условия.

    Attributes:
        status: Статус.
        category_id: Категория.
        component_id: Компонент.
        query: Строка полнотекстового поиска.
    """

    status: TicketStatus | None = None
    category_id: int | None = None
    component_id: int | None = None
    query: str = ""

    def as_params(self) -> dict[str, str]:
        """Заданные условия в виде параметров запроса (для ссылок на страницы).

        Returns:
            Непустые параметры ``q``, ``status``, ``category_id``, ``component_id``.
        """
        params = {
            "q": self.query.strip(),
            "status": self.status.value if self.status else "",
            "category_id": str(self.category_id or ""),
            "component_id": str(self.component_id or ""),
        }
        return {k: v for k, v in params.items() if v}


@dataclass(frozen=True, slots=True)
class RegistryPage:
    """Страница реестра.

    Attributes:
        items: Обращения страницы.
        total: Число обращений, удовлетворяющих условиям.
        page: Номер страницы, начиная с 1.
        pages: Число страниц (не меньше 1).
    """

    items: list[Ticket]
    total: int
    page: int
    pages: int


def _filtered(actor: Actor, flt: RegistryFilter) -> Select[tuple[Ticket]]:
    """Запрос обращений с видимостью и совместными условиями фильтра."""
    stmt = select(Ticket)
    if not is_staff(actor):
        stmt = stmt.where(Ticket.author_id == actor.id)
    if flt.status is not None:
        stmt = stmt.where(Ticket.status == flt.status)
    if flt.category_id is not None:
        stmt = stmt.where(Ticket.category_id == flt.category_id)
    if flt.component_id is not None:
        stmt = stmt.where(Ticket.component_id == flt.component_id)
    return stmt


def search_registry(
    session: Session, actor: Actor, flt: RegistryFilter, page: int = 1
) -> RegistryPage:
    """Возвращает страницу реестра (Ф11).

    Без строки поиска обращения упорядочены от новых к старым, со строкой —
    по релевантности (``ts_rank``), при равенстве — от новых к старым.

    Args:
        session: Сессия БД.
        actor: Пользователь.
        flt: Условия отбора.
        page: Номер страницы, начиная с 1; больше последней — последняя.

    Returns:
        Страница реестра.

    Raises:
        TicketValidationError: Строка поиска длиннее :data:`QUERY_MAX`.
    """
    query = flt.query.replace("\x00", "").strip()  # PostgreSQL не допускает NUL в тексте
    if len(query) > QUERY_MAX:
        raise TicketValidationError(f"Строка поиска длиннее {QUERY_MAX} символов")
    stmt = _filtered(actor, flt)
    order: list[ColumnElement] = [Ticket.id.desc()]
    if query:
        tsquery = func.websearch_to_tsquery(_CONFIG, query)
        stmt = stmt.where(fts_document().op("@@")(tsquery))
        order.insert(0, func.ts_rank(fts_document(), tsquery).desc())
    total = session.scalar(stmt.with_only_columns(func.count(), maintain_column_froms=True)) or 0
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(page, 1), pages)
    items = session.scalars(
        stmt.options(selectinload(Ticket.category), selectinload(Ticket.component))
        .order_by(*order)
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )
    return RegistryPage(list(items), total, page, pages)
