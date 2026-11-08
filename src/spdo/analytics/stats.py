"""Отчёт о качестве рекомендаций за период (Ф14, ТТ.Ф14, ПЦ9).

Показатели работы системы считаются по сеансам поиска, рекомендациям и
оценкам решений за период; recall@3 на проверочной выборке берётся из
последнего сохранённого замера (``spdo eval --save``), потому что по живым
сеансам его вычислить нельзя — нужна эталонная разметка.

Определения:

* доля сеансов с подтверждённым дубликатом — от сеансов, завершённых
  регистрацией (подбор при вводе создаёт несколько сеансов на обращение);
* доля отклонённых рекомендаций — от рекомендаций с вынесенным вердиктом;
* дубликаты в первых трёх позициях — подтверждённые дубликаты с позицией
  не больше 3; их доля — от всех подтверждённых дубликатов.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from spdo.analytics.measurements import recent_measurements
from spdo.analytics.models import QualityMeasurement
from spdo.config import settings
from spdo.recommendations import stats as rec_stats
from spdo.relations.service import duplicate_recommendation_ids
from spdo.tickets import service as tickets
from spdo.tickets.access import Actor, TicketValidationError, require_staff

#: Период отчёта по умолчанию, дней (включая сегодняшний).
DEFAULT_DAYS = 30
#: Число последних замеров качества в отчёте.
HISTORY_LIMIT = 10
#: Самый ранний допустимый год периода.
MIN_YEAR = 2000
#: Наибольшая длина периода, дней.
MAX_SPAN_DAYS = 5 * 366
#: Насколько конец периода может быть позже текущей даты, дней.
MAX_AHEAD_DAYS = 366
#: Норма recall@3 на проверочной части (ТЗ, п. 8.3; ТТ.К1).
RECALL_TARGET = 0.80


def share(part: int, whole: int) -> float | None:
    """Доля ``part / whole`` или ``None``, если знаменатель равен нулю.

    Args:
        part: Числитель.
        whole: Знаменатель.

    Returns:
        Доля в [0, 1] или ``None``.
    """
    return part / whole if whole else None


@dataclass(frozen=True, slots=True)
class Period:
    """Период отчёта в календарных днях часового пояса интерфейса.

    Attributes:
        first: Первый день (включительно).
        last: Последний день (включительно).
    """

    first: date
    last: date

    @classmethod
    def parse(cls, first: str | None, last: str | None, today: date) -> "Period":
        """Разбирает даты из формы; пустые — последние :data:`DEFAULT_DAYS` дней.

        Args:
            first: Первый день в формате ``ГГГГ-ММ-ДД`` или пусто.
            last: Последний день в формате ``ГГГГ-ММ-ДД`` или пусто.
            today: Текущая дата.

        Returns:
            Период.

        Raises:
            TicketValidationError: Дата неверна, начало позже конца или период
                вне допустимого диапазона (с :data:`MIN_YEAR` года, не дальше
                :data:`MAX_AHEAD_DAYS` дней вперёд, не длиннее
                :data:`MAX_SPAN_DAYS` дней).
        """
        try:
            end = date.fromisoformat(last) if last else today
            start = date.fromisoformat(first) if first else end - timedelta(days=DEFAULT_DAYS - 1)
        except (ValueError, OverflowError) as exc:
            raise TicketValidationError("Дата периода указана неверно") from exc
        if start > end:
            raise TicketValidationError("Начало периода позже конца")
        too_long = (end - start).days >= MAX_SPAN_DAYS
        if start.year < MIN_YEAR or (end - today).days > MAX_AHEAD_DAYS or too_long:
            raise TicketValidationError("Период вне допустимого диапазона")
        return cls(start, end)

    def bounds(self) -> tuple[datetime, datetime]:
        """Границы периода как полуинтервал [начало первого дня, начало дня после последнего).

        Returns:
            Пара моментов времени с часовым поясом интерфейса.
        """
        tz = ZoneInfo(settings.display_timezone)
        start = datetime.combine(self.first, time.min, tz)
        end = datetime.combine(self.last + timedelta(days=1), time.min, tz)
        return start, end


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Отчёт о качестве рекомендаций.

    Attributes:
        period: Период.
        sessions: Сеансы поиска.
        recommendations: Рекомендации.
        ratings_total: Оценки решений.
        ratings_helped: Оценки «помогло».
        measurements: Последние замеры на проверочной части, от новых к старым.
    """

    period: Period
    sessions: rec_stats.SessionStats
    recommendations: rec_stats.RecommendationStats
    ratings_total: int
    ratings_helped: int
    measurements: list[QualityMeasurement]

    @property
    def duplicate_share(self) -> float | None:
        """Доля сеансов с подтверждённым дубликатом среди завершённых регистрацией."""
        return share(self.sessions.with_duplicate, self.sessions.registered)

    @property
    def rejected_share(self) -> float | None:
        """Доля отклонённых среди рекомендаций с вердиктом."""
        r = self.recommendations
        return share(r.rejected, r.accepted + r.rejected)

    @property
    def top_share(self) -> float | None:
        """Доля подтверждённых дубликатов, найденных в первых трёх позициях."""
        return share(self.recommendations.duplicates_top, self.recommendations.duplicates)

    @property
    def helped_share(self) -> float | None:
        """Доля оценок «помогло»."""
        return share(self.ratings_helped, self.ratings_total)

    @property
    def latest(self) -> QualityMeasurement | None:
        """Последний замер recall@3 на проверочной выборке."""
        return self.measurements[0] if self.measurements else None


def build_report(session: Session, actor: Actor, period: Period) -> QualityReport:
    """Строит отчёт о качестве рекомендаций за период (Ф14).

    Args:
        session: Сессия БД.
        actor: Оператор или администратор.
        period: Период.

    Returns:
        Отчёт.

    Raises:
        PermissionDeniedError: Пользователь не оператор и не администратор.
    """
    require_staff(actor)
    start, end = period.bounds()
    dup_ids = duplicate_recommendation_ids()
    total, helped = tickets.rating_stats(session, start, end)
    return QualityReport(
        period=period,
        sessions=rec_stats.session_stats(session, start, end, dup_ids),
        recommendations=rec_stats.recommendation_stats(session, start, end, dup_ids),
        ratings_total=total,
        ratings_helped=helped,
        measurements=recent_measurements(session, "holdout", HISTORY_LIMIT),
    )
