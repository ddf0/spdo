"""Реестр моделей всех модулей.

Импорт этого модуля регистрирует все таблицы в ``Base.metadata``. Нужен
Alembic для автогенерации миграций и приложению — чтобы связи между
моделями, заданные строками, разрешались при первом обращении к БД.
"""

from spdo.analytics.models import ActionLogEntry
from spdo.recommendations.models import Recommendation, SearchSession
from spdo.relations.models import Link
from spdo.search.models import SearchIndexEntry
from spdo.tickets.models import (
    Category,
    Component,
    HistoryEntry,
    Solution,
    SolutionRating,
    Ticket,
    TicketDraft,
)
from spdo.users.models import User

__all__ = [
    "ActionLogEntry",
    "Category",
    "Component",
    "HistoryEntry",
    "Link",
    "Recommendation",
    "SearchIndexEntry",
    "SearchSession",
    "Solution",
    "SolutionRating",
    "Ticket",
    "TicketDraft",
    "User",
]
