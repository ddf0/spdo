"""Офлайн-оценка качества подбора на эталонном наборе (ТЗ, п. 8.2).

Для каждого обращения выбранной части, у которого есть дубликаты по
эталонной разметке, формируется запрос к поиску; корпус — все обращения
набора, кроме самого запроса. По выдаче вычисляются:

* recall@3 — доля истинных дубликатов среди первых трёх позиций
  ранжирования, делённая на ``min(число дубликатов, 3)``: при группах из
  4–6 формулировок в три позиции не поместятся все, и без этой поправки
  максимум был бы меньше единицы;
* recall@3 показанных — то же, но среди кандидатов с S ≥ P2, которых
  пользователь действительно увидит;
* hit@3 — доля запросов, у которых хотя бы один дубликат в первых трёх;
* MRR — средняя обратная позиция первого дубликата;
* доля ложных «дубликатов» — доля всех запросов части (включая
  независимые обращения), у которых на первой позиции с S ≥ P1 стоит
  обращение, не являющееся дубликатом; тематически близкое на этой позиции
  тоже считается ложным «дубликатом» (строгое прочтение).

Показатели recall, hit и MRR считаются по запросам, у которых есть
дубликаты; долю ложных «дубликатов» — по всем запросам части.
"""

from collections.abc import Callable
from dataclasses import asdict, dataclass

from spdo.analytics.dataset import Dataset
from spdo.search.base import TicketQuery
from spdo.search.lexical import Document, LexicalIndex, LexicalWeights

#: Функция ранжирования: запрос → пары (номер обращения, S) по убыванию S.
Ranker = Callable[[TicketQuery], list[tuple[int, float]]]
#: Глубина, на которой считается recall.
K = 3


@dataclass(frozen=True, slots=True)
class Quality:
    """Показатели качества подбора на части набора.

    Attributes:
        queries: Число запросов (обращений с дубликатами).
        recall_at_3: recall@3 по ранжированию.
        recall_at_3_shown: recall@3 среди показанных (S ≥ P2).
        hit_at_3: Доля запросов с дубликатом в первых трёх.
        mrr: Средняя обратная позиция первого дубликата.
        false_duplicate_rate: Доля запросов с ложным «дубликатом» на
            первой позиции.
    """

    queries: int
    recall_at_3: float
    recall_at_3_shown: float
    hit_at_3: float
    mrr: float
    false_duplicate_rate: float

    def as_dict(self) -> dict[str, float]:
        """Возвращает показатели словарём (для JSON-отчёта)."""
        return asdict(self)


class IdMap:
    """Числовые идентификаторы для строковых ключей.

    Поиск работает с числовыми номерами обращений и справочников, а набор —
    со строковыми; отображение выдаёт номера по порядку первого обращения.
    """

    def __init__(self) -> None:
        """Создаёт пустое отображение."""
        self._map: dict[str, int] = {}

    def __call__(self, key: str) -> int:
        """Возвращает номер ключа, назначая новый при первом обращении.

        Args:
            key: Строковый ключ.

        Returns:
            Числовой номер.
        """
        return self._map.setdefault(key, len(self._map) + 1)


@dataclass(frozen=True, slots=True)
class Corpus:
    """Набор, проиндексированный для офлайн-оценки.

    Attributes:
        data: Набор.
        index: Лексический индекс всех обращений набора.
        ids: Номера обращений.
        dicts: Номера значений справочников (``cat:…``, ``comp:…``).
    """

    data: Dataset
    index: LexicalIndex
    ids: IdMap
    dicts: IdMap

    def query(self, ticket: dict[str, str]) -> TicketQuery:
        """Формирует запрос по обращению набора, исключая его самого.

        Args:
            ticket: Строка ``tickets.csv``.

        Returns:
            Запрос к поиску.
        """
        return TicketQuery(
            title=ticket["title"],
            description=ticket["description"],
            category_id=self.dicts("cat:" + ticket["category"]),
            component_id=self.dicts("comp:" + ticket["component"]),
            exclude_ids=frozenset({self.ids(ticket["id"])}),
        )


