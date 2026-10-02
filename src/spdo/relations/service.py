"""Сервисные функции связей «дубликат» и «связанное» (Ф7, Ф8).

Связи «дубликат» образуют лес: у обращения не более одного основного
(ограничение схемы ``uq_links_one_main``). Цикл возникнет, только если
источник уже лежит на цепочке основных от цели, поэтому запрет прямых и
косвенных циклов сводится к обходу этой цепочки вверх.

Группы дубликатов одноуровневые: дубликат указывает прямо на основное
обращение. Для этого связь с дубликатом переадресуется на его основное, а
обращение, к которому уже отнесены дубликаты, само дубликатом не становится.

Блокировки берутся в одном порядке: сначала advisory-блокировка графа
дубликатов, затем строки обращений в режиме ``FOR NO KEY UPDATE``, который
не конфликтует с ``KEY SHARE`` внешних ключей при вставке связи и записей
истории. Расчёт на уровень изоляции READ COMMITTED: после ожидания
блокировки запросы видят зафиксированные чужие изменения.

Связь, смена статуса, записи истории обоих обращений и журнал действий
выполняются в одной транзакции; фиксирует её вызывающий код.
"""

from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from spdo.analytics.journal import record_action
from spdo.analytics.models import ActionKind
from spdo.relations.models import Link, RelationKind
from spdo.tickets import service as tickets
from spdo.tickets.access import Actor, TicketNotFoundError
from spdo.tickets.models import TicketStatus
from spdo.tickets.transitions import Trigger, check_transition

#: Ключ транзакционной advisory-блокировки изменений графа дубликатов.
#: Сериализует создание связей «дубликат»: блокировки отдельных строк
#: недостаточно, цикл A → B → C → A могут собрать три параллельных запроса.
DUPLICATE_GRAPH_LOCK = 0x5350_444F_0001
#: Предел глубины обхода цепочки — защита от зацикливания на испорченных данных.
MAX_CHAIN_DEPTH = 1000
#: Ограничения схемы, нарушение которых означает повтор связи.
REPEAT_CONSTRAINTS = frozenset({"uq_links_pair", "uq_links_one_main", "ck_links_not_self"})

_CHAIN_UP = text(
    """
    WITH RECURSIVE chain(id, depth) AS (
        SELECT CAST(:start AS integer), 0
        UNION ALL
        SELECT l.target_id, c.depth + 1
        FROM links l JOIN chain c ON l.source_id = c.id
        WHERE l.kind = 'duplicate' AND c.depth < :max_depth
    )
    SELECT id FROM chain ORDER BY depth
    """
)
_CHAIN_DOWN = text(
    """
    WITH RECURSIVE grp(id, depth) AS (
        SELECT l.source_id, 1 FROM links l
        WHERE l.target_id = :main AND l.kind = 'duplicate'
        UNION ALL
        SELECT l.source_id, g.depth + 1
        FROM links l JOIN grp g ON l.target_id = g.id
        WHERE l.kind = 'duplicate' AND g.depth < :max_depth
    )
    SELECT id FROM grp ORDER BY id
    """
)


class RelationError(ValueError):
    """Базовая ошибка операций со связями."""


class SelfLinkError(RelationError):
    """Связь обращения с самим собой."""


class LinkExistsError(RelationError):
    """Между обращениями уже есть связь или источник уже дубликат."""


class CycleError(RelationError):
    """Связь «дубликат» замкнула бы цепочку основных обращений."""


class HasDuplicatesError(RelationError):
    """Обращение уже основное для других дубликатов и не может стать дубликатом."""


class LinkIntegrityError(RelationError):
    """Связь нарушает ограничение схемы, не связанное с её повтором.

    Например, указана несуществующая или уже использованная рекомендация.
    """


