"""Маршруты API подбора, рекомендаций, регистрации, карточки и оценки.

Карточка обращения и оценка решения здесь, а не в ``tickets``: доступ к
чужому обращению зависит от того, показывалось ли оно в рекомендациях.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from spdo.recommendations import service
from spdo.recommendations.models import Recommendation
from spdo.recommendations.schemas import (
    CardOut,
    RatingIn,
    RecommendationOut,
    RegistrationRequest,
    RegistrationResponse,
    SearchRequest,
    SearchResponse,
)
from spdo.relations.service import RelationError
from spdo.tickets.access import TicketNotFoundError, TicketValidationError
from spdo.tickets.transitions import TransitionError
from spdo.users.deps import CurrentUser, DbSession, verify_csrf

#: Пометка в выдаче: решение принимает человек (ТЗ, раздел «Алгоритм»).
AUTOMATIC_NOTICE = "Рекомендации сформированы автоматически и могут быть ошибочными."

router = APIRouter(prefix="/api", dependencies=[Depends(verify_csrf)])


def _http_error(exc: Exception) -> HTTPException:
    """Переводит ошибку предметной области в ответ HTTP."""
    if isinstance(exc, TicketNotFoundError | service.RecommendationNotFoundError):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(
        exc, service.VerdictError | service.SessionUsedError | RelationError | TransitionError
    ):
        code = status.HTTP_409_CONFLICT  # конфликт состояния
    else:  # неверные данные запроса, в том числе рекомендация не из сеанса
        code = status.HTTP_400_BAD_REQUEST
    return HTTPException(code, str(exc))


def _out(session: Session, user: CurrentUser, rec: Recommendation) -> RecommendationOut:
    """Рекомендация с данными найденного обращения из его карточки."""
    card = service.card(session, user, rec.ticket_id)
    return RecommendationOut(
        id=rec.id,
        position=rec.position,
        ticket_id=rec.ticket_id,
        title=card.title,
        status=card.status.value,
        category=card.category,
        score=rec.score,
        kind=rec.suggested_kind.value,
        components={"sem": rec.s_sem, "lex": rec.s_lex, "cat": rec.s_cat, "comp": rec.s_comp},
        explanation=rec.explanation,
        verdict=rec.verdict.value,
    )


@router.post("/search")
def search(body: SearchRequest, session: DbSession, user: CurrentUser) -> SearchResponse:
    """Подбирает похожие обращения и фиксирует сеанс поиска (Ф3, Ф4).

    Args:
        body: Введённые заголовок, описание, категория, компонент.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Сеанс и рекомендации; при недоступности поиска — предупреждение.

    Raises:
        HTTPException: 400 — пустой или слишком длинный текст, неизвестная
            категория или компонент.
    """
    try:
        outcome = service.run_search(
            session,
            user,
            title=body.title,
            description=body.description,
            category_id=body.category_id,
            component_id=body.component_id,
        )
    except TicketValidationError as exc:
        session.rollback()
        raise _http_error(exc) from exc
    recs = []
    for rec in outcome.recommendations:
        try:
            recs.append(_out(session, user, rec))
        except TicketNotFoundError:  # стало недоступным между подбором и ответом
            continue
    session.commit()
    return SearchResponse(
        session_id=outcome.search_session.id,
        unavailable=outcome.unavailable,
        notice=outcome.notice,
        automatic_notice=AUTOMATIC_NOTICE,
        recommendations=recs,
    )


@router.post("/recommendations/{recommendation_id}/reject")
def reject(recommendation_id: int, session: DbSession, user: CurrentUser) -> RecommendationOut:
    """Отклоняет рекомендацию (Ф9).

    Args:
        recommendation_id: Номер рекомендации.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Рекомендация с вердиктом.

    Raises:
        HTTPException: 404 — недоступна, 409 — уже принята.
    """
    try:
        rec = service.reject(session, user, recommendation_id)
        out = _out(session, user, rec)
    except (service.RecommendationError, TicketNotFoundError) as exc:
        session.rollback()
        raise _http_error(exc) from exc
    session.commit()
    return out


@router.post("/registrations", status_code=status.HTTP_201_CREATED)
def register(
    body: RegistrationRequest, session: DbSession, user: CurrentUser
) -> RegistrationResponse:
    """Регистрирует обращение по итогам подбора (ПЦ1; Ф1, Ф7, Ф8, Ф10).

    Args:
        body: Данные обращения, сеанс поиска и выбранная рекомендация.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Номер и статус обращения.

    Raises:
        HTTPException: 400 — неверные данные или рекомендация не из сеанса,
            404 — сеанс или рекомендация недоступны, 409 — сеанс уже
            использован, рекомендация отклонена или связь невозможна.
    """
    link = None
    if body.link is not None:
        link = service.LinkChoice(body.link.recommendation_id, body.link.kind)
    try:
        ticket = service.register(
            session,
            user,
            title=body.title,
            description=body.description,
            category_id=body.category_id,
            component_id=body.component_id,
            session_id=body.session_id,
            draft_id=body.draft_id,
            link=link,
        )
    except (
        TicketValidationError,
        TicketNotFoundError,
        service.RecommendationError,
        RelationError,
        TransitionError,
    ) as exc:
        session.rollback()
        raise _http_error(exc) from exc
    session.commit()
    return RegistrationResponse(ticket_id=ticket.id, status=ticket.status.value)


@router.get("/tickets/{ticket_id}/card")
def ticket_card(ticket_id: int, session: DbSession, user: CurrentUser) -> CardOut:
    """Возвращает карточку обращения с учётом прав (Ф5).

    Args:
        ticket_id: Номер обращения.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Карточка.

    Raises:
        HTTPException: 404 — обращение недоступно: чужое видно, только если
            показывалось пользователю в рекомендациях.
    """
    try:
        c = service.card(session, user, ticket_id)
    except TicketNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return CardOut(
        ticket_id=c.ticket_id,
        title=c.title,
        status=c.status.value,
        category=c.category,
        component=c.component,
        full=c.full,
        description=c.description,
        solution_id=c.solution_id,
        solution_text=c.solution_text,
        my_rating=c.my_rating,
    )


@router.put("/solutions/{solution_id}/rating", status_code=status.HTTP_204_NO_CONTENT)
def rate_solution(
    solution_id: int, body: RatingIn, session: DbSession, user: CurrentUser
) -> Response:
    """Сохраняет оценку решения «помогло / не помогло» (Ф6).

    Args:
        solution_id: Номер решения.
        body: Оценка.
        session: Сессия БД.
        user: Текущий пользователь.

    Returns:
        Пустой ответ 204.

    Raises:
        HTTPException: 404 — решение недоступно.
    """
    try:
        service.rate(session, user, solution_id, body.helped)
    except TicketNotFoundError as exc:
        session.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
