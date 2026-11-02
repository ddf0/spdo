"""Кандидаты гибридного режима для офлайн-оценки и подбора весов.

Отбор кандидатов повторяет приложение, но поиск ближайших по вектору здесь
точный; векторы текстов вычисляются один раз и кешируются.
"""

import numpy as np

from spdo.analytics.quality import Corpus, Ranker
from spdo.search.base import TicketQuery
from spdo.search.embeddings import Encoder
from spdo.search.hybrid import HybridWeights, combine, same


class CachedEncoder:
    """Кодировщик с кешем векторов по тексту.

    Текст запроса при оценке совпадает с текстом обращения корпуса, поэтому
    вектор вычисляется один раз.
    """

    def __init__(self, encoder: Encoder) -> None:
        """Оборачивает кодировщик.

        Args:
            encoder: Кодировщик модели.
        """
        self._encoder = encoder
        self._cache: dict[str, np.ndarray] = {}

    def encode_many(self, texts: list[str]) -> np.ndarray:
        """Вычисляет векторы пакетом, запоминая их.

        Args:
            texts: Тексты.

        Returns:
            Матрица векторов в порядке текстов.
        """
        todo = [t for t in dict.fromkeys(texts) if t not in self._cache]
        if todo:
            self._cache.update(zip(todo, self._encoder.encode(todo), strict=True))
        return np.stack([self._cache[t] for t in texts])

    def encode_one(self, text: str) -> np.ndarray:
        """Возвращает вектор одного текста.

        Args:
            text: Текст.

        Returns:
            Вектор.
        """
        return self.encode_many([text])[0]


class HybridCandidates:
    """Кандидаты гибридного режима с составляющими сходства для запросов набора.

    Отбор повторяет приложение: объединение ``pool`` ближайших по вектору и
    ``pool`` ближайших лексически. Составляющие не зависят от весов, поэтому
    вычисляются один раз на запрос и переиспользуются при подборе весов.
    Поиск ближайших здесь точный, в приложении — приближённый (HNSW).
    """

    def __init__(self, corpus: Corpus, encoder: CachedEncoder, pool: int) -> None:
        """Вычисляет векторы корпуса.

        Args:
            corpus: Проиндексированный набор.
            encoder: Кодировщик с кешем.
            pool: Размер каждого списка кандидатов.
        """
        self._corpus = corpus
        self._encoder = encoder
        self._pool = pool
        self._docs = corpus.index.documents()
        self._matrix = encoder.encode_many([d.text for d in self._docs])
        self._ids = np.asarray([d.ticket_id for d in self._docs])
        self._cache: dict[tuple[str, frozenset[int]], list[tuple[int, dict[str, float]]]] = {}

    def parts(self, query: TicketQuery) -> list[tuple[int, dict[str, float]]]:
        """Возвращает кандидатов запроса и их составляющие сходства.

        Args:
            query: Запрос.

        Returns:
            Пары (номер обращения, составляющие ``sem``, ``lex``, ``cat``, ``comp``).
        """
        key = (query.text, query.exclude_ids)
        if key not in self._cache:
            self._cache[key] = self._compute(query)
        return self._cache[key]

    def _compute(self, query: TicketQuery) -> list[tuple[int, dict[str, float]]]:
        """Отбирает кандидатов и вычисляет их составляющие."""
        sem = self._matrix @ self._encoder.encode_one(query.text)
        allowed = np.asarray([tid not in query.exclude_ids for tid in self._ids])
        lex_map = self._corpus.index.scores(query.text)
        lex = np.asarray([lex_map.get(int(t), 0.0) for t in self._ids])
        rows = set(np.argsort(-np.where(allowed, sem, -np.inf))[: self._pool].tolist())
        rows |= set(np.argsort(-np.where(allowed, lex, -np.inf))[: self._pool].tolist())
        return [
            (
                self._docs[r].ticket_id,
                {
                    "sem": max(float(sem[r]), 0.0),
                    "lex": float(lex[r]),
                    "cat": same(query.category_id, self._docs[r].category_id),
                    "comp": same(query.component_id, self._docs[r].component_id),
                },
            )
            for r in rows
        ]


def hybrid_ranker(candidates: HybridCandidates, weights: HybridWeights) -> Ranker:
    """Возвращает гибридное ранжирование по кандидатам и весам.

    S вычисляется функцией :func:`spdo.search.hybrid.combine`, как в
    приложении.

    Args:
        candidates: Кандидаты с составляющими.
        weights: Веса w1–w4.

    Returns:
        Функция ранжирования.
    """

    def rank(query: TicketQuery) -> list[tuple[int, float]]:
        """Ранжирует кандидатов по гибридному S."""
        scored = [(tid, combine(weights, **p)) for tid, p in candidates.parts(query)]
        return sorted(scored, key=lambda item: -item[1])

    return rank