def main_chain(session: Session, ticket_id: int) -> list[int]:
    """Возвращает цепочку основных обращений, начиная с самого обращения.

    Args:
        session: Сессия БД.
        ticket_id: Номер обращения.

    Returns:
        Номера ``[ticket_id, основное, основное основного, …]``; последний
        элемент — корневое основное обращение группы.
    """
    rows = session.execute(_CHAIN_UP, {"start": ticket_id, "max_depth": MAX_CHAIN_DEPTH})
    chain = [row.id for row in rows]
    if len(chain) > MAX_CHAIN_DEPTH:
        # Обрезанная цепочка дала бы неверный корень и пропуск цикла.
        raise RelationError(f"Цепочка основных обращений от {ticket_id} длиннее допустимой")
    return chain


def root_main(session: Session, ticket_id: int) -> int:
    """Возвращает корневое основное обращение группы дубликатов.

    Args:
        session: Сессия БД.
        ticket_id: Номер обращения.

    Returns:
        Номер корневого основного обращения или ``ticket_id``, если
        обращение не является дубликатом.
    """
    return main_chain(session, ticket_id)[-1]


def duplicates_of(session: Session, main_id: int) -> list[int]:
    """Возвращает все прямые и косвенные дубликаты основного обращения.

    Args:
        session: Сессия БД.
        main_id: Номер основного обращения.

    Returns:
        Номера дубликатов по возрастанию.
    """
    rows = session.execute(_CHAIN_DOWN, {"main": main_id, "max_depth": MAX_CHAIN_DEPTH})
    return [row.id for row in rows]


def _pair_link(session: Session, a: int, b: int) -> Link | None:
    """Возвращает связь между двумя обращениями в любом направлении."""
    stmt = select(Link).where(
        or_(
            (Link.source_id == a) & (Link.target_id == b),
            (Link.source_id == b) & (Link.target_id == a),
        )
    )
    return session.scalar(stmt)


def _resolve_duplicate_target(session: Session, source_id: int, target_id: int) -> int:
    """Проверяет связь «дубликат» на циклы и находит основное обращение.

    Если цель сама закрыта как дубликат, основным становится корень её
    группы: группы остаются плоскими, решение основного обращения
    распространяется на все дубликаты.

    Вызывается под advisory-блокировкой графа дубликатов.

    Raises:
        LinkExistsError: Источник уже является дубликатом.
        HasDuplicatesError: К источнику уже отнесены дубликаты.
        CycleError: Источник лежит на цепочке основных от цели.
    """
    if len(main_chain(session, source_id)) > 1:
        raise LinkExistsError(f"Обращение {source_id} уже отмечено как дубликат")
    chain = main_chain(session, target_id)
    if source_id in chain:
        # Промежуточные номера цепочки не показываются: среди них могут быть чужие.
        raise CycleError(f"Связь {source_id} → {target_id} образует цикл дубликатов")
    if duplicates_of(session, source_id):
        raise HasDuplicatesError(
            f"К обращению {source_id} уже отнесены дубликаты; оно не может стать дубликатом"
        )
    return chain[-1]


def _lock_duplicate_graph(session: Session) -> None:
    """Берёт транзакционную advisory-блокировку графа дубликатов."""
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": DUPLICATE_GRAPH_LOCK})


def _insert(session: Session, link: Link, requested_target: int) -> None:
    """Вставляет связь в точке сохранения, разбирая нарушения ограничений.

    Raises:
        LinkExistsError: Нарушено ограничение повтора связи.
        LinkIntegrityError: Нарушено иное ограничение (рекомендация).
    """
    try:
        with session.begin_nested():
            session.add(link)
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint in REPEAT_CONSTRAINTS:
            raise LinkExistsError(
                f"Обращения {link.source_id} и {requested_target} уже связаны"
            ) from exc
        raise LinkIntegrityError("Связь не может быть сохранена: рекомендация недоступна") from exc


