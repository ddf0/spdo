"""Движок SQLAlchemy и фабрика сессий."""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from spdo.config import settings

#: Движок подключения к PostgreSQL.
engine = create_engine(settings.database_url, pool_pre_ping=True)

#: Фабрика сессий; фиксация транзакции выполняется явно.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """Выдаёт сессию на время обработки запроса.

    Зависимость FastAPI. Фиксацию выполняет обработчик запроса, чтобы
    изменение статуса, связи и истории попадало в одну транзакцию.
    Незафиксированные изменения откатываются при закрытии сессии.

    Yields:
        Сессия SQLAlchemy.
    """
    with SessionLocal() as session:
        yield session
