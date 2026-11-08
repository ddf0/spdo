"""Замеры качества подбора на эталонном наборе (ТЗ, п. 8.2; ТТ.К2)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from spdo import __version__
from spdo.analytics.models import QualityMeasurement
from spdo.analytics.quality import Quality


def save_measurement(  # noqa: PLR0913 — сведения о замере
    session: Session,
    *,
    dataset: str,
    split: str,
    holdout_checksum: str,
    mode: str,
    model_version: str,
    params: dict[str, object],
    quality: Quality,
) -> QualityMeasurement:
    """Сохраняет замер качества.

    Версия приложения записывается автоматически (ТЗ, п. 8.2: замер
    фиксируется для каждого релиза).

    Args:
        session: Сессия БД.
        dataset: Каталог набора.
        split: Часть набора: ``holdout`` или ``tune``.
        holdout_checksum: SHA-256 проверочной части набора.
        mode: Режим поиска.
        model_version: Версия модели и алгоритма.
        params: Пороги и веса.
        quality: Показатели качества.

    Returns:
        Сохранённый замер.
    """
    record = QualityMeasurement(
        release=__version__,
        dataset=dataset,
        split=split,
        holdout_checksum=holdout_checksum,
        mode=mode,
        model_version=model_version,
        params=params,
        **quality.as_dict(),
    )
    session.add(record)
    session.flush()
    return record


def recent_measurements(
    session: Session, split: str = "holdout", limit: int = 10
) -> list[QualityMeasurement]:
    """Возвращает последние замеры части набора, начиная с нового.

    Args:
        session: Сессия БД.
        split: Часть набора.
        limit: Наибольшее число замеров.

    Returns:
        Замеры от новых к старым.
    """
    stmt = (
        select(QualityMeasurement)
        .where(QualityMeasurement.split == split)
        .order_by(QualityMeasurement.created_at.desc(), QualityMeasurement.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt))
