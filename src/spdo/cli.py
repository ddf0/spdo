"""Команды администрирования СПДО: ``spdo <команда>``.

Команды:
    create-user: Создать пользователя (в том числе первого администратора).
    close-expired: Закрыть решённые обращения, не подтверждённые в срок.
    dataset: Подготовка, проверка и загрузка эталонного набора.
"""

import argparse
import csv
import getpass
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from spdo.analytics import dataset, loader, sodd
from spdo.config import settings
from spdo.db import models  # noqa: F401  регистрирует таблицы всех модулей
from spdo.db.session import SessionLocal
from spdo.tickets import service as tickets
from spdo.tickets.access import PermissionDeniedError, TicketValidationError
from spdo.users import service
from spdo.users.models import Role, User
from spdo.users.security import PasswordPolicyError

#: Переменная окружения с паролем для неинтерактивного запуска.
PASSWORD_ENV = "SPDO_NEW_USER_PASSWORD"


def _read_password() -> str:
    """Читает пароль из окружения или с терминала без отображения.

    Returns:
        Введённый пароль.

    Raises:
        SystemExit: Пароли при повторном вводе не совпали.
    """
    from_env = os.environ.pop(PASSWORD_ENV, None)
    if from_env:
        return from_env
    first = getpass.getpass("Пароль: ")
    if first != getpass.getpass("Повторите пароль: "):
        raise SystemExit("Пароли не совпадают")
    return first


def cmd_create_user(args: argparse.Namespace) -> int:
    """Создаёт пользователя.

    Args:
        args: Разобранные аргументы: ``username``, ``full_name``, ``role``.

    Returns:
        Код завершения: 0 — успех, 1 — ошибка данных.
    """
    password = _read_password()
    with SessionLocal() as session:
        try:
            user = service.create_user(
                session,
                username=args.username,
                full_name=args.full_name,
                password=password,
                role=Role(args.role),
            )
        except (service.UserError, PasswordPolicyError) as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        session.commit()
        print(f"Создан пользователь {user.username} ({user.role.value}), id={user.id}")
    return 0


def positive_int(value: str) -> int:
    """Разбирает целое число не меньше 1.

    Args:
        value: Строка из командной строки.

    Returns:
        Число.

    Raises:
        argparse.ArgumentTypeError: Строка — не целое число ≥ 1.
    """
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("нужно целое число") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("нужно число не меньше 1")
    return number


def cmd_close_expired(args: argparse.Namespace) -> int:
    """Закрывает решённые обращения с истёкшим сроком подтверждения.

    Операцию запускает администратор вручную (ТЗ исключает закрытие без
    участия человека); изменения записываются в историю от его имени.
    С ``--dry-run`` только выводит номера обращений.

    Args:
        args: Разобранные аргументы: ``admin``, ``days``, ``dry_run``.

    Returns:
        Код завершения: 0 — успех, 1 — администратор не указан, не найден
        или не имеет прав.
    """
    days = settings.resolved_close_days if args.days is None else args.days
    now = datetime.now(UTC)
    with SessionLocal() as session:
        if args.dry_run:
            ids = [t.id for t in tickets.find_expired(session, days, now)]
            print(f"Будут закрыты ({len(ids)}): {', '.join(map(str, ids)) or '—'}")
            return 0
        admin = _find_admin(session, args.admin)
        if admin is None:
            return 1
        try:
            ids = tickets.close_expired(session, admin, days, now)
        except PermissionDeniedError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        session.commit()
    print(f"Закрыто обращений: {len(ids)}" + (f" ({', '.join(map(str, ids))})" if ids else ""))
    return 0


def _find_admin(session, username: str | None) -> User | None:
    """Находит активного пользователя по имени для входа; сообщает об ошибке."""
    if not username:
        print("Ошибка: укажите --admin", file=sys.stderr)
        return None
    user = session.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None or not user.is_active:
        print(f"Ошибка: активный пользователь «{username}» не найден", file=sys.stderr)
        return None
    return user


def cmd_dataset_convert_sodd(args: argparse.Namespace) -> int:
    """Преобразует исходные файлы SODD-ru в эталонный формат.

    Args:
        args: Разобранные аргументы: ``raw_dir``, ``out_dir``, ``force``.

    Returns:
        Код завершения: 0 — успех, 1 — ошибка исходных данных или попытка
        заменить зафиксированную проверочную часть.
    """
    raw, out = Path(args.raw_dir), Path(args.out_dir)
    try:
        data = sodd.convert(raw)
        files = {"ATTRIBUTION.md": sodd.attribution(raw)}
        checksum = dataset.write(out, data, sodd.EXTRA_FIELDS, force=args.force, extra_files=files)
    except (dataset.DatasetError, OSError, KeyError, csv.Error) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    holdout = sum(t["split"] == "holdout" for t in data.tickets)
    print(f"Обращений: {len(data.tickets)}, из них в проверочной части: {holdout}")
    print(f"Контрольная сумма проверочной части: {checksum}")
    return 0


