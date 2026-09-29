"""Поиск похожих обращений.

Модули:
    base: Интерфейс :class:`SimilaritySearcher` и структуры данных.
    lexical: Базовый режим — лексическое сходство.
    hybrid: Гибридный режим — взвешенная сумма четырёх составляющих.

Пакет не зависит от бизнес-модулей; они обращаются к нему только через
:class:`~spdo.search.base.SimilaritySearcher`.
"""

from spdo.search.base import Candidate, LinkKind, SimilaritySearcher, TicketQuery

__all__ = ["Candidate", "LinkKind", "SimilaritySearcher", "TicketQuery"]
