"""Поиск похожих обращений.

Модули:
    base: Интерфейс :class:`SimilaritySearcher` и структуры данных.
    lexical: Базовый режим — лексическое сходство.
    hybrid: Гибридный режим — взвешенная сумма четырёх составляющих.
    models: Таблица поискового индекса.
    factory: Создание поисковика по конфигурации.

Пакет не зависит от бизнес-модулей; они обращаются к нему только через
:class:`~spdo.search.base.SimilaritySearcher`.
"""

from spdo.search.base import (
    Candidate,
    IndexedTicket,
    LinkKind,
    SearchScope,
    SimilaritySearcher,
    TicketQuery,
)

__all__ = [
    "Candidate",
    "IndexedTicket",
    "LinkKind",
    "SearchScope",
    "SimilaritySearcher",
    "TicketQuery",
]
