"""Сеансы поиска, рекомендации и вердикты (Ф3, Ф4, Ф9, Ф10; ПЦ1, ПЦ2).

Каждый запуск подбора фиксируется сеансом поиска с режимом, версией модели
и снимком параметров. Показанные рекомендации получают вердикт: принята
(создана связь), отклонена пользователем (Ф9) или отклонена при регистрации
независимого обращения (Ф10) — по ПЦ1 вердикт записывается для каждой
показанной рекомендации.

Функции не фиксируют транзакцию: регистрация, связь, вердикты и записи
журнала попадают в БД одним коммитом обработчика запроса.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from spdo.analytics.journal import record_action
from spdo.analytics.models import ActionKind
from spdo.config import settings
from spdo.recommendations.models import Recommendation, SearchSession, Verdict
from spdo.relations import service as relations
from spdo.relations.models import RelationKind
from spdo.search.base import Candidate, SearchScope, SearchUnavailableError, TicketQuery
from spdo.search.factory import get_searcher
from spdo.tickets import service as tickets
from spdo.tickets.access import Actor, TicketNotFoundError, TicketValidationError, is_staff
from spdo.tickets.models import DESCRIPTION_MAX, TITLE_MAX, Ticket, TicketStatus

logger = logging.getLogger(__name__)

#: Предупреждение пользователю при недоступности подбора (ТЗ).
UNAVAILABLE_NOTICE = (
    "Подбор похожих обращений временно недоступен. Обращение можно зарегистрировать."
)


class RecommendationError(ValueError):
    """Базовая ошибка операций с рекомендациями."""


class RecommendationNotFoundError(RecommendationError):
    """Рекомендация или сеанс не найдены или недоступны пользователю."""


class VerdictError(RecommendationError):
    """Вердикт по рекомендации уже вынесен и не может быть изменён."""


class SessionUsedError(RecommendationError):
    """По сеансу поиска уже зарегистрировано обращение."""


#: Запас кандидатов: часть может отпасть при замене дубликатов основными
#: обращениями, скрытии и исключении повторов.
OVERFETCH = 2


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Результат подбора для показа пользователю.

    Attributes:
        search_session: Зафиксированный сеанс поиска.
        recommendations: Рекомендации по позициям.
        unavailable: Подбор не выполнен (поиск недоступен).
        notice: Предупреждение пользователю или ``None``.
    """

    search_session: SearchSession
    recommendations: list[Recommendation]
    unavailable: bool
    notice: str | None


def _decimal(value: float) -> str:
    """Число с двумя знаками и десятичной запятой, как в интерфейсе."""
    return f"{value:.2f}".replace(".", ",")


def explain(components: dict[str, float]) -> str:
    """Формирует объяснение сходства из его составляющих.

    Args:
        components: Составляющие ``sem``, ``lex``, ``cat``, ``comp``.

    Returns:
        Краткое объяснение для пользователя.
    """
    parts = []
    if components.get("sem", 0) > 0:
        parts.append(f"смысловое сходство {_decimal(components['sem'])}")
    if components.get("lex", 0) > 0:
        parts.append(f"общие слова и фрагменты {_decimal(components['lex'])}")
    if components.get("cat"):
        parts.append("та же категория")
    if components.get("comp"):
        parts.append("тот же компонент")
    return "; ".join(parts) or "сходство по совокупности признаков"


def _main_candidate(session: Session, actor: Actor, candidate: Candidate) -> int | None:
    """Номер обращения для показа вместо кандидата или ``None``, если скрыт.

    Кандидат, закрытый как дубликат, заменяется основным обращением его
    группы: решение основного распространяется на дубликаты (ТЗ).
    """
    try:
        ticket = tickets.get_link_target(session, actor, candidate.ticket_id)
        if ticket.status is not TicketStatus.CLOSED_DUPLICATE:
            return ticket.id
        main_id = relations.root_main(session, ticket.id)
        return tickets.get_link_target(session, actor, main_id).id
    except TicketNotFoundError:
        return None


