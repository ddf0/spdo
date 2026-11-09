"""Конфигурация приложения.

Все параметры читаются из переменных окружения с префиксом ``SPDO_``
или из файла ``.env``. Константы порогов и весов в коде не допускаются.
"""

import math
import secrets
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, PrivateAttr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Соединения сверх рабочих потоков: фоновая сборка и синхронизация
#: лексического индекса читают зафиксированные записи отдельной сессией.
BACKGROUND_CONNECTIONS = 3

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
        worker_threads: Число потоков, в которых выполняются синхронные
            обработчики запросов (пул потоков anyio).
        db_pool_size: Постоянные соединения пула БД.
        db_max_overflow: Дополнительные соединения пула сверх постоянных.
            Пул должен быть не меньше ``worker_threads`` плюс
            :data:`BACKGROUND_CONNECTIONS`: иначе под нагрузкой все потоки
            ждут соединение, а соединения держат запросы, ожидающие поток
            для сериализации ответа, — до тайм-аута пула ни один запрос не
            завершается (выявлено нагрузочным испытанием на 50 соединениях).
            Условие исключает такую остановку, пока одновременных запросов
            не больше размера пула; при большем числе запросы снова ждут
            соединение до тайм-аута, поэтому за обратным прокси или
            параметром uvicorn ``--limit-concurrency`` число одновременных
            запросов ограничивается. Пул одного процесса (по умолчанию до 50
            соединений) должен помещаться в ``max_connections`` PostgreSQL
            (по умолчанию 100) вместе с командами ``spdo`` и миграциями.
        login_max_failures: Число неудачных входов по одному имени
            пользователя за окно, после которого вход по нему временно
            запрещён.
        login_max_failures_ip: То же для одного IP-адреса (выше: с одного
            адреса могут входить несколько человек); 0 — не ограничивать.
            За обратным прокси без ``--proxy-headers`` все запросы приходят
            с адреса прокси — ограничение по адресу тогда нужно отключить.
            Пять неверных паролей временно блокируют вход по имени и его
            владельцу — это осознанная плата за защиту от перебора.
        login_window_seconds: Окно подсчёта неудачных входов и срок
            запрета, секунды.
        embedding_model: Идентификатор модели sentence-transformers.
        embedding_device: Устройство вычисления векторов (``cpu``, ``cuda``).
        embedding_max_tokens: Наибольшая длина текста для модели в токенах.
        embedding_threads: Число потоков torch на CPU; 0 — по умолчанию.
        embedding_allow_download: Разрешить скачивание модели при работе
            (по умолчанию модель берётся только из локального кеша).
        embedding_dim: Размерность векторного представления модели; при
            смене модели требуется миграция столбца и переиндексация.
        search_mode: Базовый (лексический) или гибридный режим.
        threshold_duplicate: Порог P1 — «возможный дубликат».
        threshold_related: Порог P2 — «возможно связанное».
        top_n: Максимальное число рекомендаций N.
        w_semantic: Вес семантического сходства w1.
        w_lexical: Вес лексического сходства w2.
        w_category: Вес совпадения категории w3.
        w_component: Вес совпадения компонента w4. Начальные значения ТЗ
            (w = 0,55 / 0,15 / 0,15 / 0,15; P1 = 0,80; P2 = 0,60) заменены
            подобранными командой ``spdo tune --mode hybrid`` на настроечной
            части эталонного набора: при начальных значениях recall@3 ниже
            базового режима, а пороги не соответствуют шкале косинуса модели.
        lexical_w_lexical: Вес лексического сходства в базовом режиме (w1 = 0).
        lexical_w_category: Вес совпадения категории в базовом режиме.
        lexical_w_component: Вес совпадения компонента в базовом режиме.
        lexical_threshold_duplicate: Порог P1 базового режима.
        lexical_threshold_related: Порог P2 базового режима. Шкала косинуса
            TF-IDF ниже семантической, поэтому пороги режимов различаются.
            Значения по умолчанию весов и порогов базового режима подобраны
            командой ``spdo tune`` на настроечной части эталонного набора.
            P1 обоих режимов подбирается по наибольшему F1 решения
            «дубликат», P2 — как порог показа: наименьший порог, при котором
            не меньше половины показанных кандидатов — дубликаты или
            тематически близкие (``spdo.analytics.tuning``). По сравнению с
            прежним подбором P2 по F1 «показать» на проверочной части
            recall@3 среди показанных вырос с 0,844 до 0,875 в гибридном
            режиме (доля ложных «дубликатов» прежняя, 19,2 %) и с 0,688 до
            0,729 в базовом (доля ложных «дубликатов» выросла с 26,9 % до
            30,8 % из-за снижения P1 базового режима с 0,23 до 0,21).
        search_warm_up: Строить поисковый индекс в фоне при запуске
            приложения (в тестах отключается).
        candidate_pool: Число кандидатов с наибольшим текстовым сходством,
            для которых вычисляется итоговый S.
        resolved_close_days: Срок в днях, после которого решённое, но не
            подтверждённое автором обращение может быть закрыто командой
            ``spdo close-expired``. В ТЗ значение не задано; 14 дней —
            допущение разработчиков.
        incident_window_hours: Окно поиска массовых обращений, часов (Ф20).
        incident_min_size: Наименьшее число однотипных обращений в окне,
            при котором они предлагаются к объединению в инцидент.
        incident_threshold_offset: Надбавка к порогу P1 действующего режима
            для группировки массовых обращений. P1 настроен для показа
            рекомендаций человеку и для автоматической группировки мягок:
            на эталонном наборе (гибридный режим) при пороге P1 = 0,53
            чистота групп 0,84, при P1 + 0,03 = 0,56 — 0,90 при охвате 71
            обращения из 89.
        incident_max_tickets: Наибольшее число свежих обращений, среди
            которых ищутся группы (ограничивает время расчёта). Значения по
            умолчанию — допущения разработчиков, в ТЗ не заданы.
        display_timezone: Часовой пояс дат в интерфейсе (имя IANA); в БД
            даты хранятся в UTC.
    """

    model_config = SettingsConfigDict(env_prefix="SPDO_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://spdo:spdo@localhost:5432/spdo"
    secret_key: str = ""
    session_max_age: int = 8 * 60 * 60
    session_https_only: bool = False
    password_rounds: int = 12
    worker_threads: int = Field(default=40, ge=1, le=200)
    db_pool_size: int = Field(default=20, ge=1)
    db_max_overflow: int = Field(default=30, ge=0)
    login_max_failures: int = Field(default=5, ge=1)
    login_max_failures_ip: int = Field(default=30, ge=0)
    login_window_seconds: int = Field(default=900, ge=1)
    embedding_model: str = "cointegrated/rubert-tiny2"
    embedding_dim: int = Field(default=312, ge=1, le=2000)
    embedding_device: str = "cpu"
    embedding_max_tokens: int = Field(default=512, ge=32, le=2048)
    embedding_threads: int = Field(default=0, ge=0)
    embedding_allow_download: bool = False
    search_mode: SearchMode = SearchMode.HYBRID
    threshold_duplicate: float = 0.53
    threshold_related: float = 0.46
    top_n: int = 10
    w_semantic: float = 0.55
    w_lexical: float = 0.40
    w_category: float = 0.025
    w_component: float = 0.025
    lexical_w_lexical: float = 1.0
    lexical_w_category: float = 0.0
    lexical_w_component: float = 0.0
    lexical_threshold_duplicate: float = 0.21
    lexical_threshold_related: float = 0.16
    candidate_pool: int = Field(default=50, ge=10, le=500)
    search_warm_up: bool = True
    resolved_close_days: int = Field(default=14, ge=1)
    incident_window_hours: int = Field(default=24, ge=1, le=24 * 30)
    incident_min_size: int = Field(default=3, ge=2)
    incident_threshold_offset: float = Field(default=0.03, ge=0.0, le=0.5)
    incident_max_tickets: int = Field(default=500, ge=2, le=5000)
    display_timezone: str = "Europe/Moscow"

    _secret_key_generated: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _check_search_params(self) -> "Settings":
        """Проверяет согласованность порогов, числа результатов и весов.

        Returns:
            Проверенный экземпляр настроек.

        Raises:
            ValueError: Нарушено условие 0 < P2 < P1 ≤ 1, 1 ≤ N ≤ 10,
                веса не конечны, отрицательны либо не дают в сумме 1,
                часовой пояс неизвестен или пул БД меньше пула потоков.
        """
        for p1, p2 in (
            (self.threshold_duplicate, self.threshold_related),
            (self.lexical_threshold_duplicate, self.lexical_threshold_related),
        ):
            if not 0 < p2 < p1 <= 1:
                raise ValueError("Требуется 0 < P2 < P1 <= 1")
        try:
            ZoneInfo(self.display_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Неизвестный часовой пояс {self.display_timezone}") from exc
        if self.db_pool_size + self.db_max_overflow < self.worker_threads + BACKGROUND_CONNECTIONS:
            raise ValueError(
                "Пул БД (db_pool_size + db_max_overflow) должен быть не меньше "
                f"worker_threads + {BACKGROUND_CONNECTIONS}"
            )
        if not 1 <= self.top_n <= 10:
            raise ValueError("Требуется 1 <= N <= 10")
        hybrid = (self.w_semantic, self.w_lexical, self.w_category, self.w_component)
        lexical = (self.lexical_w_lexical, self.lexical_w_category, self.lexical_w_component)
        for weights in (hybrid, lexical):
            # NaN не проходит ни одно сравнение, поэтому проверяется явно.
            if not all(map(math.isfinite, weights)):
                raise ValueError("Веса должны быть конечными числами")
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
