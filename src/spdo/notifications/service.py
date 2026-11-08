"""Уведомления авторов дубликатов о решении основного обращения (Ф18).

Уведомление создаётся в транзакции публикации решения или установления
связи. Номер и содержание основного обращения в текст не включаются: оно
может быть чужим конфиденциальным. Решение основного обращения автор
дубликата видит в карточке своего обращения, если основное ему доступно
для ссылки.
"""

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from spdo.notifications.models import Notification
from spdo.relations import service as relations
from spdo.tickets import service as tickets
from spdo.tickets.access import Actor

#: Наибольшее число уведомлений на странице.
PAGE_LIMIT = 50


def _notify(session: Session, actor: Actor, ticket_ids: list[int], text: str) -> int:
    """Создаёт уведомления авторам обращений, кроме самого исполнителя.

    Returns:
        Число созданных уведомлений.
    """
    created = 0
    for ticket_id, author_id in tickets.author_ids(session, ticket_ids).items():
        if author_id != actor.id:
            session.add(
                Notification(user_id=author_id, ticket_id=ticket_id, text=text.format(ticket_id))
            )
            created += 1
    session.flush()
    return created


def solution_published(session: Session, actor: Actor, main_id: int) -> int:
    """Уведомляет авторов дубликатов о решении основного обращения.

    Args:
        session: Сессия БД.
        actor: Оператор, опубликовавший решение.
        main_id: Основное обращение.

    Returns:
        Число созданных уведомлений.
    """
    return _notify(
        session,
        actor,
        relations.duplicates_of(session, main_id),
        "Опубликовано решение основного обращения для вашего обращения № {}.",
    )


def linked_to_resolved(session: Session, actor: Actor, duplicate_id: int, main_id: int) -> int:
    """Уведомляет автора нового дубликата, если у основного уже есть решение.

    Args:
        session: Сессия БД.
        actor: Пользователь, установивший связь.
        duplicate_id: Обращение-дубликат.
        main_id: Основное обращение группы.

    Returns:
        Число созданных уведомлений (0 или 1).
    """
    if not tickets.has_solution(session, main_id):
        return 0
    return _notify(
        session,
        actor,
        [duplicate_id],
        "Ваше обращение № {} закрыто как дубликат обращения, для которого уже есть решение.",
    )


def unread_count(session: Session, user_id: int) -> int:
    """Число непрочитанных уведомлений пользователя.

    Args:
        session: Сессия БД.
        user_id: Пользователь.

    Returns:
        Число уведомлений.
    """
    stmt = select(func.count()).where(
        Notification.user_id == user_id, Notification.read_at.is_(None)
    )
    return session.scalar(stmt) or 0


def recent(session: Session, user_id: int) -> list[Notification]:
    """Последние уведомления пользователя, начиная с новых.

    Args:
        session: Сессия БД.
        user_id: Пользователь.

    Returns:
        Не больше :data:`PAGE_LIMIT` уведомлений.
    """
    stmt = (
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.id.desc())
        .limit(PAGE_LIMIT)
    )
    return list(session.scalars(stmt))


def mark_all_read(session: Session, user_id: int) -> int:
    """Отмечает все уведомления пользователя прочитанными.

    Args:
        session: Сессия БД.
        user_id: Пользователь.

    Returns:
        Число отмеченных уведомлений.
    """
    result = session.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=func.now())
    )
    return result.rowcount
