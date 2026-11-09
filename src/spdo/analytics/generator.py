"""Генерация нагрузочного набора обращений.

Для профилирования и нагрузочного тестирования (ТЗ: подбор кандидатов и
реестр до 5 000 обращений, p95 ≤ 2 с) реестр заполняется обращениями,
полученными из эталонного набора: к шаблону добавляются вводные и
заключительные фразы, номера аудиторий и корпусов, коды ошибок. Темы
распределены неравномерно, как в реальном потоке обращений: вес шаблона
убывает как 1/√ранга, порядок тем случаен при заданном зерне.

Генерация детерминирована при заданном зерне. Обращения создаются
сервисными функциями модуля ``tickets`` (проверки, история, поисковый
индекс), статусы меняются только допустимыми переходами. Все записи
создаются в одной транзакции и получили бы одно время, поэтому даты
обращений затем равномерно распределяются по периоду, а записи истории,
журнала действий и решения — сдвигаются вслед за датой обращения. Набор
предназначен для отдельной базы нагрузочных испытаний, а не для рабочей.
"""

import math
import random
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import bindparam, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session
from sqlalchemy.types import BigInteger

from spdo.analytics.loader import ensure_dictionaries
from spdo.search.models import SearchIndexEntry
from spdo.tickets import service as tickets
from spdo.tickets.access import Actor, require_admin
from spdo.tickets.models import DESCRIPTION_MAX, TITLE_MAX, TicketStatus
from spdo.users import service as users
from spdo.users.models import Role, User

#: Вводные фразы описания.
OPENERS = (
    "",
    "Добрый день! ",
    "Здравствуйте. ",
    "Коллеги, помогите, пожалуйста. ",
    "Повторно обращаюсь. ",
    "Срочно, идёт занятие. ",
)
#: Заключительные фразы описания с подставляемыми подробностями.
CLOSERS = (
    "",
    " Аудитория {room}, корпус {building}.",
    " Рабочее место {room}-{seat}, инвентарный номер {inv}.",
    " Проблема началась сегодня около {hour}:00.",
    " Код ошибки 0x{code:04X}.",
    " Перезагрузка не помогла.",
    " Контактный телефон: доб. {phone}.",
)
#: Окончания заголовка.
TITLE_SUFFIXES = ("", "", " (корпус {building})", " — ауд. {room}", ", срочно", " снова")
#: Доли статусов в наборе.
STATUS_SHARES = {
    TicketStatus.NEW: 0.35,
    TicketStatus.IN_PROGRESS: 0.20,
    TicketStatus.RESOLVED: 0.15,
    TicketStatus.CLOSED: 0.30,
}
#: Доля конфиденциальных обращений.
CONFIDENTIAL_SHARE = 0.05
#: Префикс имён создаваемых авторов.
AUTHOR_PREFIX = "gen-"
#: Интервал между последовательными записями истории одного обращения.
STEP = timedelta(minutes=30)
#: Текст типового решения.
SOLUTION_TEXT = "Выполнена типовая процедура восстановления, работоспособность проверена."


class GenerationError(RuntimeError):
    """Набор создан не полностью: обращения не попали в поисковый индекс."""


@dataclass(frozen=True, slots=True)
class Item:
    """Сгенерированное обращение.

    Attributes:
        title: Заголовок.
        description: Описание.
        category: Имя категории.
        component: Имя компонента.
        status: Итоговый статус.
        confidential: Признак конфиденциальности.
        author: Порядковый номер автора, с нуля.
    """

    title: str
    description: str
    category: str
    component: str
    status: TicketStatus
    confidential: bool
    author: int


def _details(rng: random.Random) -> dict[str, int]:
    """Случайные подробности для подстановки во фразы."""
    return {
        "room": rng.randint(100, 950),
        "building": rng.randint(1, 9),
        "seat": rng.randint(1, 30),
        "inv": rng.randint(100000, 999999),
        "hour": rng.randint(8, 20),
        "code": rng.randint(0, 0xFFFF),
        "phone": rng.randint(1000, 9999),
    }