def _unique_mains(
    session: Session, actor: Actor, candidates: list[Candidate], exclude: frozenset[int]
) -> list[tuple[int, Candidate]]:
    """Заменяет дубликаты основными обращениями и убирает повторы по убыванию S.

    Основное обращение показывается с S и составляющими найденного
    дубликата: сходство вычислено для его текста, что отмечается в
    объяснении.
    """
    shown: dict[int, Candidate] = {}
    for c in candidates:
        main_id = _main_candidate(session, actor, c)
        if main_id is not None and main_id not in exclude and main_id not in shown:
            shown[main_id] = c
    return list(shown.items())[: settings.top_n]


def run_search(
    session: Session,
    actor: Actor,
    *,
    title: str,
    description: str,
    category_id: int | None = None,
    component_id: int | None = None,
    exclude_ticket_id: int | None = None,
) -> SearchOutcome:
    """Выполняет подбор похожих обращений и фиксирует сеанс поиска (Ф3, Ф4).

    Недоступность поиска не прерывает работу: сеанс сохраняется с
    признаком «подбор не выполнен», пользователь получает предупреждение.

    Args:
        session: Сессия БД.
        actor: Пользователь, для которого выполняется подбор.
        title: Заголовок нового обращения.
        description: Описание нового обращения.
        category_id: Категория или ``None``.
        component_id: Компонент или ``None``.
        exclude_ticket_id: Обращение, не включаемое в выдачу (при подборе
            для уже зарегистрированного обращения).

    Returns:
        Сеанс поиска и рекомендации.

    Raises:
        TicketValidationError: Текст запроса пуст или слишком длинный.
    """
    title, description = title.strip(), description.strip()
    if not (title or description):
        raise TicketValidationError("Для подбора нужен заголовок или описание")
    if len(title) > TITLE_MAX or len(description) > DESCRIPTION_MAX:
        raise TicketValidationError("Текст запроса длиннее допустимого")
    tickets.check_dictionaries(session, category_id, component_id)
    searcher = get_searcher()
    exclude = frozenset({exclude_ticket_id} if exclude_ticket_id else ())
    query = TicketQuery(
        title=title,
        description=description,
        category_id=category_id,
        component_id=component_id,
        scope=SearchScope(viewer_id=actor.id, sees_confidential=is_staff(actor)),
        exclude_ids=exclude,
    )
    record = SearchSession(
        user_id=actor.id,
        query_text=query.text,
        category_id=category_id,
        component_id=component_id,
        mode=searcher.mode,
        model_version=searcher.model_version,
        params={**searcher.params(), "top_n": settings.top_n},
    )
    session.add(record)
    try:
        found = searcher.search(session, query, settings.top_n * OVERFETCH)
    except SearchUnavailableError as exc:
        logger.warning("Подбор не выполнен: %s", exc)
        record.failed = True
        session.flush()
        return SearchOutcome(record, [], True, UNAVAILABLE_NOTICE)
    for position, (ticket_id, c) in enumerate(_unique_mains(session, actor, found, exclude), 1):
        record.recommendations.append(
            Recommendation(
                ticket_id=ticket_id,
                position=position,
                score=c.score,
                s_sem=c.components.get("sem", 0.0),
                s_lex=c.components.get("lex", 0.0),
                s_cat=c.components.get("cat", 0.0),
                s_comp=c.components.get("comp", 0.0),
                suggested_kind=c.kind,
                explanation=explain(c.components)
                + ("" if ticket_id == c.ticket_id else f"; найден дубликат №{c.ticket_id}"),
            )
        )
    record.candidates_count = len(record.recommendations)
    session.flush()
    return SearchOutcome(record, list(record.recommendations), False, None)


def _accessible_session(
    session: Session, actor: Actor, session_id: int, *, staff_allowed: bool = True
) -> SearchSession:
    """Сеанс поиска пользователя; оператору — любой, если ``staff_allowed``."""
    record = session.get(SearchSession, session_id, with_for_update={"key_share": True})
    owner = record is not None and record.user_id == actor.id
    if record is None or not (owner or (staff_allowed and is_staff(actor))):
        raise RecommendationNotFoundError(f"Сеанс поиска {session_id} не найден")
    return record


