"""Конфигурация приложения.

Все параметры читаются из переменных окружения с префиксом ``SPDO_``
или из файла ``.env``. Константы порогов и весов в коде не допускаются.
"""

import secrets
from enum import StrEnum

from pydantic import Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Значения ключа подписи сессий, считающиеся незаданными.
UNSET_SECRET_KEYS = frozenset({"", "change-me"})


class SearchMode(StrEnum):
    """Режим вычисления сходства."""

    LEXICAL = "lexical"
    HYBRID = "hybrid"


class Settings(BaseSettings):
    """Параметры приложения и поиска похожих обращений.

    Attributes:
        database_url: Строка подключения SQLAlchemy к PostgreSQL.
        secret_key: Ключ подписи сессий. Если не задан, при запуске
            генерируется случайный: подделать сессию нельзя, но сессии
            сбрасываются при перезапуске.
        session_max_age: Время жизни сессии пользователя, секунды.
        session_https_only: Передавать cookie сессии только по HTTPS.
        password_rounds: Стоимость bcrypt (логарифм числа раундов).
        embedding_model: Идентификатор модели sentence-transformers.
        embedding_dim: Размерность векторного представления модели; при
            смене модели требуется миграция столбца и переиндексация.
        search_mode: Базовый (лексический) или гибридный режим.
        threshold_duplicate: Порог P1 — «возможный дубликат».
        threshold_related: Порог P2 — «возможно связанное».
        top_n: Максимальное число рекомендаций N.
        w_semantic: Вес семантического сходства w1.
        w_lexical: Вес лексического сходства w2.
        w_category: Вес совпадения категории w3.
        w_component: Вес совпадения компонента w4.
        resolved_close_days: Срок в днях, после которого решённое, но не
            подтверждённое автором обращение может быть закрыто командой
            ``spdo close-expired``. В ТЗ значение не задано; 14 дней —
            допущение разработчиков.
    """

    model_config = SettingsConfigDict(env_prefix="SPDO_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://spdo:spdo@localhost:5432/spdo"
    secret_key: str = ""
    session_max_age: int = 8 * 60 * 60
    session_https_only: bool = False
    password_rounds: int = 12
    embedding_model: str = "cointegrated/rubert-tiny2"
    embedding_dim: int = 312
    search_mode: SearchMode = SearchMode.HYBRID
    threshold_duplicate: float = 0.80
    threshold_related: float = 0.60
    top_n: int = 10
    w_semantic: float = 0.55
    w_lexical: float = 0.15
    w_category: float = 0.15
    w_component: float = 0.15
    resolved_close_days: int = Field(default=14, ge=1)

    _secret_key_generated: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _check_search_params(self) -> "Settings":
        """Проверяет согласованность порогов, числа результатов и весов.

        Returns:
            Проверенный экземпляр настроек.

        Raises:
            ValueError: Нарушено условие 0 < P2 < P1 ≤ 1, 1 ≤ N ≤ 10
                или веса отрицательны либо не дают в сумме 1.
        """
        if not 0 < self.threshold_related < self.threshold_duplicate <= 1:
            raise ValueError("Требуется 0 < P2 < P1 <= 1")
        if not 1 <= self.top_n <= 10:
            raise ValueError("Требуется 1 <= N <= 10")
        weights = (self.w_semantic, self.w_lexical, self.w_category, self.w_component)
        if min(weights) < 0 or abs(sum(weights) - 1) > 1e-6:
            raise ValueError("Веса должны быть неотрицательны, их сумма равна 1")
        return self

    @model_validator(mode="after")
    def _ensure_secret_key(self) -> "Settings":
        """Заменяет незаданный ключ подписи сессий случайным.

        Известный всем ключ по умолчанию позволил бы подписать cookie с
        любым идентификатором пользователя, поэтому он не используется.

        Returns:
            Настройки с непредсказуемым ключом.
        """
        if self.secret_key in UNSET_SECRET_KEYS:
            self.secret_key = secrets.token_urlsafe(48)
            self._secret_key_generated = True
        return self

    @property
    def secret_key_generated(self) -> bool:
        """Ключ подписи сессий сгенерирован при запуске, а не задан явно."""
        return self._secret_key_generated


settings = Settings()
