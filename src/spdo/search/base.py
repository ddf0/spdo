"""Интерфейс поиска похожих обращений и общие структуры данных."""

import dataclasses
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np
from sqlalchemy import case, delete, func, select
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


@dataclass(frozen=True, slots=True)
class Tuning:
    """Настраиваемые параметры подбора, заменяемые одним присваиванием.

    Пороги и веса хранятся вместе в неизменяемом объекте: подбор берёт его
    снимок в начале и не видит половину нового набора параметров, если
    администратор меняет их в это время (Ф16).

    Attributes:
        threshold_duplicate: Порог P1.
        threshold_related: Порог P2.
        weights: Веса составляющих S режима или ``None``.
    """

    threshold_duplicate: float
    threshold_related: float
    weights: Any = None


class SimilaritySearcher(ABC):
    """Базовый класс поиска похожих обращений.

    Наследники реализуют конкретный способ вычисления сходства. Общая
    логика здесь: отсечение по порогам, определение вида связи и запись
    обращения в поисковый индекс (таблица ``search_index``).

    Attributes:
        tuning: Действующие пороги и веса.
    """

    #: Идентификатор режима, сохраняемый в сеансе поиска.
    mode: str = "base"

    def __init__(self, threshold_duplicate: float, threshold_related: float) -> None:
        """Создаёт поисковик с заданными порогами.

        Args:
            threshold_duplicate: Порог P1 для «возможного дубликата».
            threshold_related: Порог P2 для «возможно связанного».
        """
        self.tuning = Tuning(threshold_duplicate, threshold_related)

    @property
    def threshold_duplicate(self) -> float:
        """Порог P1 действующих параметров."""
        return self.tuning.threshold_duplicate

    @threshold_duplicate.setter
    def threshold_duplicate(self, value: float) -> None:
        self.tuning = dataclasses.replace(self.tuning, threshold_duplicate=value)

    @property
    def threshold_related(self) -> float:
        """Порог P2 действующих параметров."""
        return self.tuning.threshold_related

    @threshold_related.setter
    def threshold_related(self, value: float) -> None:
        self.tuning = dataclasses.replace(self.tuning, threshold_related=value)

    @property
    def weights(self) -> Any:  # noqa: ANN401 — веса своего типа у каждого режима
        """Веса составляющих S действующих параметров."""
        return self.tuning.weights

    @weights.setter
    def weights(self, value: Any) -> None:  # noqa: ANN401
        self.tuning = dataclasses.replace(self.tuning, weights=value)

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

    def params(self, tuning: Tuning | None = None) -> dict[str, object]:
        """Возвращает параметры подбора для записи в сеанс поиска.

        Наследники дополняют пороги своими весами.

        Args:
            tuning: Снимок параметров; ``None`` — действующие.

        Returns:
            Пороги P1 и P2.
        """
        t = tuning or self.tuning
        return {
            "threshold_duplicate": t.threshold_duplicate,
            "threshold_related": t.threshold_related,
        }

    def pairwise(self, session: Session, ticket_ids: list[int]) -> np.ndarray:
        """Возвращает попарный коэффициент сходства S обращений индекса (Ф20).

        По умолчанию — подбором для каждого обращения среди остальных;
        режимы переопределяют метод матричным расчётом по той же формуле S.
        Видимость не ограничивается: метод вызывается для оператора.

        Args:
            session: Сессия БД.
            ticket_ids: Номера обращений.

        Returns:
            Симметричная матрица S в порядке номеров; диагональ — нули.

        Raises:
            SearchUnavailableError: Поиск временно недоступен.
        """
        pos = {tid: i for i, tid in enumerate(ticket_ids)}
        matrix = np.zeros((len(ticket_ids), len(ticket_ids)))
        for i, row in enumerate(index_rows(session, ticket_ids)):
            if row is None:
                continue
            query = TicketQuery(
                title=row.text,
                description="",
                category_id=row.category_id,
                component_id=row.component_id,
                exclude_ids=frozenset({ticket_ids[i]}),
            )
            for c in self.search(session, query, len(ticket_ids)):
                if c.ticket_id in pos:
                    matrix[i, pos[c.ticket_id]] = c.score
        np.fill_diagonal(matrix, 0.0)
        return np.maximum(matrix, matrix.T)

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
        self._write(session, ticket, self.index_values(ticket))

    def _write(self, session: Session, ticket: IndexedTicket, extra: dict[str, object]) -> None:
        """Выполняет вставку или обновление записи индекса.

        Если вектор не передан, а текст изменился, прежний вектор и модель
        сбрасываются: иначе гибридный режим использовал бы вектор чужого
        текста.
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
            **extra,
        }
        changed = {k: v for k, v in values.items() if k != "ticket_id"}
        stmt = insert(SearchIndexEntry).values(**values)
        if "embedding" not in extra:
            same_text = SearchIndexEntry.text == stmt.excluded.text
            changed["embedding"] = case((same_text, SearchIndexEntry.embedding), else_=None)
            changed["model_version"] = case((same_text, SearchIndexEntry.model_version), else_=None)
        stmt = stmt.on_conflict_do_update(index_elements=[SearchIndexEntry.ticket_id], set_=changed)
        session.execute(stmt)

    def remove(self, session: Session, ticket_id: int) -> None:
        """Удаляет обращение из поискового индекса.

        Args:
            session: Сессия БД.
            ticket_id: Номер обращения.
        """
        session.execute(delete(SearchIndexEntry).where(SearchIndexEntry.ticket_id == ticket_id))

    def classify(self, score: float, tuning: Tuning | None = None) -> LinkKind | None:
        """Определяет вид связи по двухпороговой схеме.

        Args:
            score: Итоговый коэффициент сходства.
            tuning: Снимок параметров подбора; ``None`` — действующие.

        Returns:
            ``DUPLICATE`` при S ≥ P1, ``RELATED`` при P2 ≤ S < P1,
            ``None`` — если кандидат не показывается.
        """
        t = tuning or self.tuning
        if score >= t.threshold_duplicate:
            return LinkKind.DUPLICATE
        if score >= t.threshold_related:
            return LinkKind.RELATED
        return None


def index_rows(session: Session, ticket_ids: list[int]) -> list:
    """Записи индекса в порядке номеров; отсутствующие — ``None``.

    Args:
        session: Сессия БД.
        ticket_ids: Номера обращений.

    Returns:
        Строки с полями ``text``, ``category_id``, ``component_id``,
        ``embedding``, ``model_version`` или ``None``.
    """
    stmt = select(
        SearchIndexEntry.ticket_id,
        SearchIndexEntry.text,
        SearchIndexEntry.category_id,
        SearchIndexEntry.component_id,
        SearchIndexEntry.embedding,
        SearchIndexEntry.model_version,
    ).where(SearchIndexEntry.ticket_id.in_(ticket_ids))
    rows = {r.ticket_id: r for r in session.execute(stmt)}
    return [rows.get(tid) for tid in ticket_ids]


def same_matrix(values: list[int | None]) -> np.ndarray:
    """Матрица совпадений значений справочника: 1, если оба заданы и равны.

    Args:
        values: Значения по порядку.

    Returns:
        Квадратная матрица из 0 и 1.
    """
    arr = np.asarray([-1 if v is None else v for v in values])
    return ((arr[:, None] == arr[None, :]) & (arr[:, None] >= 0)).astype(float)


def dictionary_matrices(rows: list) -> tuple[np.ndarray, np.ndarray]:
    """Матрицы совпадения категории и компонента записей индекса.

    Args:
        rows: Записи :func:`index_rows` (``None`` — нет в индексе).

    Returns:
        Пара матриц (категория, компонент).
    """
    cat = same_matrix([r.category_id if r else None for r in rows])
    comp = same_matrix([r.component_id if r else None for r in rows])
    return cat, comp


def finish_pairwise(score: np.ndarray, rows: list) -> np.ndarray:
    """Обнуляет диагональ и строки обращений, которых нет в индексе.

    Args:
        score: Матрица S.
        rows: Записи :func:`index_rows`.

    Returns:
        Та же матрица.
    """
    absent = np.asarray([r is None for r in rows])
    score[absent, :] = 0.0
    score[:, absent] = 0.0
    np.fill_diagonal(score, 0.0)
    return score


def texts_of(rows: list) -> list[str]:
    """Тексты записей индекса; отсутствующие — пустые строки.

    Args:
        rows: Записи :func:`index_rows`.

    Returns:
        Тексты.
    """
    return [r.text if r else "" for r in rows]
