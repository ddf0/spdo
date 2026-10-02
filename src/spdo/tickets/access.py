"""Субъект операций, ошибки и правила доступа модуля обращений.

Сервисные функции принимают «действующее лицо» — любой объект с
идентификатором и ролью (на практике — пользователь из модуля
``users``). Права проверяются здесь, на сервере, в каждой операции.
"""

from typing import Protocol

from spdo.tickets.models import Ticket
from spdo.users.models import Role


class Actor(Protocol):
    """Пользователь, выполняющий операцию."""

    @property
    def id(self) -> int:
        """Идентификатор пользователя."""
        ...

    @property
    def role(self) -> Role:
        """Роль пользователя."""
        ...


class TicketError(ValueError):
    """Базовая ошибка операций с обращениями."""


class TicketNotFoundError(TicketError):
    """Обращение не найдено или недоступно пользователю.

    Для чужого обращения выдаётся та же ошибка, что и для
    несуществующего, чтобы не раскрывать факт его существования.
    """


class PermissionDeniedError(TicketError):
    """Роль пользователя не допускает операцию."""


class TicketValidationError(TicketError):
    """Данные обращения не прошли проверку."""


def is_staff(actor: Actor) -> bool:
    """Проверяет, является ли пользователь оператором или администратором.

    Args:
        actor: Пользователь.

    Returns:
        ``True`` для оператора и администратора.
    """
    return actor.role in (Role.OPERATOR, Role.ADMIN)


def can_view(actor: Actor, ticket: Ticket) -> bool:
    """Проверяет право на полный просмотр обращения.

    Пользователь видит только свои обращения; чужие ему доступны лишь в
    рекомендациях в сокращённом виде (модуль ``recommendations``).

    Args:
        actor: Пользователь.
        ticket: Обращение.

    Returns:
        ``True``, если обращение можно показать полностью.
    """
    return is_staff(actor) or ticket.author_id == actor.id


def can_reference(actor: Actor, ticket: Ticket) -> bool:
    """Проверяет право сослаться на обращение — сделать его целью связи.

    Ссылаться можно на свои обращения, на любые — оператору, а на чужие
    неконфиденциальные — любому пользователю: такие обращения он видит в
    рекомендациях.

    Args:
        actor: Пользователь.
        ticket: Обращение — цель связи.

    Returns:
        ``True``, если связь с обращением допустима.
    """
    return can_view(actor, ticket) or not ticket.confidential


def require_staff(actor: Actor) -> None:
    """Допускает только оператора или администратора.

    Args:
        actor: Пользователь.

    Raises:
        PermissionDeniedError: Роль пользователя — «пользователь».
    """
    if not is_staff(actor):
        raise PermissionDeniedError("Операция доступна оператору или администратору")


def require_admin(actor: Actor) -> None:
    """Допускает только администратора.

    Args:
        actor: Пользователь.

    Raises:
        PermissionDeniedError: Пользователь не администратор.
    """
    if actor.role is not Role.ADMIN:
        raise PermissionDeniedError("Операция доступна администратору")
