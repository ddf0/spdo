"""Загрузка эталонного набора в базу данных.

Обращения создаются сервисными функциями модуля ``tickets`` от имени
администратора: так проходят те же проверки и та же запись истории, что и
при регистрации через интерфейс. Справочники пополняются недостающими
категориями и компонентами. Эталонная разметка групп в БД не загружается:
это ответы для оценки качества, а не установленные связи.

Соответствие идентификаторов набора номерам обращений в БД сохраняется в
``db_ids.csv`` рядом с набором: оно нужно для оценки recall@3 и защищает от
повторной загрузки. Соответствие пишется во временный файл до фиксации
транзакции и становится постоянным после неё, поэтому прерывание между
фиксацией и записью файла не приводит к повторной загрузке.
"""

import csv
import os
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from spdo.analytics.dataset import Dataset
from spdo.tickets import dictionaries
from spdo.tickets import service as tickets
from spdo.tickets.access import Actor, require_admin
from spdo.tickets.models import Ticket

#: Файл соответствия идентификаторов набора номерам обращений в БД.
DB_IDS_FILE = "db_ids.csv"
#: Временный файл соответствия, записываемый до фиксации транзакции.
STAGED_FILE = "db_ids.csv.tmp"


class AlreadyLoadedError(RuntimeError):
    """Набор уже загружен: найден файл соответствия идентификаторов."""


def _dictionary_ids(
    session: Session, actor: Actor, data: Dataset
) -> tuple[dict[str, int], dict[str, int]]:
    """Создаёт недостающие категории и компоненты, возвращает их номера по имени."""
    categories = {c.name.lower(): c.id for c in dictionaries.list_categories(session)}
    components = {c.name.lower(): c.id for c in dictionaries.list_components(session)}
    for name in sorted({t["category"] for t in data.tickets}):
        if name.lower() not in categories:
            categories[name.lower()] = dictionaries.create_category(session, actor, name).id
    for name in sorted({t["component"] for t in data.tickets}):
        if name.lower() not in components:
            components[name.lower()] = dictionaries.create_component(session, actor, name).id
    return categories, components


def load(session: Session, actor: Actor, data: Dataset, directory: Path) -> dict[str, int]:
    """Загружает обращения набора в БД.

    Порядок для вызывающего кода: :func:`recover` → :func:`load` →
    :func:`stage_mapping` → фиксация транзакции → :func:`finalize_mapping`.

    Args:
        session: Сессия БД.
        actor: Администратор — автор загружаемых обращений.
        data: Набор.
        directory: Каталог набора для файла ``db_ids.csv``.

    Returns:
        Соответствие идентификатора в наборе номеру обращения в БД.

    Raises:
        PermissionDeniedError: Пользователь не администратор.
        AlreadyLoadedError: Набор уже загружен.
        TicketValidationError: Обращение набора не прошло проверку.
    """
    require_admin(actor)
    if (directory / DB_IDS_FILE).exists():
        raise AlreadyLoadedError(f"Набор уже загружен: есть {directory / DB_IDS_FILE}")
    categories, components = _dictionary_ids(session, actor, data)
    mapping: dict[str, int] = {}
    for row in data.tickets:
        ticket = tickets.create_ticket(
            session,
            actor,
            title=row["title"],
            description=row["description"],
            category_id=categories[row["category"].lower()],
            component_id=components[row["component"].lower()],
        )
        mapping[row["id"]] = ticket.id
    return mapping


def _write_mapping(path: Path, mapping: dict[str, int]) -> None:
    """Записывает соответствие идентификаторов в CSV."""
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["dataset_id", "ticket_id"])
        writer.writerows(mapping.items())


def stage_mapping(directory: Path, mapping: dict[str, int]) -> Path:
    """Записывает соответствие во временный файл — до фиксации транзакции.

    Если процесс прервётся после фиксации, но до :func:`finalize_mapping`,
    временный файл позволит :func:`recover` понять, что набор уже загружен.

    Args:
        directory: Каталог набора.
        mapping: Соответствие идентификатора в наборе номеру в БД.

    Returns:
        Путь к временному файлу.
    """
    path = directory / STAGED_FILE
    _write_mapping(path, mapping)
    return path


def finalize_mapping(directory: Path) -> Path:
    """Делает временный файл соответствия постоянным — после фиксации.

    Args:
        directory: Каталог набора.

    Returns:
        Путь к ``db_ids.csv``.
    """
    path = directory / DB_IDS_FILE
    os.replace(directory / STAGED_FILE, path)
    return path


def recover(session: Session, directory: Path) -> None:
    """Разбирает временный файл соответствия, оставшийся от прерванной загрузки.

    Если все обращения из файла есть в БД, загрузка была зафиксирована —
    файл становится постоянным. Иначе транзакция откатилась — файл удаляется.

    Args:
        session: Сессия БД.
        directory: Каталог набора.
    """
    staged = directory / STAGED_FILE
    if not staged.exists():
        return
    with staged.open(encoding="utf-8", newline="") as fh:
        ids = [int(row["ticket_id"]) for row in csv.DictReader(fh)]
    found = session.scalar(select(func.count()).select_from(Ticket).where(Ticket.id.in_(ids)))
    if ids and found == len(ids):
        finalize_mapping(directory)
    else:
        staged.unlink()
