"""Формат эталонного набора обращений: чтение, запись, разбиение, контрольная сумма.

Набор — каталог с файлами (описание формата — ``data/synthetic/README.md``):

* ``tickets.csv`` — обращения: ``id, title, description, category,
  component, status, split`` и необязательные дополнительные графы;
* ``groups.csv`` — эталонная разметка: ``ticket_id, group_id, kind``
  (``duplicate`` | ``related``);
* ``checksum.txt`` — SHA-256 проверочной части (``split = holdout``).

Проверочная часть используется только для измерения recall@3 и не меняется
между релизами; контрольная сумма позволяет это проверить.
"""

import csv
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from spdo.tickets.dictionaries import NAME_MAX
from spdo.tickets.models import DESCRIPTION_MAX, TITLE_MAX, TicketStatus

#: Обязательные графы ``tickets.csv``.
TICKET_FIELDS = ("id", "title", "description", "category", "component", "status", "split")
#: Графы ``groups.csv``.
GROUP_FIELDS = ("ticket_id", "group_id", "kind")
#: Допустимые значения графы ``split``.
SPLITS = frozenset({"tune", "holdout"})
#: Допустимые значения графы ``kind``.
KINDS = frozenset({"duplicate", "related"})
#: Допустимые значения графы ``status``.
STATUSES = frozenset(s.value for s in TicketStatus)
#: Предельные длины полей: как при регистрации обращения и в справочниках.
FIELD_LIMITS = {
    "title": TITLE_MAX,
    "description": DESCRIPTION_MAX,
    "category": NAME_MAX,
    "component": NAME_MAX,
}
#: Версия канонической записи для контрольной суммы.
CHECKSUM_FORMAT = "spdo-holdout-v1"


class DatasetError(ValueError):
    """Набор не соответствует формату."""


@dataclass(frozen=True, slots=True)
class Dataset:
    """Эталонный набор в памяти.

    Attributes:
        tickets: Строки ``tickets.csv`` в порядке файла.
        groups: Строки ``groups.csv`` в порядке файла.
    """

    tickets: list[dict[str, str]]
    groups: list[dict[str, str]]


