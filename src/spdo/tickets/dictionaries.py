"""Справочники категорий и компонентов.

Читать справочники может любой пользователь (они нужны в форме
регистрации), изменять — только администратор.
"""

from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from spdo.tickets.access import Actor, TicketNotFoundError, TicketValidationError, require_admin
from spdo.tickets.models import Category, Component

#: Наибольшая длина наименования элемента справочника.
NAME_MAX = 100
#: Код ошибки PostgreSQL «нарушение уникальности».
UNIQUE_VIOLATION = "23505"


def _clean_name(name: str) -> str:
    """Проверяет наименование элемента справочника.

    Raises:
        TicketValidationError: Наименование пусто или длиннее 100 символов.
    """
    value = name.strip()
    if not value or len(value) > NAME_MAX:
        raise TicketValidationError(f"Наименование: от 1 до {NAME_MAX} символов")
    return value


def _save_unique(session: Session, name: str, change: Callable[[], None]) -> None:
    """Выполняет изменение в точке сохранения, ловя нарушение уникальности.

    Изменение выполняется внутри точки сохранения: при конфликте
    откатывается только оно, а не вся транзакция, и объект возвращается
    к прежним значениям.

    Args:
        session: Сессия БД.
        name: Наименование для сообщения об ошибке.
        change: Действие, добавляющее или изменяющее элемент.

    Raises:
        TicketValidationError: Элемент с таким наименованием уже есть.
    """
    session.flush()  # чужие отложенные изменения не должны попасть в точку сохранения
    try:
        with session.begin_nested():
            change()
    except IntegrityError as exc:
        if getattr(exc.orig, "sqlstate", None) != UNIQUE_VIOLATION:
            raise
        raise TicketValidationError(f"«{name}» уже есть в справочнике") from exc


def _check_name_free(
    session: Session, model: type[Category | Component], name: str, own_id: int | None = None
) -> None:
    """Проверяет, что наименование не занято без учёта регистра.

    Ограничение уникальности в БД различает регистр, поэтому «Сеть» и
    «сеть» отсекаются здесь.

    Raises:
        TicketValidationError: Наименование уже занято другим элементом.
    """
    stmt = select(model.id).where(func.lower(model.name) == name.lower())
    if own_id is not None:
        stmt = stmt.where(model.id != own_id)
    if session.scalar(stmt) is not None:
        raise TicketValidationError(f"«{name}» уже есть в справочнике")


def list_categories(session: Session) -> list[Category]:
    """Возвращает категории по алфавиту.

    Args:
        session: Сессия БД.

    Returns:
        Категории.
    """
    return list(session.scalars(select(Category).order_by(func.lower(Category.name))))


def list_components(session: Session) -> list[Component]:
    """Возвращает компоненты по алфавиту.

    Args:
        session: Сессия БД.

    Returns:
        Компоненты.
    """
    return list(session.scalars(select(Component).order_by(func.lower(Component.name))))


def create_category(session: Session, actor: Actor, name: str, description: str = "") -> Category:
    """Добавляет категорию.

    Args:
        session: Сессия БД.
        actor: Администратор.
        name: Наименование.
        description: Пояснение для пользователей.

    Returns:
        Созданная категория.

    Raises:
        PermissionDeniedError: Пользователь не администратор.
        TicketValidationError: Наименование неверно или уже занято.
    """
    require_admin(actor)
    category = Category(name=_clean_name(name), description=description.strip())
    _check_name_free(session, Category, category.name)
    _save_unique(session, category.name, lambda: session.add(category))
    return category


def create_component(session: Session, actor: Actor, name: str) -> Component:
    """Добавляет компонент.

    Args:
        session: Сессия БД.
        actor: Администратор.
        name: Наименование.

    Returns:
        Созданный компонент.

    Raises:
        PermissionDeniedError: Пользователь не администратор.
        TicketValidationError: Наименование неверно или уже занято.
    """
    require_admin(actor)
    component = Component(name=_clean_name(name))
    _check_name_free(session, Component, component.name)
    _save_unique(session, component.name, lambda: session.add(component))
    return component


def rename_category(
    session: Session, actor: Actor, category_id: int, name: str, description: str | None = None
) -> Category:
    """Переименовывает категорию.

    Args:
        session: Сессия БД.
        actor: Администратор.
        category_id: Идентификатор категории.
        name: Новое наименование.
        description: Новое пояснение или ``None`` — не менять.

    Returns:
        Изменённая категория.

    Raises:
        PermissionDeniedError: Пользователь не администратор.
        TicketNotFoundError: Категория не найдена.
        TicketValidationError: Наименование неверно или уже занято.
    """
    require_admin(actor)
    category = session.get(Category, category_id)
    if category is None:
        raise TicketNotFoundError(f"Категория {category_id} не найдена")
    new_name = _clean_name(name)
    _check_name_free(session, Category, new_name, category.id)

    def change() -> None:
        category.name = new_name
        if description is not None:
            category.description = description.strip()

    _save_unique(session, new_name, change)
    return category


def rename_component(session: Session, actor: Actor, component_id: int, name: str) -> Component:
    """Переименовывает компонент.

    Args:
        session: Сессия БД.
        actor: Администратор.
        component_id: Идентификатор компонента.
        name: Новое наименование.

    Returns:
        Изменённый компонент.

    Raises:
        PermissionDeniedError: Пользователь не администратор.
        TicketNotFoundError: Компонент не найден.
        TicketValidationError: Наименование неверно или уже занято.
    """
    require_admin(actor)
    component = session.get(Component, component_id)
    if component is None:
        raise TicketNotFoundError(f"Компонент {component_id} не найден")
    new_name = _clean_name(name)
    _check_name_free(session, Component, new_name, component.id)

    def change() -> None:
        component.name = new_name

    _save_unique(session, new_name, change)
    return component
