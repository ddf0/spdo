"""Показатели качества подбора на эталонном наборе (ТЗ, п. 8.2).

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
from spdo.search.lexical_index import Document, LexicalIndex, LexicalWeights

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


def _group_map(data: Dataset, kind: str) -> dict[str, set[str]]:
    """Для каждого обращения — другие обращения его групп вида ``kind``."""
    groups: dict[str, set[str]] = {}
    for g in data.groups:
        if g["kind"] == kind:
            groups.setdefault(g["group_id"], set()).add(g["ticket_id"])
    result: dict[str, set[str]] = {}
    for members in groups.values():
        for tid in members:
            result.setdefault(tid, set()).update(members - {tid})
    return result


def duplicates_map(data: Dataset) -> dict[str, set[str]]:
    """Возвращает для каждого обращения множество его истинных дубликатов.

    Args:
        data: Набор.

    Returns:
        Номер обращения → номера других обращений его групп ``duplicate``.
    """
    return _group_map(data, "duplicate")


def related_map(data: Dataset) -> dict[str, set[str]]:
    """Возвращает для каждого обращения множество тематически близких.

    Args:
        data: Набор.

    Returns:
        Номер обращения → номера других обращений его групп ``related``.
    """
    return _group_map(data, "related")


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
    totals = dict.fromkeys(("recall", "shown", "hit", "rr", "false", "queries"), 0.0)
    part = [t for t in corpus.data.tickets if t["split"] == split]
    for t in part:
        relevant = {corpus.ids(d) for d in dups.get(t["id"], ())}
        ranked = rank(corpus.query(t))
        totals["false"] += _false_duplicate(ranked, relevant, p1)
        if relevant:
            _accumulate(totals, ranked, relevant, p2)
    n = totals["queries"] or 1
    return Quality(
        queries=int(totals["queries"]),
        recall_at_3=totals["recall"] / n,
        recall_at_3_shown=totals["shown"] / n,
        hit_at_3=totals["hit"] / n,
        mrr=totals["rr"] / n,
        false_duplicate_rate=totals["false"] / (len(part) or 1),
    )


def _false_duplicate(ranked: list[tuple[int, float]], relevant: set[int], p1: float) -> float:
    """1, если на первой позиции с S ≥ P1 стоит не дубликат, иначе 0."""
    return float(bool(ranked) and ranked[0][1] >= p1 and ranked[0][0] not in relevant)


def _accumulate(
    totals: dict[str, float], ranked: list[tuple[int, float]], relevant: set[int], p2: float
) -> None:
    """Добавляет вклад одного запроса с дубликатами в суммы показателей."""
    totals["queries"] += 1
    denominator = min(len(relevant), K)
    top = ranked[:K]
    found = sum(tid in relevant for tid, _ in top)
    totals["recall"] += found / denominator
    totals["shown"] += sum(tid in relevant for tid, s in top if s >= p2) / denominator
    totals["hit"] += float(found > 0)
    first = next((pos for pos, (tid, _) in enumerate(ranked, start=1) if tid in relevant), None)
    totals["rr"] += 1 / first if first else 0.0