def generate(
    templates: Sequence[Mapping[str, str]], count: int, authors: int, seed: int
) -> list[Item]:
    """Строит обращения по шаблонам эталонного набора.

    Args:
        templates: Строки ``tickets.csv`` с полями ``title``,
            ``description``, ``category``, ``component``.
        count: Число обращений.
        authors: Число авторов, между которыми распределяются обращения.
        seed: Зерно генератора случайных чисел.

    Returns:
        Обращения в порядке регистрации.

    Raises:
        ValueError: Нет шаблонов, ``count`` или ``authors`` меньше единицы.
        KeyError: В шаблоне нет обязательного поля.
    """
    if not templates or count < 1 or authors < 1:
        raise ValueError("Нужны шаблоны, count >= 1 и authors >= 1")
    rng = random.Random(seed)
    topics = list(templates)
    rng.shuffle(topics)
    weights = [1 / math.sqrt(rank) for rank in range(1, len(topics) + 1)]
    statuses, shares = list(STATUS_SHARES), list(STATUS_SHARES.values())
    items = []
    for _ in range(count):
        tpl = rng.choices(topics, weights)[0]
        details = _details(rng)
        title = tpl["title"] + rng.choice(TITLE_SUFFIXES).format(**details)
        description = (
            rng.choice(OPENERS) + tpl["description"] + rng.choice(CLOSERS).format(**details)
        )
        items.append(
            Item(
                title=title[:TITLE_MAX],
                description=description[:DESCRIPTION_MAX],
                category=tpl["category"],
                component=tpl["component"],
                status=rng.choices(statuses, shares)[0],
                confidential=rng.random() < CONFIDENTIAL_SHARE,
                author=rng.randrange(authors),
            )
        )
    return items


def _check_indexed(session: Session, ticket_ids: Sequence[int]) -> None:
    """Проверяет, что обращения попали в поисковый индекс.

    Регистрация не прерывается сбоем поиска (обращение просто не попадает
    в индекс), но нагрузочный замер по неполному индексу был бы ложно
    быстрым.

    Raises:
        GenerationError: Часть обращений не проиндексирована.
    """
    indexed = session.scalar(
        select(func.count())
        .select_from(SearchIndexEntry)
        .where(SearchIndexEntry.ticket_id.in_(ticket_ids))
    )
    if indexed != len(ticket_ids):
        raise GenerationError(
            f"В поисковом индексе {indexed} из {len(ticket_ids)} обращений; "
            "проверьте режим поиска и модель (журнал приложения)"
        )


#: Даты записей, связанных с обращением, вслед за датой обращения.
_SPREAD_SQL = text(
    """
WITH s AS (
    SELECT id, ord FROM unnest(:ids) WITH ORDINALITY AS u(id, ord)
), t AS (
    UPDATE tickets SET created_at = :start + (s.ord - 1) * :step
    FROM s WHERE tickets.id = s.id
    RETURNING tickets.id, tickets.created_at
), h AS (
    UPDATE ticket_history SET created_at = t.created_at + (
        SELECT count(*) FROM ticket_history p
        WHERE p.ticket_id = ticket_history.ticket_id AND p.id < ticket_history.id
    ) * :history_step
    FROM t WHERE ticket_history.ticket_id = t.id
    RETURNING ticket_history.ticket_id, ticket_history.created_at
), a AS (
    UPDATE action_log SET created_at = t.created_at + (
        SELECT count(*) FROM action_log p
        WHERE p.ticket_id = action_log.ticket_id AND p.id <= action_log.id
    ) * :history_step
    FROM t WHERE action_log.ticket_id = t.id
)
UPDATE solutions SET created_at = t.created_at + 2 * :history_step
FROM t WHERE solutions.ticket_id = t.id
"""
).bindparams(bindparam("ids", type_=ARRAY(BigInteger)))
#: Дата изменения обращения — дата его последней записи истории.
_UPDATED_SQL = text(
    """
UPDATE tickets SET updated_at = h.last
FROM (SELECT ticket_id, max(created_at) AS last FROM ticket_history
      WHERE ticket_id = ANY(:ids) GROUP BY ticket_id) h
WHERE tickets.id = h.ticket_id
"""
).bindparams(bindparam("ids", type_=ARRAY(BigInteger)))


