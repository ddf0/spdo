"""Команды администрирования СПДО: ``spdo <команда>``.

Команды:
    create-user: Создать пользователя (в том числе первого администратора).
    close-expired: Закрыть решённые обращения, не подтверждённые в срок.
    dataset: Подготовка, проверка и загрузка эталонного набора.
    reindex: Перестроить поисковый индекс по всем обращениям.
    eval: Измерить качество подбора на эталонном наборе.
    tune: Подобрать веса и пороги режима на настроечной части.
    download-model: Скачать модель векторов в локальный кеш (при установке).
"""

import argparse
import csv
import getpass
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from spdo.analytics import dataset, evaluation, loader, sodd
from spdo.config import SearchMode, settings
from spdo.db import models  # noqa: F401  регистрирует таблицы всех модулей
from spdo.db.session import SessionLocal
from spdo.search.base import SearchUnavailableError
from spdo.search.embeddings import Encoder
from spdo.search.factory import build_searcher
from spdo.search.hybrid import HybridWeights
from spdo.search.lexical import ALGORITHM_VERSION as LEXICAL_VERSION
from spdo.search.lexical import LexicalWeights
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


def cmd_dataset_freeze(args: argparse.Namespace) -> int:
    """Проверяет набор и фиксирует контрольную сумму после ручной правки.

    Если проверочная часть уже зафиксирована и изменилась, требуется
    ``--force``: замена проверочной части должна быть осознанной.

    Args:
        args: Разобранные аргументы: ``dir``, ``force``.

    Returns:
        Код завершения: 0 — успех, 1 — ошибка формата или отказ без ``--force``.
    """
    directory = Path(args.dir)
    try:
        data = dataset.read(directory)
        extra = tuple(f for f in data.tickets[0] if f not in dataset.TICKET_FIELDS)
        checksum = dataset.write(directory, data, extra, force=args.force)
    except dataset.DatasetError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"Проверочная часть зафиксирована: {checksum}")
    return 0


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


def cmd_reindex(args: argparse.Namespace) -> int:
    """Перестраивает поисковый индекс по всем обращениям реестра.

    Args:
        args: Разобранные аргументы: ``admin``.

    Returns:
        Код завершения: 0 — успех, 1 — администратор не найден или без прав.
    """
    with SessionLocal() as session:
        admin = _find_admin(session, args.admin)
        if admin is None:
            return 1
        try:
            count = tickets.reindex_all(session, admin)
        except PermissionDeniedError as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        session.commit()
    print(f"Проиндексировано обращений: {count}")
    return 0


def _lexical_weights() -> LexicalWeights:
    """Веса базового режима из конфигурации."""
    return LexicalWeights(
        settings.lexical_w_lexical, settings.lexical_w_category, settings.lexical_w_component
    )


def _ranker(corpus: evaluation.Corpus, mode: SearchMode) -> tuple[evaluation.Ranker, dict]:
    """Ранжирование и параметры режима из конфигурации для офлайн-оценки."""
    if mode is SearchMode.LEXICAL:
        rank = evaluation.lexical_ranker(corpus, _lexical_weights(), settings.candidate_pool)
        params = {
            "weights": [
                settings.lexical_w_lexical,
                settings.lexical_w_category,
                settings.lexical_w_component,
            ],
            "threshold_duplicate": settings.lexical_threshold_duplicate,
            "threshold_related": settings.lexical_threshold_related,
            "model_version": LEXICAL_VERSION,
        }
        return rank, params
    weights = HybridWeights(
        settings.w_semantic, settings.w_lexical, settings.w_category, settings.w_component
    )
    encoder = evaluation.CachedEncoder(_encoder())
    candidates = evaluation.HybridCandidates(corpus, encoder, settings.candidate_pool)
    rank = evaluation.hybrid_ranker(candidates, weights)
    params = {
        "weights": [weights.semantic, weights.lexical, weights.category, weights.component],
        "threshold_duplicate": settings.threshold_duplicate,
        "threshold_related": settings.threshold_related,
        "model_version": build_searcher(settings, SearchMode.HYBRID).model_version,
    }
    return rank, params


def _encoder() -> Encoder:
    """Кодировщик модели из конфигурации."""
    return Encoder(
        settings.embedding_model,
        settings.embedding_dim,
        settings.embedding_device,
        settings.embedding_max_tokens,
        settings.embedding_threads,
        settings.embedding_allow_download,
    )


