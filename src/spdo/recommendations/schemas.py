"""Схемы запросов и ответов API подбора, рекомендаций и регистрации."""

from pydantic import BaseModel, Field

from spdo.relations.models import RelationKind
from spdo.tickets.models import DESCRIPTION_MAX, TITLE_MAX


class SearchRequest(BaseModel):
    """Запрос подбора похожих обращений по введённому тексту.

    Attributes:
        title: Заголовок.
        description: Описание.
        category_id: Категория или ``None``.
        component_id: Компонент или ``None``.
    """

    title: str = Field(default="", max_length=TITLE_MAX)
    description: str = Field(default="", max_length=DESCRIPTION_MAX)
    category_id: int | None = None
    component_id: int | None = None


class RecommendationOut(BaseModel):
    """Рекомендация в выдаче (Ф4).

    Attributes:
        id: Номер рекомендации.
        position: Позиция в выдаче.
        ticket_id: Номер найденного обращения.
        title: Заголовок найденного обращения.
        status: Статус найденного обращения.
        category: Категория найденного обращения.
        score: Коэффициент сходства S.
        kind: Предложенный вид связи.
        components: Составляющие S.
        explanation: Объяснение сходства.
        verdict: Вердикт.
    """

    id: int
    position: int
    ticket_id: int
    title: str
    status: str
    category: str
    score: float
    kind: str
    components: dict[str, float]
    explanation: str
    verdict: str


class SearchResponse(BaseModel):
    """Результат подбора.

    Attributes:
        session_id: Номер сеанса поиска.
        unavailable: Подбор не выполнен.
        notice: Предупреждение пользователю.
        automatic_notice: Пометка, что рекомендации автоматические (ТЗ).
        recommendations: Рекомендации по позициям.
    """

    session_id: int
    unavailable: bool
    notice: str | None
    automatic_notice: str
    recommendations: list[RecommendationOut]


class LinkIn(BaseModel):
    """Выбор рекомендации при регистрации.

    Attributes:
        recommendation_id: Номер рекомендации.
        kind: Вид связи.
    """

    recommendation_id: int
    kind: RelationKind


class RegistrationRequest(BaseModel):
    """Регистрация обращения по итогам подбора.

    Attributes:
        title: Заголовок.
        description: Описание.
        category_id: Категория.
        component_id: Компонент.
        session_id: Сеанс поиска или ``None``.
        draft_id: Черновик или ``None``.
        link: Принятая рекомендация или ``None`` — независимое обращение.
    """

    title: str = Field(max_length=TITLE_MAX)
    description: str = Field(max_length=DESCRIPTION_MAX)
    category_id: int
    component_id: int
    session_id: int | None = None
    draft_id: int | None = None
    link: LinkIn | None = None


class RegistrationResponse(BaseModel):
    """Зарегистрированное обращение.

    Attributes:
        ticket_id: Номер обращения.
        status: Статус после регистрации.
    """

    ticket_id: int
    status: str


class CardOut(BaseModel):
    """Карточка обращения (Ф5).

    Attributes:
        ticket_id: Номер обращения.
        title: Заголовок.
        status: Статус.
        category: Категория.
        component: Компонент (только при полном показе).
        full: Показана полностью.
        description: Описание (только при полном показе).
        solution_id: Номер опубликованного решения.
        solution_text: Текст опубликованного решения.
        my_rating: Оценка решения текущим пользователем.
    """

    ticket_id: int
    title: str
    status: str
    category: str
    component: str | None
    full: bool
    description: str | None
    solution_id: int | None
    solution_text: str | None
    my_rating: bool | None


class RatingIn(BaseModel):
    """Оценка решения.

    Attributes:
        helped: ``True`` — помогло, ``False`` — не помогло.
    """

    helped: bool
