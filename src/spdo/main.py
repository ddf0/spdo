"""Точка входа веб-приложения FastAPI."""

import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import DataError
from starlette.middleware.sessions import SessionMiddleware

from spdo import __version__, logsafe
from spdo.config import settings
from spdo.db import models  # noqa: F401  регистрирует таблицы всех модулей
from spdo.db.session import SessionLocal
from spdo.recommendations.routes import router as recommendations_router
from spdo.search import params
from spdo.search.factory import get_searcher
from spdo.tickets.routes import router as tickets_router
from spdo.users.deps import NotAuthenticatedError
from spdo.users.routes import router as users_router
from spdo.web.admin import router as web_admin_router
from spdo.web.incidents import router as web_incidents_router
from spdo.web.notifications import router as web_notifications_router
from spdo.web.routes import router as web_router
from spdo.web.stats import router as web_stats_router
from spdo.web.templating import STATIC_DIR
from spdo.web.tickets import router as web_tickets_router


def _warm_up_search() -> None:
    """Прогревает поисковик в фоне; ошибка прогрева не мешает запуску."""
    try:
        with SessionLocal() as session:
            get_searcher().warm_up(session)
    except Exception:  # noqa: BLE001 — недоступность поиска не блокирует работу (ТЗ)
        logging.getLogger(__name__).exception("Не удалось подготовить поиск при запуске")


def _load_search_params() -> None:
    """Применяет параметры поиска, заданные администратором (Ф16), до первого запроса.

    Ошибка БД (например, миграция ещё не применена) не мешает запуску:
    действуют параметры окружения.
    """
    try:
        with SessionLocal() as session:
            params.load_and_apply(session)
    except Exception:  # noqa: BLE001 — запуск не должен зависеть от настроек
        logging.getLogger(__name__).exception("Не удалось загрузить параметры поиска")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Подключает фильтр секретов к журналам и прогревает поиск в фоне.

    Предупреждает, если ключ подписи сессий не задан и сгенерирован.

    Yields:
        Управление приложению на время работы.
    """
    logsafe.install()
    _load_search_params()
    if settings.search_warm_up:
        threading.Thread(target=_warm_up_search, name="search-warm-up", daemon=True).start()
    if settings.secret_key_generated:
        logging.getLogger(__name__).warning(
            "SPDO_SECRET_KEY не задан: ключ случайный, сессии сбросятся при перезапуске"
        )
    yield


app = FastAPI(title="СПДО", version=__version__, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="spdo_session",
    max_age=settings.session_max_age,
    same_site="lax",
    https_only=settings.session_https_only,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


#: Заголовки, добавляемые ко всем ответам.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


@app.middleware("http")
async def security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Добавляет защитные заголовки и запрещает кеширование страниц и API.

    Страницы содержат CSRF-токен, ответы API — персональные данные,
    поэтому их нельзя сохранять в кеше браузера или прокси.

    Args:
        request: Текущий запрос.
        call_next: Следующий обработчик цепочки.

    Returns:
        Ответ с добавленными заголовками.
    """
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(users_router)
app.include_router(recommendations_router)
app.include_router(tickets_router)
app.include_router(web_router)
app.include_router(web_tickets_router)
app.include_router(web_stats_router)
app.include_router(web_admin_router)
app.include_router(web_incidents_router)
app.include_router(web_notifications_router)


@app.exception_handler(NotAuthenticatedError)
def not_authenticated(request: Request, _: NotAuthenticatedError) -> Response:
    """Отвечает на запрос без входа в систему.

    Браузер перенаправляется на страницу входа с возвратом на исходный
    адрес; клиент API получает 401.

    Args:
        request: Текущий запрос.
        _: Исключение.

    Returns:
        Перенаправление или ответ 401.
    """
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Требуется вход"}, status_code=status.HTTP_401_UNAUTHORIZED)
    target = quote(request.url.path, safe="/")
    return RedirectResponse(f"/login?next={target}", status_code=status.HTTP_303_SEE_OTHER)


@app.exception_handler(DataError)
def invalid_data(request: Request, exc: DataError) -> Response:
    """Отвечает 400 на ввод, который БД не может сохранить или сравнить.

    Например, символ с кодом 0 в тексте (PostgreSQL его не допускает) или
    число вне диапазона столбца. Без обработчика такой ввод давал бы 500.

    Args:
        request: Текущий запрос.
        exc: Ошибка данных от драйвера БД.

    Returns:
        Ответ 400.
    """
    logging.getLogger(__name__).warning(
        "Недопустимые данные в запросе %s: %s", request.url.path, type(exc.orig).__name__
    )
    return JSONResponse(
        {"detail": "Введённые данные содержат недопустимые символы или значения"},
        status_code=status.HTTP_400_BAD_REQUEST,
    )


@app.get("/health")
def health() -> dict[str, str]:
    """Проверка работоспособности сервиса.

    Returns:
        Статус и версия приложения.
    """
    return {"status": "ok", "version": __version__}
