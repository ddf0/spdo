"""Команды администрирования СПДО: ``spdo <команда>``.

Команды:
    close-expired: Закрыть решённые обращения, не подтверждённые в срок.
"""

import argparse
import sys
from datetime import UTC, datetime

from sqlalchemy import select

from spdo.config import settings
from spdo.db import models  # noqa: F401  регистрирует таблицы всех модулей
from spdo.db.session import SessionLocal
from spdo.tickets import service as tickets
from spdo.tickets.access import PermissionDeniedError
from spdo.users.models import User


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
        if not args.admin:
            print("Ошибка: укажите --admin", file=sys.stderr)
            return 1
        admin = session.scalar(select(User).where(User.username == args.admin.strip().lower()))
        if admin is None or not admin.is_active:
            print(f"Ошибка: активный пользователь «{args.admin}» не найден", file=sys.stderr)
            return 1
        try:
            ids = tickets.close_expired(session, admin, days, now)
        except PermissionDeniedError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        session.commit()
    print(f"Закрыто обращений: {len(ids)}" + (f" ({', '.join(map(str, ids))})" if ids else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Создаёт разборщик аргументов командной строки.

    Returns:
        Разборщик с подкомандами.
    """
    parser = argparse.ArgumentParser(prog="spdo", description="Администрирование СПДО")
    commands = parser.add_subparsers(dest="command", required=True)

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