def _read_csv(path: Path, required: tuple[str, ...]) -> list[dict[str, str]]:
    """Читает CSV в кодировке UTF-8 и проверяет наличие обязательных граф."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = set(required) - set(reader.fieldnames or ())
        if missing:
            raise DatasetError(f"{path.name}: нет граф {', '.join(sorted(missing))}")
        return list(reader)


def _write_csv(path: Path, rows: list[dict[str, str]], fields: tuple[str, ...]) -> None:
    """Записывает CSV в UTF-8 без BOM, все поля в кавычках."""
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)


def _check_ticket(t: dict[str, str | None]) -> None:
    """Проверяет обязательные поля обращения: непусты и не длиннее предела."""
    for field in TICKET_FIELDS:
        value = t.get(field)
        if value is None or not value.strip():
            raise DatasetError(f"Обращение {t.get('id')}: пустое поле {field}")
    for field, limit in FIELD_LIMITS.items():
        if len(t[field]) > limit:
            raise DatasetError(f"Обращение {t['id']}: {field} длиннее {limit} символов")
    if t["split"] not in SPLITS:
        raise DatasetError(f"Обращение {t['id']}: split допускает {', '.join(sorted(SPLITS))}")
    if t["status"] not in STATUSES:
        raise DatasetError(f"Обращение {t['id']}: неизвестный статус {t['status']}")


def _check_groups(groups: list[dict[str, str]], split_of: dict[str, str]) -> None:
    """Проверяет группы: состав, единый вид, целостность при разбиении."""
    members: dict[str, set[str]] = {}
    kinds: dict[str, set[str]] = {}
    seen: set[tuple[str, str]] = set()
    for g in groups:
        key = (g.get("group_id") or "", g.get("ticket_id") or "")
        if g.get("kind") not in KINDS or key[1] not in split_of or key in seen:
            raise DatasetError(f"groups.csv: неверная или повторная строка {g}")
        seen.add(key)
        members.setdefault(key[0], set()).add(key[1])
        kinds.setdefault(key[0], set()).add(g["kind"])
    for gid, tids in members.items():
        if len(tids) < 2:
            raise DatasetError(f"Группа {gid} состоит из одного обращения")
        if len(kinds[gid]) > 1:
            raise DatasetError(f"Группа {gid} смешивает виды {', '.join(sorted(kinds[gid]))}")
        if len({split_of[t] for t in tids}) > 1:
            raise DatasetError(f"Группа {gid} разорвана между tune и holdout")


def validate(data: Dataset) -> None:
    """Проверяет набор до любой работы с БД.

    Обращение может входить в несколько групп (например, дубликат одного и
    связанное с другим); группы не объединяются транзитивно.

    Args:
        data: Набор.

    Raises:
        DatasetError: Пустые или слишком длинные поля, неизвестные
            ``split``, ``status`` или ``kind``, повторяющиеся идентификаторы,
            ссылки на отсутствующие обращения, группа из одного обращения,
            со смешанными видами или разорванная разбиением, пустая
            проверочная часть.
    """
    for t in data.tickets:
        _check_ticket(t)
    split_of = {t["id"]: t["split"] for t in data.tickets}
    if len(split_of) != len(data.tickets):
        raise DatasetError("Идентификаторы обращений повторяются")
    if "holdout" not in split_of.values():
        raise DatasetError("Проверочная часть пуста")
    _check_groups(data.groups, split_of)


def read(directory: Path) -> Dataset:
    """Читает и проверяет набор из каталога.

    Args:
        directory: Каталог с ``tickets.csv`` и ``groups.csv``.

    Returns:
        Набор.

    Raises:
        DatasetError: Набор не соответствует формату или файлы не читаются.
    """
    try:
        data = Dataset(
            tickets=_read_csv(directory / "tickets.csv", TICKET_FIELDS),
            groups=_read_csv(directory / "groups.csv", GROUP_FIELDS),
        )
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise DatasetError(f"Набор не читается: {exc}") from exc
    validate(data)
    return data


def holdout_checksum(data: Dataset) -> str:
    """Вычисляет SHA-256 проверочной части.

    Хешируется каноническая JSON-запись: разделы обращений и групп со
    счётчиками строк, строки отсортированы по идентификаторам. Сумма не
    зависит от порядка строк и формы записи CSV и однозначна при любых
    символах в тексте. Учитываются только обязательные графы: дополнительные
    (например, ``translation_review``) в измерении не участвуют и не защищены.

    Args:
        data: Набор.

    Returns:
        Шестнадцатеричная строка SHA-256 в нижнем регистре.
    """
    holdout = {t["id"] for t in data.tickets if t["split"] == "holdout"}
    tickets = sorted([t[f] for f in TICKET_FIELDS] for t in data.tickets if t["id"] in holdout)
    groups = sorted([g[f] for f in GROUP_FIELDS] for g in data.groups if g["ticket_id"] in holdout)
    canonical = {
        "format": CHECKSUM_FORMAT,
        "tickets": {"count": len(tickets), "rows": tickets},
        "groups": {"count": len(groups), "rows": groups},
    }
    payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def stored_checksum(directory: Path) -> str | None:
    """Возвращает зафиксированную контрольную сумму или ``None``.

    Args:
        directory: Каталог набора.

    Returns:
        Содержимое ``checksum.txt`` в нижнем регистре или ``None``.
    """
    path = directory / "checksum.txt"
    return path.read_text(encoding="utf-8").strip().lower() if path.exists() else None


def write(
    directory: Path,
    data: Dataset,
    extra_fields: tuple[str, ...] = (),
    *,
    force: bool = False,
    extra_files: dict[str, str] | None = None,
) -> str:
    """Проверяет и атомарно записывает набор вместе с контрольной суммой.

    Файлы готовятся во временном каталоге и переносятся на место,
    ``checksum.txt`` — последним. Если проверочная часть уже зафиксирована
    и новая сумма с ней расходится, запись отклоняется: проверочную часть
    нельзя незаметно заменить повторным формированием.

    Args:
        directory: Каталог вывода; создаётся при необходимости.
        data: Набор.
        extra_fields: Дополнительные графы ``tickets.csv`` после обязательных.
        force: Перезаписать зафиксированную проверочную часть.
        extra_files: Дополнительные текстовые файлы: имя → содержимое.

    Returns:
        Контрольная сумма проверочной части.

    Raises:
        DatasetError: Набор не соответствует формату или изменил бы
            зафиксированную проверочную часть без ``force``.
    """
    validate(data)
    checksum = holdout_checksum(data)
    stored = stored_checksum(directory)
    if stored is not None and stored != checksum and not force:
        raise DatasetError(
            f"Проверочная часть изменилась бы ({stored[:12]}… → {checksum[:12]}…); "
            "чтобы заменить её осознанно, укажите --force"
        )
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory, prefix=".tmp-") as tmp:
        staging = Path(tmp)
        _write_csv(staging / "tickets.csv", data.tickets, TICKET_FIELDS + extra_fields)
        _write_csv(staging / "groups.csv", data.groups, GROUP_FIELDS)
        for name, text in (extra_files or {}).items():
            (staging / name).write_text(text, encoding="utf-8")
        (staging / "checksum.txt").write_text(checksum + "\n", encoding="utf-8")
        names = ["tickets.csv", "groups.csv", *(extra_files or {}), "checksum.txt"]
        for name in names:
            os.replace(staging / name, directory / name)
    return checksum


def verify(directory: Path) -> tuple[bool, str, str]:
    """Сверяет проверочную часть с зафиксированной контрольной суммой.

    Args:
        directory: Каталог набора с ``checksum.txt``.

    Returns:
        Тройка: совпадает ли, зафиксированная сумма, фактическая сумма.

    Raises:
        DatasetError: Набор не соответствует формату или нет ``checksum.txt``.
    """
    stored = stored_checksum(directory)
    if stored is None:
        raise DatasetError("Нет checksum.txt: проверочная часть не зафиксирована")
    actual = holdout_checksum(read(directory))
    return actual == stored, stored, actual


def stable_holdout(keys: list[str], share: float) -> set[str]:
    """Выбирает долю ключей в проверочную часть детерминированно.

    Ключи упорядочиваются по SHA-256, и в проверочную часть попадают первые
    ``⌊n · share + 0,5⌋``. Результат не зависит от генератора случайных
    чисел и порядка входа, поэтому разбиение воспроизводимо.

    Args:
        keys: Уникальные ключи (например, идентификаторы пар одного класса).
        share: Доля проверочной части, от 0 до 1.

    Returns:
        Ключи проверочной части.

    Raises:
        DatasetError: Ключи повторяются.
    """
    if len(set(keys)) != len(keys):
        raise DatasetError("Ключи разбиения повторяются")
    ranked = sorted(keys, key=lambda k: hashlib.sha256(k.encode()).hexdigest())
    return set(ranked[: int(len(keys) * share + 0.5)])
