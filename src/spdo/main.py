"""Точка входа веб-приложения FastAPI."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from spdo import __version__, logsafe
from spdo.config import settings
from spdo.db import models  # noqa: F401  регистрирует таблицы всех модулей
from spdo.users.deps import NotAuthenticatedError
from spdo.users.routes import router as users_router
from spdo.web.routes import router as web_router
from spdo.web.templating import STATIC_DIR


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Подключает фильтр секретов к журналам после настройки uvicorn.

    Предупреждает, если ключ подписи сессий не задан и сгенерирован.

    Yields:
        Управление приложению на время работы.
    """
    logsafe.install()
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
app.include_router(web_router)


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


@app.get("/health")
def health() -> dict[str, str]:
    """Проверка работоспособности сервиса.

    Returns:
        Статус и версия приложения.
    """
    return {"status": "ok", "version": __version__}
