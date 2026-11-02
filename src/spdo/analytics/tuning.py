"""Подбор весов и порогов на настроечной части эталонного набора.

Веса выбираются по наибольшему recall@3 ранжирования на сетке (при
равенстве — первые в сетке). Затем пороги по парам (S, метка) из первых
:data:`TUNE_DEPTH` позиций выдачи всех запросов части: P2 — по наибольшему
F1 решения «показать» (дубликат или близкое), P1 — по наибольшему F1
решения «дубликат» при условии P1 > P2. Это приближение: в работе решение
принимается по каждой позиции выдачи, а не по совокупности пар.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TypeVar

from spdo.analytics.candidates import CachedEncoder, HybridCandidates, hybrid_ranker
from spdo.analytics.dataset import Dataset
from spdo.analytics.quality import (
    Corpus,
    Ranker,
    build_corpus,
    duplicates_map,
    evaluate,
    lexical_ranker,
    related_map,
)
from spdo.search.hybrid import HybridWeights
from spdo.search.lexical_index import LexicalWeights

#: Тип весов режима (лексического или гибридного).
W = TypeVar("W", LexicalWeights, HybridWeights)

#: Сетка порогов при подборе.
THRESHOLD_GRID = [round(0.05 + 0.01 * i, 2) for i in range(91)]
#: Сетка доли лексического веса; остаток делится поровну между категорией и компонентом.
LEXICAL_SHARE_GRID = (0.6, 0.7, 0.8, 0.9, 1.0)
#: Сетка весов w1 и w2 при подборе гибридного режима; w3 = w4 = остаток пополам.
SEMANTIC_GRID = (0.2, 0.3, 0.4, 0.5, 0.55, 0.6, 0.7, 0.8, 0.9, 1.0)
LEXICAL_GRID = (0.0, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8)
#: Глубина выдачи, по которой собираются пары (S, метка) для подбора порогов.
TUNE_DEPTH = 10


@dataclass(frozen=True, slots=True)
class Tuning:
    """Подобранные на настроечной части параметры лексического режима.

    Attributes:
        weights: Веса составляющих S.
        threshold_duplicate: Порог P1.
        threshold_related: Порог P2.
        f1_duplicate: F1 классификации «дубликат» при P1.
        f1_shown: F1 решения «показать» при P2.
    """

    weights: LexicalWeights
    threshold_duplicate: float
    threshold_related: float
    f1_duplicate: float
    f1_shown: float


@dataclass(frozen=True, slots=True)
class HybridTuning:
    """Подобранные на настроечной части параметры гибридного режима.

    Attributes:
        weights: Веса w1–w4.
        threshold_duplicate: Порог P1.
        threshold_related: Порог P2.
        f1_duplicate: F1 решения «дубликат» при P1.
        f1_shown: F1 решения «показать» при P2.
    """

    weights: HybridWeights
    threshold_duplicate: float
    threshold_related: float
    f1_duplicate: float
    f1_shown: float


def labelled_scores(corpus: Corpus, split: str, rank: Ranker) -> list[tuple[float, str]]:
    """Собирает пары (S, метка) по первым позициям выдачи всех запросов части.

    Метка: ``duplicate``, ``related`` или ``other``. Запросами служат все
    обращения части, включая независимые: так учитываются ложные срабатывания.

    Args:
        corpus: Проиндексированный набор.
        split: Часть набора.
        rank: Функция ранжирования.

    Returns:
        Пары (S, метка).
    """
    dups, rels = duplicates_map(corpus.data), related_map(corpus.data)
    pairs = []
    for t in (t for t in corpus.data.tickets if t["split"] == split):
        d = {corpus.ids(x) for x in dups.get(t["id"], ())}
        r = {corpus.ids(x) for x in rels.get(t["id"], ())}
        for tid, score in rank(corpus.query(t))[:TUNE_DEPTH]:
            pairs.append((score, "duplicate" if tid in d else "related" if tid in r else "other"))
    return pairs


def _f1(pairs: list[tuple[float, bool]], threshold: float, positives: int) -> float:
    """F1 решения «S ≥ порог»; 0, если верных срабатываний нет."""
    tp = sum(label for s, label in pairs if s >= threshold)
    if not tp:
        return 0.0
    precision, recall = tp / sum(s >= threshold for s, _ in pairs), tp / positives
    return 2 * precision * recall / (precision + recall)


def best_threshold(pairs: list[tuple[float, bool]], low: float = 0.0) -> tuple[float, float]:
    """Находит порог с наибольшим F1 для бинарного решения «S ≥ порог».

    Args:
        pairs: Пары (S, истинная метка).
        low: Нижняя граница порога (порог строго больше).

    Returns:
        Пара (порог, F1); при равных F1 — меньший порог.
    """
    positives = sum(label for _, label in pairs)
    best = (THRESHOLD_GRID[-1], 0.0)
    for t in (t for t in THRESHOLD_GRID if t > low):
        f1 = _f1(pairs, t, positives)
        if f1 > best[1]:
            best = (t, f1)
    return best


def _thresholds(pairs: list[tuple[float, str]]) -> tuple[float, float, float, float]:
    """Подбирает P1 > P2 по парам (S, метка).

    Returns:
        (P1, P2, F1 «дубликат», F1 «показать»).
    """
    p2, f1_shown = best_threshold([(s, lbl != "other") for s, lbl in pairs])
    p1, f1_dup = best_threshold([(s, lbl == "duplicate") for s, lbl in pairs], low=p2)
    if p1 <= p2:  # P2 у края сетки: сохранить инвариант P1 > P2
        p2 = round(p1 - 0.01, 2)
    return p1, p2, f1_dup, f1_shown


def _best_weights(
    corpus: Corpus, split: str, grid: Iterable[W], ranker: Callable[[W], Ranker]
) -> W:
    """Веса сетки с наибольшим recall@3 ранжирования; при равенстве — первые."""
    best: tuple[float, W] | None = None
    for weights in grid:
        recall = evaluate(corpus, split, ranker(weights), (1.0, 1.0)).recall_at_3
        if best is None or recall > best[0] + 1e-9:
            best = (recall, weights)
    if best is None:
        raise ValueError("Пустая сетка весов")
    return best[1]


def _lexical_grid() -> Iterable[LexicalWeights]:
    """Сетка весов лексического режима: доля лексики, остаток пополам."""
    for share in LEXICAL_SHARE_GRID:
        rest = round((1 - share) / 2, 4)
        yield LexicalWeights(share, rest, rest)


def _hybrid_grid() -> Iterable[HybridWeights]:
    """Сетка весов гибридного режима с w1 + w2 ≤ 1, w3 = w4 = остаток пополам."""
    for w1 in SEMANTIC_GRID:
        for w2 in (w for w in LEXICAL_GRID if w1 + w <= 1 + 1e-9):
            rest = round((1 - w1 - w2) / 2, 4)
            yield HybridWeights(w1, w2, rest, rest)


def tune_lexical(data: Dataset, pool: int, split: str = "tune") -> Tuning:
    """Подбирает веса и пороги лексического режима на части набора.

    Args:
        data: Набор.
        pool: Размер пула кандидатов.
        split: Часть набора для подбора.

    Returns:
        Подобранные параметры.
    """
    corpus = build_corpus(data)

    def ranker(w: LexicalWeights) -> Ranker:
        """Лексическое ранжирование с весами ``w``."""
        return lexical_ranker(corpus, w, pool)

    weights = _best_weights(corpus, split, _lexical_grid(), ranker)
    p1, p2, f1_dup, f1_shown = _thresholds(labelled_scores(corpus, split, ranker(weights)))
    return Tuning(weights, p1, p2, f1_dup, f1_shown)


def tune_hybrid(
    corpus: Corpus, encoder: CachedEncoder, pool: int, split: str = "tune"
) -> HybridTuning:
    """Подбирает веса w1–w4 и пороги гибридного режима на части набора.

    Веса — на сетке :data:`SEMANTIC_GRID` × :data:`LEXICAL_GRID`
    (w3 = w4 = остаток пополам); пороги — как в :func:`tune_lexical`.

    Args:
        corpus: Проиндексированный набор.
        encoder: Кодировщик с кешем.
        pool: Размер списков кандидатов.
        split: Часть набора для подбора.

    Returns:
        Подобранные параметры.
    """
    candidates = HybridCandidates(corpus, encoder, pool)

    def ranker(w: HybridWeights) -> Ranker:
        """Гибридное ранжирование с весами ``w``."""
        return hybrid_ranker(candidates, w)

    weights = _best_weights(corpus, split, _hybrid_grid(), ranker)
    p1, p2, f1_dup, f1_shown = _thresholds(labelled_scores(corpus, split, ranker(weights)))
    return HybridTuning(weights, p1, p2, f1_dup, f1_shown)