def duplicates_map(data: Dataset) -> dict[str, set[str]]:
    """Возвращает для каждого обращения множество его истинных дубликатов.

    Args:
        data: Набор.

    Returns:
        Номер обращения → номера других обращений его групп ``duplicate``.
    """
    groups: dict[str, set[str]] = {}
    for g in data.groups:
        if g["kind"] == "duplicate":
            groups.setdefault(g["group_id"], set()).add(g["ticket_id"])
    result: dict[str, set[str]] = {}
    for members in groups.values():
        for tid in members:
            result.setdefault(tid, set()).update(members - {tid})
    return result


def build_corpus(data: Dataset) -> Corpus:
    """Строит лексический индекс по всем обращениям набора.

    Индекс не зависит от весов, поэтому строится один раз на набор.

    Args:
        data: Набор.

    Returns:
        Проиндексированный набор.
    """
    ids, dicts = IdMap(), IdMap()
    docs = [
        Document(
            ids(t["id"]),
            f"{t['title']}\n{t['description']}",
            dicts("cat:" + t["category"]),
            dicts("comp:" + t["component"]),
            author_id=0,
            confidential=False,
        )
        for t in data.tickets
    ]
    return Corpus(data, LexicalIndex.build(docs), ids, dicts)


def lexical_ranker(corpus: Corpus, weights: LexicalWeights, pool: int) -> Ranker:
    """Возвращает ранжирование лексическим индексом набора.

    Args:
        corpus: Проиндексированный набор.
        weights: Веса составляющих S.
        pool: Размер пула кандидатов.

    Returns:
        Функция ранжирования.
    """

    def rank(query: TicketQuery) -> list[tuple[int, float]]:
        """Ранжирует кандидатов лексическим индексом."""
        found = corpus.index.search(query, weights, pool)
        return [(d.ticket_id, parts["score"]) for d, parts in found]

    return rank


def evaluate(corpus: Corpus, split: str, rank: Ranker, thresholds: tuple[float, float]) -> Quality:
    """Измеряет качество подбора на части набора.

    Args:
        corpus: Проиндексированный набор.
        split: Часть набора: ``holdout`` или ``tune``.
        rank: Функция ранжирования.
        thresholds: Пороги (P1, P2).

    Returns:
        Показатели качества.
    """
    p1, p2 = thresholds
    dups = duplicates_map(corpus.data)
    totals = {"recall": 0.0, "shown": 0.0, "hit": 0.0, "rr": 0.0, "false": 0.0}
    part = [t for t in corpus.data.tickets if t["split"] == split]
    with_dups = 0
    for t in part:
        ranked = rank(corpus.query(t))
        relevant = {corpus.ids(d) for d in dups.get(t["id"], ())}
        if ranked and ranked[0][1] >= p1 and ranked[0][0] not in relevant:
            totals["false"] += 1
        if relevant:
            with_dups += 1
            _accumulate(totals, ranked, relevant, p2)
    n = with_dups or 1
    return Quality(
        queries=with_dups,
        recall_at_3=totals["recall"] / n,
        recall_at_3_shown=totals["shown"] / n,
        hit_at_3=totals["hit"] / n,
        mrr=totals["rr"] / n,
        false_duplicate_rate=totals["false"] / (len(part) or 1),
    )


def _accumulate(
    totals: dict[str, float], ranked: list[tuple[int, float]], relevant: set[int], p2: float
) -> None:
    """Добавляет вклад одного запроса с дубликатами в суммы показателей."""
    denominator = min(len(relevant), K)
    top = ranked[:K]
    found = sum(tid in relevant for tid, _ in top)
    totals["recall"] += found / denominator
    totals["shown"] += sum(tid in relevant for tid, s in top if s >= p2) / denominator
    totals["hit"] += float(found > 0)
    first = next((pos for pos, (tid, _) in enumerate(ranked, start=1) if tid in relevant), None)
    totals["rr"] += 1 / first if first else 0.0


