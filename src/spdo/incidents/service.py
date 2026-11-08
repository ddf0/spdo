"""Массовые однотипные обращения (Ф20).

Группы ищутся среди свежих открытых обращений за окно
``incident_window_hours``: поисковик вычисляет попарный коэффициент
сходства S той же формулой, что и при подборе; группа — центр и обращения
с S не ниже P1 + ``incident_threshold_offset`` непосредственно с ним, не
меньше ``incident_min_size`` (см. :func:`_groups`). Объединение в инцидент — это связи «дубликат» с
выбранным основным обращением (Ф7, Ф21): статусы, история, журнал и запрет
циклов — как при ручной связи. Решение принимает оператор.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy.orm import Session

from spdo.config import settings
from spdo.relations import service as relations
from spdo.relations.models import RelationKind
from spdo.relations.service import RelationError
from spdo.search.factory import get_searcher
from spdo.tickets import service as tickets
from spdo.tickets.access import Actor, TicketNotFoundError, TicketValidationError, require_staff
from spdo.tickets.models import Ticket
from spdo.tickets.transitions import TransitionError


@dataclass(frozen=True, slots=True)
class Incident:
    """Группа однотипных обращений.

    Attributes:
        main: Центр группы — предлагаемое основное обращение.
        tickets: Обращения группы от старых к новым.
        min_score: Наименьший S обращения группы с центром.
    """

    main: Ticket
    tickets: list[Ticket]
    min_score: float


def _groups(
    matrix: np.ndarray, threshold: float, min_size: int
) -> list[tuple[int, list[int], float]]:
    """Группы «звездой» вокруг центров.

    Связные компоненты графа «S ≥ порога» склеивали разные проблемы через
    промежуточные обращения (Wi-Fi — пароль — почта). Поэтому группа — это
    центр и обращения, сходные **непосредственно с ним**. Центром выбирается
    нераспределённое обращение с наибольшим числом таких соседей; затем его
    группа исключается, и выбор повторяется.

    Args:
        matrix: Симметричная матрица S.
        threshold: Порог сходства с центром.
        min_size: Наименьший размер группы.

    Returns:
        Тройки (индекс центра, индексы группы по возрастанию, наименьший S с
        центром).
    """
    adjacency = matrix >= threshold
    free = np.ones(len(matrix), dtype=bool)
    groups = []
    while True:
        degree = (adjacency & free[None, :]).sum(axis=1) * free
        center = int(degree.argmax())
        if degree[center] + 1 < min_size:
            return groups
        members = np.flatnonzero(adjacency[center] & free)
        groups.append(
            (center, sorted([center, *members.tolist()]), float(matrix[center, members].min()))
        )
        free[members] = free[center] = False


def detect(session: Session, actor: Actor, now: datetime) -> list[Incident]:
    """Находит группы однотипных обращений за окно (Ф20).

    Args:
        session: Сессия БД.
        actor: Оператор или администратор.
        now: Текущий момент.

    Returns:
        Группы, начиная с самых крупных.

    Raises:
        PermissionDeniedError: Пользователь не оператор.
        SearchUnavailableError: Поиск временно недоступен.
    """
    require_staff(actor)
    since = now - timedelta(hours=settings.incident_window_hours)
    recent = tickets.recent_open(session, actor, since, settings.incident_max_tickets)
    if len(recent) < settings.incident_min_size:
        return []
    searcher = get_searcher()
    matrix = searcher.pairwise(session, [t.id for t in recent])
    threshold = min(searcher.tuning.threshold_duplicate + settings.incident_threshold_offset, 1.0)
    found = [
        Incident(recent[center], [recent[i] for i in idx], score)
        for center, idx, score in _groups(matrix, threshold, settings.incident_min_size)
    ]
    return sorted(found, key=lambda inc: (-len(inc.tickets), inc.tickets[0].id))


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Итог объединения в инцидент.

    Attributes:
        merged: Обращения, ставшие дубликатами основного.
        skipped: Обращения, которые связать не удалось, с причиной.
    """

    merged: list[int]
    skipped: list[tuple[int, str]]


def merge(session: Session, actor: Actor, main_id: int, ticket_ids: list[int]) -> MergeResult:
    """Объединяет обращения в инцидент: делает их дубликатами основного.

    Каждая связь создаётся в своей точке сохранения: обращение, которое
    связать нельзя (уже решено, уже дубликат, цикл), пропускается с
    причиной, остальные объединяются.

    Args:
        session: Сессия БД.
        actor: Оператор или администратор.
        main_id: Основное обращение инцидента.
        ticket_ids: Обращения группы (основное в списке допускается).

    Returns:
        Итог объединения.

    Raises:
        PermissionDeniedError: Пользователь не оператор.
        TicketValidationError: Не выбрано ни одного обращения кроме основного.
        TicketNotFoundError: Основное обращение не найдено.
    """
    require_staff(actor)
    tickets.get_ticket(session, actor, main_id)
    others = [tid for tid in dict.fromkeys(ticket_ids) if tid != main_id]
    if not others:
        raise TicketValidationError("Выберите обращения для объединения")
    merged, skipped = [], []
    for tid in others:
        try:
            with session.begin_nested():
                relations.create_link(
                    session, actor, source_id=tid, target_id=main_id, kind=RelationKind.DUPLICATE
                )
            merged.append(tid)
        except (RelationError, TransitionError, TicketNotFoundError, TicketValidationError) as exc:
            skipped.append((tid, str(exc)))
    return MergeResult(merged, skipped)
