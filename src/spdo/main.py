"""Точка входа веб-приложения FastAPI."""

from fastapi import FastAPI

from spdo import __version__

app = FastAPI(title="СПДО", version=__version__)


@app.get("/health")
def health() -> dict[str, str]:
    """Проверка работоспособности сервиса.

    Returns:
        Статус и версия приложения.
    """
    return {"status": "ok", "version": __version__}