def create_link(
    session: Session,
    actor: Actor,
    *,
    source_id: int,
    target_id: int,
    kind: RelationKind,
    recommendation_id: int | None = None,
) -> Link:
    """Устанавливает связь между обращениями.

    Для вида «дубликат» источник переводится в статус «Закрыто как
    дубликат» (Ф7); вид «связанное» статусы не меняет (Ф8). В историю
    обоих обращений и в журнал действий записывается установление связи.

    Args:
        session: Сессия БД.
        actor: Пользователь: автор источника или оператор.
        source_id: Номер обращения-источника (для «дубликата» — дубликат).
        target_id: Номер обращения-цели (для «дубликата» — основное).
        kind: Вид связи.
        recommendation_id: Рекомендация, по которой создана связь, или ``None``.

    Returns:
        Созданная связь; для «дубликата» её цель — основное обращение
        группы.

    Raises:
        SelfLinkError: Источник и цель совпадают.
        TicketNotFoundError: Источник недоступен пользователю, цель или
            основное обращение её группы не существует либо скрыто.
        LinkExistsError: Связь между обращениями уже есть или источник уже
            является дубликатом.
        HasDuplicatesError: К источнику «дубликата» уже отнесены дубликаты.
        CycleError: Связь «дубликат» образует прямой или косвенный цикл.
        LinkIntegrityError: Указанная рекомендация недоступна.
        TransitionError: Источник уже решён или закрыт.
    """
    if source_id == target_id:
        raise SelfLinkError("Обращение нельзя связать с самим собой")
    if kind is RelationKind.DUPLICATE:
        _lock_duplicate_graph(session)  # до блокировок строк — единый порядок
    source = tickets.get_ticket(session, actor, source_id, lock=True)
    tickets.get_link_target(session, actor, target_id)
    main_id = target_id
    if kind is RelationKind.DUPLICATE:
        # Отказать до вставки связи, если источник уже решён или закрыт.
        check_transition(source.status, TicketStatus.CLOSED_DUPLICATE, Trigger.DUPLICATE_LINK)
        main_id = _resolve_duplicate_target(session, source_id, target_id)
        if main_id != target_id:
            # Основное обращение группы тоже должно быть доступно для ссылки;
            # в ошибке — только запрошенный номер, чтобы не раскрыть скрытое.
            try:
                tickets.get_link_target(session, actor, main_id)
            except TicketNotFoundError as exc:
                raise TicketNotFoundError(f"Обращение {target_id} не найдено") from exc
    if _pair_link(session, source_id, main_id) is not None:
        raise LinkExistsError(f"Обращения {source_id} и {target_id} уже связаны")
    link = Link(
        source_id=source_id,
        target_id=main_id,
        kind=kind,
        author_id=actor.id,
        recommendation_id=recommendation_id,
    )
    _insert(session, link, target_id)
    _apply_link_effects(session, actor, link, requested_target=target_id)
    return link


def _apply_link_effects(session: Session, actor: Actor, link: Link, requested_target: int) -> None:
    """Меняет статус дубликата и записывает историю обоих обращений и журнал."""
    note = ""
    if link.target_id != requested_target:
        note = f"Основное обращение группы — {link.target_id} (выбрано {requested_target})"
    if link.kind is RelationKind.DUPLICATE:
        tickets.close_as_duplicate(session, actor, link.source_id, link.target_id)
    tickets.record_link(session, actor, link.source_id, link.target_id, link.kind.value, note)
    tickets.record_link(session, actor, link.target_id, link.source_id, link.kind.value, note)
    record_action(
        session,
        user_id=actor.id,
        action=ActionKind.LINK_CREATED,
        ticket_id=link.source_id,
        other_ticket_id=link.target_id,
        details=link.kind.value + (f"; {note}" if note else ""),
    )
    session.flush()


def list_links(session: Session, actor: Actor, ticket_id: int) -> list[Link]:
    """Возвращает связи обращения в порядке установления (для карточки).

    Args:
        session: Сессия БД.
        actor: Пользователь, которому доступно обращение.
        ticket_id: Номер обращения.

    Returns:
        Связи, в которых обращение — источник или цель.

    Raises:
        TicketNotFoundError: Обращение не существует или чужое.
    """
    tickets.get_ticket(session, actor, ticket_id)
    stmt = (
        select(Link)
        .where(or_(Link.source_id == ticket_id, Link.target_id == ticket_id))
        .order_by(Link.id)
    )
    return list(session.scalars(stmt))
