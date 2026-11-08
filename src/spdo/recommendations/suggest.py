"""Предложение категории и компонента нового обращения (Ф19).

Категория и компонент выбираются голосованием похожих обращений: голос
кандидата весит его коэффициент сходства S. Значение предлагается, только
если у победителя не меньше :data:`SUGGEST_SHARE` суммарного веса, —
при разногласии кандидатов предложения нет. Решение принимает
пользователь: предложение лишь заполняет поле по его нажатию.
"""

from collections import defaultdict
from dataclasses import dataclass

#: Наименьшая доля веса победителя голосования для предложения.
#: Значение — допущение разработчиков, в ТЗ не задано.
SUGGEST_SHARE = 0.5


@dataclass(frozen=True, slots=True)
class Suggestion:
    """Предложенные значения справочников.

    Attributes:
        category_id: Категория или ``None`` — предложения нет.
        component_id: Компонент или ``None`` — предложения нет.
    """

    category_id: int | None
    component_id: int | None


def vote(items: list[tuple[float, int]]) -> int | None:
    """Взвешенное голосование.

    Args:
        items: Пары (вес голоса, значение).

    Returns:
        Значение с наибольшим весом, если его доля не меньше
        :data:`SUGGEST_SHARE`, иначе ``None``.
    """
    totals: dict[int, float] = defaultdict(float)
    for weight, value in items:
        totals[value] += max(weight, 0.0)
    whole = sum(totals.values())
    if not whole:
        return None
    best = max(totals, key=lambda value: (totals[value], -value))
    return best if totals[best] / whole >= SUGGEST_SHARE else None


def suggest(scored: list[tuple[float, int, int]]) -> Suggestion:
    """Предлагает категорию и компонент по похожим обращениям.

    Args:
        scored: Тройки (S, категория, компонент) кандидатов подбора.

    Returns:
        Предложение; поля независимы друг от друга.
    """
    return Suggestion(
        vote([(s, cat) for s, cat, _ in scored]),
        vote([(s, comp) for s, _, comp in scored]),
    )