def spread_dates(session: Session, ticket_ids: Sequence[int], days: int, end: datetime) -> None:
    """Распределяет даты обращений равномерно по периоду, в порядке номеров.

    Записи истории обращения получают даты через :data:`STEP` после даты
    обращения (создание, принятие в работу, решение, закрытие), записи
    журнала действий — так же, решение — дату своей записи истории.

    Args:
        session: Сессия БД.
        ticket_ids: Обращения в порядке регистрации.
        days: Длина периода в днях.
        end: Конец периода (дата последнего обращения).
    """
    if not ticket_ids:
        return
    span = timedelta(days=days)
    step = span / max(len(ticket_ids) - 1, 1)
    params = {"ids": list(ticket_ids), "start": end - span, "step": step, "history_step": STEP}
    session.execute(_SPREAD_SQL, params)
    session.execute(_UPDATED_SQL, {"ids": list(ticket_ids)})
    session.expire_all()


def _authors(session: Session, count: int) -> list[User]:
    """Находит или создаёт авторов ``gen-001`` … с недоступными для входа паролями."""
    result = []
    for number in range(1, count + 1):
        name = f"{AUTHOR_PREFIX}{number:03d}"
        user = session.scalar(select(User).where(User.username == name))
        if user is None:
            user = users.create_user(
                session,
                username=name,
                full_name=f"Сотрудник {number:03d}",
                password=secrets.token_urlsafe(24),
                role=Role.USER,
            )
        result.append(user)
    return result


def _advance(session: Session, admin: Actor, author: Actor, ticket_id: int, item: Item) -> None:
    """Переводит обращение в итоговый статус допустимыми переходами."""
    if item.status is TicketStatus.NEW:
        return
    tickets.change_status(session, admin, ticket_id, TicketStatus.IN_PROGRESS)
    if item.status is TicketStatus.IN_PROGRESS:
        return
    tickets.publish_solution(session, admin, ticket_id, SOLUTION_TEXT)
    if item.status is TicketStatus.CLOSED:
        tickets.confirm_resolution(session, author, ticket_id)


def populate(
    session: Session,
    admin: Actor,
    items: Sequence[Item],
    *,
    days: int = 365,
    end: datetime | None = None,
    progress: Callable[[int], None] | None = None,
) -> list[int]:
    """Регистрирует сгенерированные обращения в БД.

    Наличие первого обращения в поисковом индексе проверяется сразу,
    остальных — в конце: сбой поиска не тратит время на весь набор.

    Args:
        session: Сессия БД.
        admin: Администратор: создаёт справочники и меняет статусы.
        items: Обращения из :func:`generate`.
        days: Период, по которому распределяются даты обращений.
        end: Дата последнего обращения; по умолчанию — текущее время БД.
        progress: Вызывается с числом созданных обращений каждые 500.

    Returns:
        Номера созданных обращений в порядке регистрации.

    Raises:
        PermissionDeniedError: Пользователь не администратор.
        GenerationError: Обращения не попали в поисковый индекс.
    """
    require_admin(admin)
    rows = [{"category": i.category, "component": i.component} for i in items]
    categories, components = ensure_dictionaries(session, admin, rows)
    # Авторов столько, сколько их номеров встречается в наборе.
    authors = _authors(session, max((i.author for i in items), default=-1) + 1)
    created: list[int] = []
    for done, item in enumerate(items, start=1):
        author = authors[item.author]
        ticket = tickets.create_ticket(
            session,
            author,
            title=item.title,
            description=item.description,
            category_id=categories[item.category.lower()],
            component_id=components[item.component.lower()],
        )
        created.append(ticket.id)
        if done == 1:
            _check_indexed(session, created)
        _advance(session, admin, author, ticket.id, item)
        if item.confidential:
            tickets.set_confidential(session, admin, ticket.id, True)
        if progress is not None and done % 500 == 0:
            progress(done)
    _check_indexed(session, created)
    spread_dates(session, created, days, end or session.scalar(select(func.now())))
    return created
