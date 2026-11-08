"""Параметры поиска, настраиваемые администратором (Ф16).

Значения из таблицы ``search_settings`` перекрывают параметры окружения
(``SPDO_*``) и проверяются тем же валидатором конфигурации: 0 < P2 < P1 ≤ 1,
1 ≤ N ≤ 10, веса неотрицательны и в сумме дают 1. Применение меняет
параметры процесса без перезапуска: при том же режиме пороги и веса
обновляются у работающего поисковика (индекс в памяти и модель
сохраняются), при смене режима поисковик создаётся заново.

Модель векторов здесь не настраивается: её смена требует миграции
размерности столбца и переиндексации (ТЗ.Ф22, ``spdo reindex``).
Параметры хранятся в памяти процесса — рекомендуется один процесс uvicorn;
при нескольких процессах изменение применяет только обработавший запрос,
остальные — после перезапуска (страница администратора предупреждает о
расхождении).
"""

import logging
import threading
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from spdo.config import SearchMode, Settings, settings
from spdo.search import factory
from spdo.search.models import SearchSettingsRecord

logger = logging.getLogger(__name__)

#: Параметры, которые может менять администратор.
TUNABLE = (
    "search_mode",
    "top_n",
    "threshold_duplicate",
    "threshold_related",
    "w_semantic",
    "w_lexical",
    "w_category",
    "w_component",
    "lexical_threshold_duplicate",
    "lexical_threshold_related",
    "lexical_w_lexical",
    "lexical_w_category",
    "lexical_w_component",
)

#: Сериализует чтение последней записи и её применение.
_APPLY_LOCK = threading.Lock()
#: Перекрытые параметры, действующие в процессе.
_applied: dict[str, Any] = {}

#: Значения параметров из окружения, зафиксированные при запуске процесса.
ENV_DEFAULTS: dict[str, Any] = {key: getattr(settings, key) for key in TUNABLE}


class SearchParamsError(ValueError):
    """Значения параметров поиска неверны или несогласованы."""


def _plain(value: Any) -> Any:  # noqa: ANN401 — значение параметра любого типа
    """Значение для JSON: режим поиска — строкой."""
    return value.value if isinstance(value, SearchMode) else value


def effective(values: dict[str, Any]) -> Settings:
    """Проверяет параметры окружения с перекрытыми значениями.

    Args:
        values: Перекрытые параметры.

    Returns:
        Проверенная конфигурация.

    Raises:
        SearchParamsError: Неизвестный параметр или значения не прошли проверку.
    """
    unknown = set(values) - set(TUNABLE)
    if unknown:
        raise SearchParamsError(f"Неизвестные параметры: {', '.join(sorted(unknown))}")
    merged = {**settings.model_dump(), **ENV_DEFAULTS, **values}
    try:
        return Settings.model_validate(merged)
    except ValidationError as exc:
        message = exc.errors()[0]["msg"].removeprefix("Value error, ")
        raise SearchParamsError(message) from exc


def current_values(session: Session) -> dict[str, Any]:
    """Действующие перекрытые параметры — значения последней записи.

    Args:
        session: Сессия БД.

    Returns:
        Перекрытые параметры; пусто — действуют параметры окружения.
    """
    latest = session.scalar(
        select(SearchSettingsRecord).order_by(SearchSettingsRecord.id.desc()).limit(1)
    )
    return dict(latest.values) if latest else {}


def history(session: Session, limit: int = 10) -> list[SearchSettingsRecord]:
    """Последние изменения параметров, начиная с нового.

    Args:
        session: Сессия БД.
        limit: Наибольшее число записей.

    Returns:
        Записи истории.
    """
    stmt = select(SearchSettingsRecord).order_by(SearchSettingsRecord.id.desc()).limit(limit)
    return list(session.scalars(stmt))


def save(
    session: Session, author_id: int, values: dict[str, Any], comment: str = ""
) -> SearchSettingsRecord:
    """Проверяет и сохраняет новые параметры; применяет их :func:`apply_latest`.

    Args:
        session: Сессия БД.
        author_id: Администратор.
        values: Перекрытые параметры; пусто — вернуть параметры окружения.
        comment: Причина изменения.

    Returns:
        Сохранённая запись.

    Raises:
        SearchParamsError: Значения неверны.
    """
    config = effective(values)
    record = SearchSettingsRecord(
        author_id=author_id,
        values={key: _plain(getattr(config, key)) for key in values},
        comment=comment.strip(),
    )
    session.add(record)
    session.flush()
    return record


def applied() -> dict[str, Any]:
    """Перекрытые параметры, действующие в этом процессе.

    Returns:
        Копия словаря параметров.
    """
    return dict(_applied)


def apply(values: dict[str, Any]) -> None:
    """Применяет параметры к процессу: конфигурация и работающий поисковик.

    Конфигурация и поисковик меняются под блокировкой построения
    поисковика, поэтому новый поисковик не строится из наполовину
    изменённой конфигурации. При смене режима новый поисковик прогревается
    в фоне.

    Args:
        values: Перекрытые параметры (непроверенные значения отклоняются).

    Raises:
        SearchParamsError: Значения неверны.
    """
    global _applied  # noqa: PLW0603 — состояние процесса
    config = effective(values)
    with factory.LOCK:
        old_mode = settings.search_mode
        for key in TUNABLE:
            setattr(settings, key, getattr(config, key))
        searcher = factory.current_searcher()
        if settings.search_mode is not old_mode:
            factory.reset_searcher()
            if settings.search_warm_up:
                threading.Thread(target=_warm_up, name="search-warm-up", daemon=True).start()
        elif searcher is not None:
            factory.reconfigure(searcher, settings)
        _applied = dict(values)
    logger.info("Параметры поиска применены: %s", {k: _plain(v) for k, v in values.items()})


def _warm_up() -> None:
    """Строит и прогревает поисковик нового режима в фоне."""
    from spdo.db.session import SessionLocal  # noqa: PLC0415 — только для фонового прогрева

    try:
        with SessionLocal() as session:
            factory.get_searcher().warm_up(session)
    except Exception:  # noqa: BLE001 — недоступность поиска не блокирует работу (ТЗ)
        logger.exception("Не удалось подготовить поиск после смены режима")


def apply_latest(session: Session) -> None:
    """Применяет последнюю сохранённую запись.

    Чтение и применение сериализованы: при одновременных сохранениях
    процесс в итоге работает с параметрами последней записи в БД.

    Args:
        session: Сессия БД, видящая зафиксированные записи.

    Raises:
        SearchParamsError: Сохранённые значения неверны.
    """
    with _APPLY_LOCK:
        apply(current_values(session))


def load_and_apply(session: Session) -> None:
    """Применяет сохранённые параметры при запуске процесса.

    Неверные сохранённые значения записываются в журнал, действуют
    параметры окружения.

    Args:
        session: Сессия БД.
    """
    try:
        apply_latest(session)
    except SearchParamsError:
        logger.exception("Сохранённые параметры поиска неверны; действуют параметры окружения")
