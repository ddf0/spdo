"""Интерфейс поиска похожих обращений и общие структуры данных."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum


class LinkKind(StrEnum):
    """Предполагаемый вид связи, предлагаемый оператору."""

    DUPLICATE = "duplicate"
    RELATED = "related"


class SearchUnavailableError(RuntimeError):
    """Поиск временно недоступен (модель не загружена, индекс не построен)."""


@dataclass(frozen=True, slots=True)
class TicketQuery:
    """Данные нового обращения, по которым ищутся похожие.

    Attributes:
        title: Заголовок, до 200 символов.
        description: Описание, до 4000 символов.
        category_id: Идентификатор категории или ``None``.
        component_id: Идентификатор компонента или ``None``.
    """

    title: str
    description: str
    category_id: int | None = None
    component_id: int | None = None

    @property
    def text(self) -> str:
        """Объединённый текст «заголовок + описание» для векторизации."""
        return f"{self.title}\n{self.description}".strip()


@dataclass(frozen=True, slots=True)
class Candidate:
    """Кандидат в похожие обращения.

    Attributes:
        ticket_id: Идентификатор найденного обращения.
        score: Итоговый коэффициент сходства S в диапазоне [0, 1].
        kind: Предполагаемый вид связи.
        components: Составляющие S: ``sem``, ``lex``, ``cat``, ``comp``.
    """

    ticket_id: int
    score: float
    kind: LinkKind
    components: dict[str, float] = field(default_factory=dict)


class SimilaritySearcher(ABC):
    """Базовый класс поиска похожих обращений.

    Наследники реализуют конкретный способ вычисления сходства. Общая
    логика — отсечение по порогам и определение вида связи — находится
    здесь.

    Attributes:
        threshold_duplicate: Порог P1.
        threshold_related: Порог P2.
    """

    #: Идентификатор режима, сохраняемый в сеансе поиска.
    mode: str = "base"

    def __init__(self, threshold_duplicate: float, threshold_related: float) -> None:
        """Создаёт поисковик с заданными порогами.

        Args:
            threshold_duplicate: Порог P1 для «возможного дубликата».
            threshold_related: Порог P2 для «возможно связанного».
        """
        self.threshold_duplicate = threshold_duplicate
        self.threshold_related = threshold_related

    @property
    @abstractmethod
    def model_version(self) -> str:
        """Версия модели или алгоритма для записи в сеанс поиска."""

    @abstractmethod
    def search(self, query: TicketQuery, limit: int) -> list[Candidate]:
        """Возвращает кандидатов, отсортированных по убыванию сходства.

        Args:
            query: Данные нового обращения.
            limit: Максимальное число кандидатов, от 1 до 10.

        Returns:
            Кандидаты с коэффициентом сходства не ниже порога P2.

        Raises:
            SearchUnavailableError: Поиск временно недоступен.
        """

    def classify(self, score: float) -> LinkKind | None:
        """Определяет вид связи по двухпороговой схеме.

        Args:
            score: Итоговый коэффициент сходства.

        Returns:
            ``DUPLICATE`` при S ≥ P1, ``RELATED`` при P2 ≤ S < P1,
            ``None`` — если кандидат не показывается.
        """
        if score >= self.threshold_duplicate:
            return LinkKind.DUPLICATE
        if score >= self.threshold_related:
            return LinkKind.RELATED
        return None
