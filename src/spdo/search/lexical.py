"""Базовый режим: лексическое сходство (TF-IDF / BM25)."""

from spdo.search.base import Candidate, SimilaritySearcher, TicketQuery


class LexicalSearcher(SimilaritySearcher):
    """Поиск по лексическому сходству текстов.

    Производный класс :class:`~spdo.search.base.SimilaritySearcher`.
    Используется как базовый режим для сравнения с гибридным (w1 = 0).
    """

    mode = "lexical"

    @property
    def model_version(self) -> str:
        """Версия алгоритма лексического поиска."""
        return "tfidf-1"

    def search(self, query: TicketQuery, limit: int) -> list[Candidate]:
        """Возвращает кандидатов по лексическому сходству.

        Args:
            query: Данные нового обращения.
            limit: Максимальное число кандидатов.

        Returns:
            Кандидаты, отсортированные по убыванию сходства.
        """
        raise NotImplementedError