def cmd_dataset_verify(args: argparse.Namespace) -> int:
    """Сверяет проверочную часть набора с контрольной суммой.

    Args:
        args: Разобранные аргументы: ``dir``.

    Returns:
        Код завершения: 0 — совпадает, 1 — изменена или набор неверен.
    """
    try:
        same, stored, actual = dataset.verify(Path(args.dir))
    except dataset.DatasetError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    if same:
        print(f"Проверочная часть не изменена ({actual})")
        return 0
    print(f"Проверочная часть ИЗМЕНЕНА: зафиксировано {stored}, фактически {actual}")
    return 1


def cmd_dataset_load(args: argparse.Namespace) -> int:
    """Загружает обращения набора в БД от имени администратора.

    Перед загрузкой сверяется контрольная сумма проверочной части.

    Args:
        args: Разобранные аргументы: ``dir``, ``admin``.

    Returns:
        Код завершения: 0 — успех, 1 — ошибка.
    """
    directory = Path(args.dir)
    with SessionLocal() as session:
        admin = _find_admin(session, args.admin)
        if admin is None:
            return 1
        try:
            same, _, _ = dataset.verify(directory)
            if not same:
                raise dataset.DatasetError("проверочная часть изменена; см. spdo dataset verify")
            loader.recover(session, directory)
            mapping = loader.load(session, admin, dataset.read(directory), directory)
            loader.stage_mapping(directory, mapping)
        except (
            dataset.DatasetError,
            loader.AlreadyLoadedError,
            PermissionDeniedError,
            TicketValidationError,
            OSError,
        ) as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        session.commit()
    path = loader.finalize_mapping(directory)
    print(f"Загружено обращений: {len(mapping)}; соответствие номеров — {path}")
    return 0


def _add_dataset_commands(commands: argparse._SubParsersAction) -> None:
    """Добавляет подкоманды ``spdo dataset …``."""
    ds = commands.add_parser("dataset", help="эталонный набор обращений")
    sub = ds.add_subparsers(dest="dataset_command", required=True)
    conv = sub.add_parser("convert-sodd", help="преобразовать SODD-ru в эталонный формат")
    conv.add_argument("raw_dir", help="каталог с pairs.csv и tickets_ru.csv")
    conv.add_argument("out_dir", help="каталог вывода набора")
    conv.add_argument(
        "--force", action="store_true", help="заменить зафиксированную проверочную часть"
    )
    conv.set_defaults(handler=cmd_dataset_convert_sodd)
    ver = sub.add_parser("verify", help="сверить проверочную часть с контрольной суммой")
    ver.add_argument("dir", help="каталог набора")
    ver.set_defaults(handler=cmd_dataset_verify)
    load = sub.add_parser("load", help="загрузить обращения набора в БД")
    load.add_argument("dir", help="каталог набора")
    load.add_argument("--admin", required=True, help="администратор — автор обращений")
    load.set_defaults(handler=cmd_dataset_load)


def build_parser() -> argparse.ArgumentParser:
    """Создаёт разборщик аргументов командной строки.

    Returns:
        Разборщик с подкомандами.
    """
    parser = argparse.ArgumentParser(prog="spdo", description="Администрирование СПДО")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-user", help="создать пользователя")
    create.add_argument("username", help="имя для входа")
    create.add_argument("--full-name", default="", help="отображаемое имя")
    create.add_argument(
        "--role", choices=[r.value for r in Role], default=Role.USER.value, help="роль"
    )
    create.set_defaults(handler=cmd_create_user)

    expire = commands.add_parser(
        "close-expired", help="закрыть решённые обращения, не подтверждённые в срок"
    )
    expire.add_argument("--admin", help="имя администратора, от чьего имени (кроме --dry-run)")
    expire.add_argument(
        "--days",
        type=positive_int,
        help=f"срок в днях (по умолчанию {settings.resolved_close_days})",
    )
    expire.add_argument("--dry-run", action="store_true", help="только показать, что закроется")
    expire.set_defaults(handler=cmd_close_expired)
    _add_dataset_commands(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Запускает команду CLI.

    Args:
        argv: Аргументы командной строки; по умолчанию — ``sys.argv``.

    Returns:
        Код завершения команды.
    """
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