def cmd_eval(args: argparse.Namespace) -> int:
    """Измеряет качество подбора на части эталонного набора.

    Args:
        args: Разобранные аргументы: ``dir``, ``split``, ``mode``, ``json``.

    Returns:
        Код завершения: 0 — успех, 1 — набор неверен.
    """
    try:
        data = dataset.read(Path(args.dir))
    except dataset.DatasetError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    mode = SearchMode(args.mode)
    corpus = evaluation.build_corpus(data)
    rank, params = _ranker(corpus, mode)
    thresholds = (params["threshold_duplicate"], params["threshold_related"])
    quality = evaluation.evaluate(corpus, args.split, rank, thresholds)
    report = {
        "dataset": str(args.dir),
        "split": args.split,
        "mode": mode.value,
        "model_version": params.pop("model_version"),
        "params": params,
        **quality.as_dict(),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json:
        Path(args.json).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def cmd_tune(args: argparse.Namespace) -> int:
    """Подбирает веса и пороги режима на настроечной части набора.

    Args:
        args: Разобранные аргументы: ``dir``, ``mode``.

    Returns:
        Код завершения: 0 — успех, 1 — набор неверен.
    """
    try:
        data = dataset.read(Path(args.dir))
    except dataset.DatasetError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    if SearchMode(args.mode) is SearchMode.LEXICAL:
        t = evaluation.tune_lexical(data, settings.candidate_pool)
        lines = {
            "SPDO_LEXICAL_W_LEXICAL": t.weights.lexical,
            "SPDO_LEXICAL_W_CATEGORY": t.weights.category,
            "SPDO_LEXICAL_W_COMPONENT": t.weights.component,
            "SPDO_LEXICAL_THRESHOLD_DUPLICATE": t.threshold_duplicate,
            "SPDO_LEXICAL_THRESHOLD_RELATED": t.threshold_related,
        }
    else:
        corpus = evaluation.build_corpus(data)
        h = evaluation.tune_hybrid(
            corpus, evaluation.CachedEncoder(_encoder()), settings.candidate_pool
        )
        t = h
        lines = {
            "SPDO_W_SEMANTIC": h.weights.semantic,
            "SPDO_W_LEXICAL": h.weights.lexical,
            "SPDO_W_CATEGORY": h.weights.category,
            "SPDO_W_COMPONENT": h.weights.component,
            "SPDO_THRESHOLD_DUPLICATE": h.threshold_duplicate,
            "SPDO_THRESHOLD_RELATED": h.threshold_related,
        }
    print(f"F1 «дубликат» = {t.f1_duplicate:.3f}, F1 «показать» = {t.f1_shown:.3f}")
    print("Параметры для .env:")
    for key, value in lines.items():
        print(f"{key}={value}")
    return 0


def cmd_download_model(args: argparse.Namespace) -> int:
    """Скачивает модель векторных представлений в локальный кеш.

    Выполняется при установке; при работе модель берётся только из кеша.

    Args:
        args: Разобранные аргументы (не используются).

    Returns:
        Код завершения: 0 — успех, 1 — модель не скачалась.
    """
    encoder = Encoder(
        settings.embedding_model,
        settings.embedding_dim,
        settings.embedding_device,
        settings.embedding_max_tokens,
        allow_download=True,
    )
    try:
        encoder.load()
    except SearchUnavailableError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"Модель {settings.embedding_model} в локальном кеше")
    return 0


def _add_search_commands(commands: argparse._SubParsersAction) -> None:
    """Добавляет подкоманды ``reindex``, ``eval``, ``tune``, ``download-model``."""
    rx = commands.add_parser("reindex", help="перестроить поисковый индекс")
    rx.add_argument("--admin", required=True, help="администратор")
    rx.set_defaults(handler=cmd_reindex)
    ev = commands.add_parser("eval", help="измерить качество подбора на эталонном наборе")
    ev.add_argument("dir", help="каталог набора")
    ev.add_argument("--split", choices=["holdout", "tune"], default="holdout")
    ev.add_argument("--mode", choices=[m.value for m in SearchMode], default=settings.search_mode)
    ev.add_argument("--json", help="файл для сохранения результата")
    ev.set_defaults(handler=cmd_eval)
    tn = commands.add_parser("tune", help="подобрать веса и пороги базового режима")
    tn.add_argument("dir", help="каталог набора")
    tn.add_argument("--mode", choices=[m.value for m in SearchMode], default=settings.search_mode)
    tn.set_defaults(handler=cmd_tune)
    dm = commands.add_parser("download-model", help="скачать модель векторов в локальный кеш")
    dm.set_defaults(handler=cmd_download_model)


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
    frz = sub.add_parser("freeze", help="проверить набор и зафиксировать контрольную сумму")
    frz.add_argument("dir", help="каталог набора")
    frz.add_argument("--force", action="store_true", help="заменить зафиксированную часть")
    frz.set_defaults(handler=cmd_dataset_freeze)
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
    _add_search_commands(commands)
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
