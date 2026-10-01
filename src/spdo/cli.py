"""Команды администрирования СПДО: ``spdo <команда>``.

Команды:
    create-user: Создать пользователя (в том числе первого администратора).
"""

import argparse
import getpass
import os
import sys

from spdo.db import models  # noqa: F401  регистрирует таблицы всех модулей
from spdo.db.session import SessionLocal
from spdo.users import service
from spdo.users.models import Role
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
