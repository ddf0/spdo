"""Агрегаты сеансов поиска и рекомендаций за период (для Ф14).

Подбор при вводе создаёт несколько сеансов на одно обращение, поэтому
доля сеансов с подтверждённым дубликатом считается от сеансов,
завершённых регистрацией. «Подтверждённый дубликат» — рекомендация, по
которой установлена связь вида «дубликат»; номера таких рекомендаций
передаёт модуль ``relations``.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, Select, and_, distinct, func, select
from sqlalchemy.orm import Session

from spdo.recommendations.models import Recommendation, SearchSession, Verdict

#: Глубина выдачи, по которой считаются дубликаты «в первых трёх позициях».
TOP = 3


@dataclass(frozen=True, slots=True)
class SessionStats:
    """Сеансы поиска за период.

    Attributes:
        total: Все сеансы.
        failed: Сеансы, в которых поиск был недоступен.
        registered: Сеансы, завершённые регистрацией обращения.
        with_duplicate: Сеансы с подтверждённым дубликатом.
    """

    total: int
    failed: int
    registered: int
    with_duplicate: int


@dataclass(frozen=True, slots=True)
class RecommendationStats:
    """Рекомендации сеансов за период.

    Attributes:
        total: Все показанные рекомендации.
        accepted: Принятые.
        rejected: Отклонённые.
        pending: Без вердикта.
        duplicates: Подтверждённые дубликаты.
        duplicates_top: Подтверждённые дубликаты в первых :data:`TOP` позициях.
    """

    total: int
    accepted: int
    rejected: int
    pending: int
    duplicates: int
    duplicates_top: int


def _in_period(start: datetime, end: datetime) -> ColumnElement[bool]:
    """Условие «сеанс создан в полуинтервале [start, end)»."""
    return and_(SearchSession.created_at >= start, SearchSession.created_at < end)


def session_stats(
    session: Session, start: datetime, end: datetime, duplicate_ids: Select[tuple[int]]
) -> SessionStats:
    """Считает сеансы поиска за период.

    Args:
        session: Сессия БД.
        start: Начало периода (включительно).
        end: Конец периода (не включительно).
        duplicate_ids: Запрос номеров рекомендаций, подтверждённых как дубликат.

    Returns:
        Число сеансов по видам.
    """
    total, failed, registered = session.execute(
        select(
            func.count(),
            func.count().filter(SearchSession.failed),
            func.count().filter(SearchSession.ticket_id.is_not(None)),
        ).where(_in_period(start, end))
    ).one()
    with_duplicate = session.scalar(
        select(func.count(distinct(SearchSession.id)))
        .join(Recommendation, Recommendation.session_id == SearchSession.id)
        .where(
            _in_period(start, end),
            SearchSession.ticket_id.is_not(None),  # та же совокупность, что в знаменателе
            Recommendation.verdict == Verdict.ACCEPTED,
            Recommendation.id.in_(duplicate_ids),
        )
    )
    return SessionStats(total, failed, registered, with_duplicate or 0)


def recommendation_stats(
    session: Session, start: datetime, end: datetime, duplicate_ids: Select[tuple[int]]
) -> RecommendationStats:
    """Считает рекомендации сеансов за период.

    Args:
        session: Сессия БД.
        start: Начало периода (включительно).
        end: Конец периода (не включительно).
        duplicate_ids: Запрос номеров рекомендаций, подтверждённых как дубликат.

    Returns:
        Число рекомендаций по вердиктам и подтверждённых дубликатов.
    """
    confirmed = and_(
        Recommendation.verdict == Verdict.ACCEPTED, Recommendation.id.in_(duplicate_ids)
    )
    row = session.execute(
        select(
            func.count(),
            func.count().filter(Recommendation.verdict == Verdict.ACCEPTED),
            func.count().filter(Recommendation.verdict == Verdict.REJECTED),
            func.count().filter(Recommendation.verdict == Verdict.PENDING),
            func.count().filter(confirmed),
            func.count().filter(confirmed, Recommendation.position <= TOP),
        )
        .join(SearchSession, Recommendation.session_id == SearchSession.id)
        .where(_in_period(start, end))
    ).one()
    return RecommendationStats(*row)
