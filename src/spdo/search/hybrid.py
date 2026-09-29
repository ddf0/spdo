"""Основной режим: гибридное ранжирование."""

from spdo.search.base import Candidate, SimilaritySearcher, TicketQuery


class HybridSearcher(SimilaritySearcher):
    """Гибридный поиск похожих обращений.

    Производный класс :class:`~spdo.search.base.SimilaritySearcher`.
    Вычисляет S = w1·S_sem + w2·S_lex + w3·S_cat + w4·S_comp.

    Attributes:
        weights: Весовые коэффициенты ``(w1, w2, w3, w4)``, сумма равна 1.
        model_name: Идентификатор модели sentence-transformers.
    """

    mode = "hybrid"

    def __init__(
        self,
        threshold_duplicate: float,
        threshold_related: float,
        weights: tuple[float, float, float, float],
        model_name: str,
    ) -> None:
        """Создаёт гибридный поисковик.

        Args:
            threshold_duplicate: Порог P1.
            threshold_related: Порог P2.
            weights: Весовые коэффициенты w1–w4.
            model_name: Идентификатор модели векторного представления.
        """
        super().__init__(threshold_duplicate, threshold_related)
        self.weights = weights
        self.model_name = model_name

    @property
    def model_version(self) -> str:
        """Идентификатор используемой модели."""
        return self.model_name

    def search(self, query: TicketQuery, limit: int) -> list[Candidate]:
        """Возвращает кандидатов по гибридному сходству.

        Args:
            query: Данные нового обращения.
            limit: Максимальное число кандидатов.

        Returns:
            Кандидаты, отсортированные по убыванию S.
        """
        raise NotImplementedError