def _accessible_recommendation(
    session: Session, actor: Actor, recommendation_id: int, *, staff_allowed: bool = True
) -> Recommendation:
    """Рекомендация доступного сеанса, заблокированная и перечитанная.

    ``FOR NO KEY UPDATE`` с перечитыванием строки: параллельные вердикты
    по одной рекомендации выполняются по очереди и видят результат друг
    друга.
    """
    rec = session.get(
        Recommendation,
        recommendation_id,
        with_for_update={"key_share": True},
        populate_existing=True,
    )
    if rec is None:
        raise RecommendationNotFoundError(f"Рекомендация {recommendation_id} не найдена")
    _accessible_session(session, actor, rec.session_id, staff_allowed=staff_allowed)
    return rec


def was_recommended(session: Session, actor: Actor, ticket_id: int) -> bool:
    """Показывалось ли обращение пользователю в его рекомендациях.

    Условие доступа к чужому обращению: по ТЗ чужие обращения видны только
    в рекомендациях.

    Args:
        session: Сессия БД.
        actor: Пользователь.
        ticket_id: Номер обращения.

    Returns:
        ``True``, если есть рекомендация этого обращения в сеансе пользователя.
    """
    stmt = (
        select(Recommendation.id)
        .join(SearchSession, SearchSession.id == Recommendation.session_id)
        .where(Recommendation.ticket_id == ticket_id, SearchSession.user_id == actor.id)
        .limit(1)
    )
    return session.scalar(stmt) is not None


def card(session: Session, actor: Actor, ticket_id: int) -> tickets.TicketCard:
    """Карточка обращения (Ф5): чужое — только если показывалось пользователю.

    Args:
        session: Сессия БД.
        actor: Пользователь.
        ticket_id: Номер обращения.

    Returns:
        Карточка обращения.

    Raises:
        TicketNotFoundError: Обращение недоступно пользователю.
    """
    shown = was_recommended(session, actor, ticket_id)
    return tickets.get_card(session, actor, ticket_id, shown=shown)


def rate(session: Session, actor: Actor, solution_id: int, helped: bool) -> None:
    """Оценка решения (Ф6): чужое — только если обращение показывалось.

    Args:
        session: Сессия БД.
        actor: Пользователь.
        solution_id: Номер решения.
        helped: Помогло ли решение.

    Raises:
        TicketNotFoundError: Решение недоступно пользователю.
    """
    ticket_id = tickets.solution_ticket_id(session, solution_id)
    shown = ticket_id is not None and was_recommended(session, actor, ticket_id)
    tickets.rate_solution(session, actor, solution_id, helped, shown=shown)


def _set_verdict(actor: Actor, rec: Recommendation, verdict: Verdict) -> None:
    """Записывает вердикт; принятую рекомендацию отклонить нельзя."""
    if rec.verdict is verdict:
        return
    if rec.verdict is not Verdict.PENDING:
        raise VerdictError(f"По рекомендации {rec.id} уже вынесен вердикт")
    rec.verdict = verdict
    rec.verdict_at = datetime.now(UTC)
    rec.verdict_by = actor.id


def reject(session: Session, actor: Actor, recommendation_id: int) -> Recommendation:
    """Отклоняет рекомендацию с сохранением вердикта (Ф9).

    Args:
        session: Сессия БД.
        actor: Автор сеанса поиска или оператор.
        recommendation_id: Номер рекомендации.

    Returns:
        Рекомендация с вердиктом «отклонена».

    Raises:
        RecommendationNotFoundError: Рекомендация недоступна пользователю.
        VerdictError: Рекомендация уже принята.
    """
    rec = _accessible_recommendation(session, actor, recommendation_id)
    was_pending = rec.verdict is Verdict.PENDING
    _set_verdict(actor, rec, Verdict.REJECTED)
    if was_pending:
        # журнал требует обращение: до регистрации — найденное обращение
        source = rec.session.ticket_id or rec.ticket_id
        record_action(
            session,
            user_id=actor.id,
            action=ActionKind.RECOMMENDATION_REJECTED,
            ticket_id=source,
            other_ticket_id=rec.ticket_id,
            details=f"рекомендация {rec.id}",
        )
    session.flush()
    return rec


