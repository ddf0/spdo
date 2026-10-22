"""Интерфейс поиска похожих обращений и общие структуры данных."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from spdo.search.models import SearchIndexEntry


class LinkKind(StrEnum):
    """Предполагаемый вид связи, предлагаемый оператору."""

    DUPLICATE = "duplicate"
    RELATED = "related"


class SearchUnavailableError(RuntimeError):
    """Поиск временно недоступен (модель не загружена, индекс не построен)."""


@dataclass(frozen=True, slots=True)
class SearchScope:
    """Кому показывается выдача: от этого зависит видимость конфиденциальных.

    Attributes:
        viewer_id: Пользователь, для которого выполняется подбор, или
            ``None`` — без ограничения (офлайн-оценка).
        sees_confidential: Пользователь видит чужие конфиденциальные
            обращения (оператор, администратор).
    """

    viewer_id: int | None = None
    sees_confidential: bool = True

    def allows(self, author_id: int, confidential: bool) -> bool:
        """Проверяет, можно ли показать обращение в выдаче.

        Args:
            author_id: Автор обращения.
            confidential: Признак конфиденциальности.

        Returns:
            ``True``, если обращение не конфиденциальное, своё или
            пользователь видит конфиденциальные.
        """
        return not confidential or self.sees_confidential or author_id == self.viewer_id


@dataclass(frozen=True, slots=True)
class TicketQuery:
    """Данные нового обращения, по которым ищутся похожие.

    Attributes:
        title: Заголовок, до 200 символов.
        description: Описание, до 4000 символов.
        category_id: Идентификатор категории или ``None``.
        component_id: Идентификатор компонента или ``None``.
        scope: Видимость выдачи.
        exclude_ids: Обращения, которые не включаются в выдачу (например,
            само обращение при поиске по зарегистрированному).
    """

    title: str
    description: str
    category_id: int | None = None
    component_id: int | None = None
    scope: SearchScope = field(default_factory=SearchScope)
    exclude_ids: frozenset[int] = frozenset()

    @property
    def text(self) -> str:
        """Объединённый текст «заголовок + описание» для векторизации."""
        return f"{self.title}\n{self.description}".strip()


@dataclass(frozen=True, slots=True)
class IndexedTicket:
    """Обращение, передаваемое в поисковый индекс.

    Attributes:
        ticket_id: Номер обращения.
        title: Заголовок.
        description: Описание.
        category_id: Категория.
        component_id: Компонент.
        author_id: Автор.
        confidential: Признак конфиденциальности.
    """

    ticket_id: int
    title: str
    description: str
    category_id: int | None
    component_id: int | None
    author_id: int
    confidential: bool

    @property
    def text(self) -> str:
        """Объединённый текст «заголовок + описание»."""
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
    логика здесь: отсечение по порогам, определение вида связи и запись
    обращения в поисковый индекс (таблица ``search_index``).

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
    def search(self, session: Session, query: TicketQuery, limit: int) -> list[Candidate]:
        """Возвращает кандидатов, отсортированных по убыванию сходства.

        Args:
            session: Сессия БД.
            query: Данные нового обращения и видимость выдачи.
            limit: Максимальное число кандидатов, от 1 до 10.

        Returns:
            Кандидаты с коэффициентом сходства не ниже порога P2.

        Raises:
            SearchUnavailableError: Поиск временно недоступен.
        """

    def warm_up(self, session: Session) -> None:
        """Подготавливает поисковик к работе при запуске приложения.

        По умолчанию ничего не делает; наследники строят индекс или
        загружают модель, чтобы первый запрос пользователя не ждал.

        Args:
            session: Сессия БД.
        """
        return  # по умолчанию подготовка не требуется

    def index_values(self, ticket: IndexedTicket) -> dict[str, object]:
        """Возвращает дополнительные поля записи индекса.

        Наследники переопределяют метод, чтобы сохранить, например,
        векторное представление текста.

        Args:
            ticket: Индексируемое обращение.

        Returns:
            Значения полей ``search_index`` сверх общих.
        """
        return {}

    def index(self, session: Session, ticket: IndexedTicket) -> None:
        """Добавляет обращение в поисковый индекс или обновляет запись.

        Вызывается бизнес-модулем в транзакции регистрации или изменения
        обращения, поэтому индекс не расходится с реестром.

        Args:
            session: Сессия БД.
            ticket: Индексируемое обращение.
        """
        values = {
            # clock_timestamp(), а не now(): время записи, а не начала
            # транзакции, — синхронизация по дате изменения не пропустит
            # запись долгой транзакции.
            "updated_at": func.clock_timestamp(),
            "ticket_id": ticket.ticket_id,
            "text": ticket.text,
            "category_id": ticket.category_id,
            "component_id": ticket.component_id,
            "author_id": ticket.author_id,
            "confidential": ticket.confidential,
            **self.index_values(ticket),
        }
        changed = {k: v for k, v in values.items() if k != "ticket_id"}
        stmt = insert(SearchIndexEntry).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[SearchIndexEntry.ticket_id],
            set_=changed,
        )
        session.execute(stmt)

    def remove(self, session: Session, ticket_id: int) -> None:
        """Удаляет обращение из поискового индекса.

        Args:
            session: Сессия БД.
            ticket_id: Номер обращения.
        """
        session.execute(delete(SearchIndexEntry).where(SearchIndexEntry.ticket_id == ticket_id))

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
