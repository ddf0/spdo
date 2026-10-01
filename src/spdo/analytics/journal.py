"""Запись в журнал действий пользователей.

Другие модули пишут в журнал только через :func:`record_action`, не
обращаясь к модели напрямую. Транзакция не фиксируется: запись попадает
в БД вместе с основным изменением или не попадает вовсе.
"""

from sqlalchemy.orm import Session

from spdo.analytics.models import ActionKind, ActionLogEntry


def record_action(
    session: Session,
    *,
    user_id: int,
    action: ActionKind,
    ticket_id: int,
    other_ticket_id: int | None = None,
    details: str = "",
) -> ActionLogEntry:
    """Добавляет запись в журнал действий.

    Args:
        session: Сессия БД.
        user_id: Кто выполнил действие.
        action: Вид действия.
        ticket_id: Обращение, над которым выполнено действие.
        other_ticket_id: Второе обращение или ``None``.
        details: Дополнительные сведения.

    Returns:
        Созданная запись журнала.
    """
    entry = ActionLogEntry(
        user_id=user_id,
        action=action,
        ticket_id=ticket_id,
        other_ticket_id=other_ticket_id,
        details=details,
    )
    session.add(entry)
    return entry