@dataclass(frozen=True, slots=True)
class LinkChoice:
    """Выбор пользователя при регистрации: связать с рекомендацией.

    Attributes:
        recommendation_id: Номер принятой рекомендации.
        kind: Вид связи: «дубликат» (Ф7) или «связанное» (Ф8).
    """

    recommendation_id: int
    kind: RelationKind


def register(
    session: Session,
    actor: Actor,
    *,
    title: str,
    description: str,
    category_id: int,
    component_id: int,
    session_id: int | None = None,
    draft_id: int | None = None,
    link: LinkChoice | None = None,
) -> Ticket:
    """Регистрирует обращение по итогам подбора (ПЦ1; Ф1, Ф7, Ф8, Ф10).

    В одной транзакции: обращение со статусом «Новое», привязка сеанса
    поиска, связь с выбранной рекомендацией (при выборе «дубликат» —
    закрытие как дубликат) и вердикты: выбранная рекомендация принимается,
    остальные нерассмотренные отклоняются.

    Args:
        session: Сессия БД.
        actor: Автор обращения.
        title: Заголовок.
        description: Описание.
        category_id: Категория.
        component_id: Компонент.
        session_id: Сеанс поиска, по итогам которого регистрируется обращение.
        draft_id: Черновик, который удаляется после регистрации.
        link: Принятая рекомендация и вид связи или ``None`` — независимое.

    Returns:
        Зарегистрированное обращение.

    Raises:
        TicketValidationError: Данные обращения неверны.
        RecommendationNotFoundError: Сеанс или рекомендация недоступны (в том
            числе чужие — даже оператору).
        RecommendationError: Рекомендация не из указанного сеанса.
        SessionUsedError: По сеансу уже зарегистрировано обращение.
        VerdictError: Выбранная рекомендация уже отклонена.
        RelationError: Связь невозможна (цикл, уже есть и т. п.).
    """
    record = None
    if session_id is not None:
        record = _accessible_session(session, actor, session_id, staff_allowed=False)
        if record.ticket_id is not None:
            raise SessionUsedError(f"По сеансу поиска {session_id} уже зарегистрировано обращение")
    chosen = None
    if link is not None:
        chosen = _accessible_recommendation(
            session, actor, link.recommendation_id, staff_allowed=False
        )
        if record is None or chosen.session_id != record.id:
            raise RecommendationError("Рекомендация не относится к сеансу поиска")
    ticket = tickets.create_ticket(
        session,
        actor,
        title=title,
        description=description,
        category_id=category_id,
        component_id=component_id,
        draft_id=draft_id,
    )
    if record is not None:
        record.ticket_id = ticket.id
    if chosen is not None:
        _set_verdict(actor, chosen, Verdict.ACCEPTED)
        relations.create_link(
            session,
            actor,
            source_id=ticket.id,
            target_id=chosen.ticket_id,
            kind=link.kind,
            recommendation_id=chosen.id,
        )
    if record is not None:
        _reject_rest(session, actor, record, ticket.id)
    session.flush()
    return ticket


def _reject_rest(session: Session, actor: Actor, record: SearchSession, ticket_id: int) -> None:
    """Отклоняет нерассмотренные рекомендации сеанса при регистрации (ПЦ1)."""
    pending = session.scalars(
        select(Recommendation).where(
            Recommendation.session_id == record.id, Recommendation.verdict == Verdict.PENDING
        )
    )
    for rec in pending:
        _set_verdict(actor, rec, Verdict.REJECTED)
        record_action(
            session,
            user_id=actor.id,
            action=ActionKind.RECOMMENDATION_REJECTED,
            ticket_id=ticket_id,
            other_ticket_id=rec.ticket_id,
            details=f"рекомендация {rec.id}; зарегистрировано обращение {ticket_id}",
        )
