"""Таблица допустимых переходов статусов обращения (ТЗ, п. 4.1.5).

Таблица задана данными, а не ветвлениями кода: её легко сверить с ТЗ, а
любой не перечисленный переход запрещён.
"""

from dataclasses import dataclass
from enum import StrEnum

from spdo.tickets.models import TicketStatus

S = TicketStatus


class Trigger(StrEnum):
    """Событие, которым вызван переход."""

    #: Оператор принял обращение или повторно открыл его.
    OPERATOR = "operator"
    #: Опубликовано решение.
    SOLUTION = "solution"
    #: Установлена связь «дубликат» (модуль ``relations``).
    DUPLICATE_LINK = "duplicate_link"
    #: Автор подтвердил, что решение подошло.
    AUTHOR_CONFIRMATION = "author_confirmation"
    #: Истёк срок подтверждения решения.
    EXPIRY = "expiry"


@dataclass(frozen=True, slots=True)
class Rule:
    """Условие перехода.

    Attributes:
        triggers: События, которыми разрешено выполнить переход.
        comment_required: Обязателен ли комментарий.
    """

    triggers: frozenset[Trigger]
    comment_required: bool = False


def _rule(*triggers: Trigger, comment: bool = False) -> Rule:
    """Создаёт правило перехода."""
    return Rule(frozenset(triggers), comment)


#: Допустимые переходы: (исходный статус, новый статус) → условие.
TRANSITIONS: dict[tuple[TicketStatus, TicketStatus], Rule] = {
    (S.NEW, S.IN_PROGRESS): _rule(Trigger.OPERATOR),
    (S.NEW, S.CLOSED_DUPLICATE): _rule(Trigger.DUPLICATE_LINK),
    (S.IN_PROGRESS, S.CLOSED_DUPLICATE): _rule(Trigger.DUPLICATE_LINK),
    (S.IN_PROGRESS, S.RESOLVED): _rule(Trigger.SOLUTION),
    (S.NEW, S.CLOSED): _rule(Trigger.OPERATOR, comment=True),
    (S.IN_PROGRESS, S.CLOSED): _rule(Trigger.OPERATOR, comment=True),
    (S.RESOLVED, S.CLOSED): _rule(Trigger.AUTHOR_CONFIRMATION, Trigger.EXPIRY),
    (S.RESOLVED, S.IN_PROGRESS): _rule(Trigger.OPERATOR, comment=True),
    (S.CLOSED, S.IN_PROGRESS): _rule(Trigger.OPERATOR, comment=True),
}


class TransitionError(ValueError):
    """Переход статуса запрещён таблицей или не выполнено его условие."""


def check_transition(
    current: TicketStatus, target: TicketStatus, trigger: Trigger, comment: str = ""
) -> None:
    """Проверяет, допустим ли переход.

    Args:
        current: Текущий статус.
        target: Новый статус.
        trigger: Событие, вызвавшее переход.
        comment: Комментарий к переходу.

    Raises:
        TransitionError: Переход отсутствует в таблице, вызван не тем
            событием или не содержит обязательного комментария.
    """
    rule = TRANSITIONS.get((current, target))
    if rule is None:
        raise TransitionError(f"Переход «{current.value}» → «{target.value}» запрещён")
    if trigger not in rule.triggers:
        raise TransitionError(
            f"Переход «{current.value}» → «{target.value}» "
            f"не выполняется событием «{trigger.value}»"
        )
    if rule.comment_required and not comment.strip():
        raise TransitionError("Для этого перехода обязателен комментарий")


def allowed_targets(current: TicketStatus, trigger: Trigger) -> list[TicketStatus]:
    """Возвращает статусы, в которые можно перейти данным событием.

    Нужна интерфейсу, чтобы показывать только допустимые действия.

    Args:
        current: Текущий статус.
        trigger: Событие.

    Returns:
        Допустимые новые статусы в порядке таблицы.
    """
    return [
        to for (frm, to), rule in TRANSITIONS.items() if frm is current and trigger in rule.triggers
    ]