@dataclass(frozen=True, slots=True)
class Tuning:
    """Подобранные на настроечной части параметры режима.

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


#: Сетка порогов при подборе.
THRESHOLD_GRID = [round(0.05 + 0.01 * i, 2) for i in range(91)]
#: Сетка доли лексического веса; остаток делится поровну между категорией и компонентом.
LEXICAL_SHARE_GRID = (0.6, 0.7, 0.8, 0.9, 1.0)
#: Глубина выдачи, по которой собираются пары (S, метка) для подбора порогов.
TUNE_DEPTH = 10


def related_map(data: Dataset) -> dict[str, set[str]]:
    """Возвращает для каждого обращения множество тематически близких.

    Args:
        data: Набор.

    Returns:
        Номер обращения → номера других обращений его групп ``related``.
    """
    groups: dict[str, set[str]] = {}
    for g in data.groups:
        if g["kind"] == "related":
            groups.setdefault(g["group_id"], set()).add(g["ticket_id"])
    result: dict[str, set[str]] = {}
    for members in groups.values():
        for tid in members:
            result.setdefault(tid, set()).update(members - {tid})
    return result


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


def best_threshold(pairs: list[tuple[float, bool]], low: float = 0.0) -> tuple[float, float]:
    """Находит порог с наибольшим F1 для бинарного решения «S ≥ порог».

    Args:
        pairs: Пары (S, истинная метка).
        low: Нижняя граница порога (порог строго больше).

    Returns:
        Пара (порог, F1).
    """
    positives = sum(label for _, label in pairs)
    best = (THRESHOLD_GRID[-1], 0.0)
    for t in (t for t in THRESHOLD_GRID if t > low):
        tp = sum(label for s, label in pairs if s >= t)
        predicted = sum(s >= t for s, _ in pairs)
        if not tp:
            continue
        precision, recall = tp / predicted, tp / positives
        f1 = 2 * precision * recall / (precision + recall)
        if f1 > best[1]:
            best = (t, f1)
    return best


def tune_lexical(data: Dataset, pool: int, split: str = "tune") -> Tuning:
    """Подбирает веса и пороги лексического режима на части набора.

    Веса выбираются по наибольшему recall@3 ранжирования (при равенстве —
    меньшая доля лексики, первая в сетке). Затем пороги по парам (S, метка)
    из первых :data:`TUNE_DEPTH` позиций выдачи всех запросов части:
    P2 — по наибольшему F1 решения «показать» (дубликат или близкое),
    P1 — по наибольшему F1 решения «дубликат» при условии P1 > P2. Это
    приближение: в работе решение принимается по каждой позиции выдачи,
    а не по совокупности пар.

    Args:
        data: Набор.
        pool: Размер пула кандидатов.
        split: Часть набора для подбора.

    Returns:
        Подобранные параметры.
    """
    corpus = build_corpus(data)
    best: tuple[float, LexicalWeights] | None = None
    for share in LEXICAL_SHARE_GRID:
        rest = round((1 - share) / 2, 4)
        weights = LexicalWeights(share, rest, rest)
        recall = evaluate(
            corpus, split, lexical_ranker(corpus, weights, pool), (1.0, 1.0)
        ).recall_at_3
        if best is None or recall > best[0] + 1e-9:
            best = (recall, weights)
    assert best is not None
    pairs = labelled_scores(corpus, split, lexical_ranker(corpus, best[1], pool))
    p2, f1_shown = best_threshold([(s, lbl != "other") for s, lbl in pairs])
    p1, f1_dup = best_threshold([(s, lbl == "duplicate") for s, lbl in pairs], low=p2)
    if p1 <= p2:  # P2 у края сетки: сохранить инвариант P1 > P2
        p2 = round(p1 - 0.01, 2)
    return Tuning(best[1], p1, p2, f1_dup, f1_shown)
