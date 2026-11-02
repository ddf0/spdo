"""Базовый режим: поиск по лексическому сходству (TF-IDF).

Поисковик держит индекс :class:`~spdo.search.lexical_index.LexicalIndex` в
памяти процесса и догоняет таблицу ``search_index`` по дате изменения
записей.

Производительность на 10 000 обращений: подбор — десятки миллисекунд,
полная сборка индекса — десятки секунд. Поэтому подбор всегда читает
готовый неизменяемый снимок индекса, а полная сборка выполняется в фоне:
при запуске (:meth:`LexicalSearcher.warm_up`), при росте корпуса больше чем
на :data:`~spdo.search.lexical_index.REFIT_GROWTH` и раз в
:data:`RECONCILE_PERIOD` для сверки с БД. Между сборками новые и изменённые
обращения добавляются за миллисекунды.

Каждый рабочий процесс сервера держит свой индекс (около 200–400 МБ на
10 000 обращений), поэтому рекомендуется один процесс uvicorn.

Классы индекса реэкспортируются отсюда для обратной совместимости импортов.
"""

import dataclasses
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from spdo.search.base import (
    Candidate,
    SearchUnavailableError,
    SimilaritySearcher,
    TicketQuery,
)
from spdo.search.lexical_index import (
    REFIT_GROWTH,
    Document,
    LexicalIndex,
    LexicalWeights,
    normalize,
)
from spdo.search.models import SearchIndexEntry

__all__ = [
    "ALGORITHM_VERSION",
    "REFIT_GROWTH",
    "Document",
    "LexicalIndex",
    "LexicalSearcher",
    "LexicalWeights",
    "normalize",
]

#: Версия алгоритма, записываемая в сеанс поиска.
ALGORITHM_VERSION = "tfidf-charwb-3-5-v1"
#: Окно повторного чтения изменённых записей: транзакция с более ранней
#: датой изменения может зафиксироваться позже предыдущей синхронизации.
SYNC_OVERLAP = timedelta(minutes=2)
#: Наименьший интервал между инкрементальными синхронизациями, секунды.
SYNC_INTERVAL = 1.0
#: Период полной сверки индекса с БД в фоне, секунды: подбирает записи,
#: пропущенные долгими транзакциями, и удалённые из индекса.
RECONCILE_PERIOD = 600.0


#: Столбцы индекса, читаемые при синхронизации (вектор не нужен).
_COLUMNS = (
    SearchIndexEntry.ticket_id,
    SearchIndexEntry.text,
    SearchIndexEntry.category_id,
    SearchIndexEntry.component_id,
    SearchIndexEntry.author_id,
    SearchIndexEntry.confidential,
    SearchIndexEntry.updated_at,
)


class LexicalSearcher(SimilaritySearcher):
    """Поиск по лексическому сходству текстов.

    Производный класс :class:`~spdo.search.base.SimilaritySearcher`.
    Используется как базовый режим для сравнения с гибридным (w1 = 0).
    Переопределяет :meth:`search` и :meth:`warm_up`.

    Подбор читает текущий снимок индекса без ожидания. Перед подбором (не
    чаще раза в :data:`SYNC_INTERVAL`) применяются записи ``search_index``,
    изменившиеся с прошлой синхронизации; учитываются только более новые
    версии записей, поэтому окно перекрытия не плодит копии. Полная сборка
    и сверка с БД выполняются в фоновом потоке, затем снимок заменяется.
    Чтение БД идёт через отдельное соединение и видит только
    зафиксированные записи.

    Attributes:
        weights: Веса составляющих S в лексическом режиме.
        pool: Размер пула кандидатов до ранжирования по S.
    """

    mode = "lexical"

    def __init__(
        self,
        threshold_duplicate: float,
        threshold_related: float,
        weights: LexicalWeights,
        pool: int,
    ) -> None:
        """Создаёт лексический поисковик.

        Args:
            threshold_duplicate: Порог P1.
            threshold_related: Порог P2.
            weights: Веса составляющих S.
            pool: Размер пула кандидатов.
        """
        super().__init__(threshold_duplicate, threshold_related)
        self.weights = weights
        self.pool = pool
        self._index: LexicalIndex | None = None
        self._seen: dict[int, datetime] = {}
        self._watermark: datetime | None = None
        self._sync_lock = threading.Lock()
        self._rebuild_lock = threading.Lock()
        self._last_sync = 0.0
        self._last_full = 0.0

    @property
    def model_version(self) -> str:
        """Версия алгоритма лексического поиска."""
        return ALGORITHM_VERSION

    def params(self) -> dict[str, object]:
        """Пороги и веса базового режима для сеанса поиска.

        Returns:
            Пороги P1, P2 и веса составляющих.
        """
        return {**super().params(), "weights": dataclasses.asdict(self.weights)}

    @staticmethod
    def _engine(session: Session) -> Engine:
        """Движок, через который читаются только зафиксированные записи."""
        bind = session.get_bind()
        return getattr(bind, "engine", bind)

    @staticmethod
    def _read(engine: Engine, since: datetime | None = None) -> list:
        """Читает записи индекса, изменённые после ``since`` (или все)."""
        stmt = select(*_COLUMNS)
        if since is not None:
            stmt = stmt.where(SearchIndexEntry.updated_at > since)
        with Session(engine) as committed:
            return list(committed.execute(stmt))

    @staticmethod
    def _document(row) -> Document:  # noqa: ANN001 — строка результата SQLAlchemy
        """Документ индекса из строки ``search_index``."""
        return Document(
            row.ticket_id,
            row.text,
            row.category_id,
            row.component_id,
            row.author_id,
            row.confidential,
        )

    def rebuild(self, engine: Engine) -> None:
        """Строит индекс заново по всем записям и заменяет снимок.

        Одновременно выполняется не более одной сборки; подбор в это время
        продолжает работать со старым снимком.

        Args:
            engine: Движок БД.
        """
        with self._rebuild_lock:
            rows = self._read(engine)
            index = LexicalIndex.build([self._document(r) for r in rows])
            seen = {r.ticket_id: r.updated_at for r in rows}
            watermark = max(seen.values(), default=None)
            with self._sync_lock:
                self._index, self._seen, self._watermark = index, seen, watermark
                self._last_full = time.monotonic()

    def _rebuild_in_background(self, engine: Engine) -> None:
        """Запускает сборку в фоновом потоке, если она ещё не идёт."""
        if not self._rebuild_lock.locked():
            threading.Thread(
                target=self.rebuild, args=(engine,), name="lexical-rebuild", daemon=True
            ).start()

    def _sync(self, engine: Engine) -> None:
        """Применяет записи, изменившиеся с прошлой синхронизации.

        Пропускается, если синхронизация уже идёт в другом потоке или
        прошло меньше :data:`SYNC_INTERVAL`.
        """
        if not self._sync_lock.acquire(blocking=False):
            return
        try:
            if self._sync_due():
                self._last_sync = time.monotonic()
                self._apply(self._changed_rows(engine))
                self._schedule_rebuild(engine)
        finally:
            self._sync_lock.release()

    def _sync_due(self) -> bool:
        """Индекс построен и с прошлой синхронизации прошло достаточно времени."""
        return self._index is not None and time.monotonic() - self._last_sync >= SYNC_INTERVAL

    def _changed_rows(self, engine: Engine) -> list:
        """Записи, изменённые после отметки (с окном перекрытия) и ещё не применённые."""
        since = self._watermark - SYNC_OVERLAP if self._watermark else None
        return [r for r in self._read(engine, since) if self._is_newer(r)]

    def _apply(self, rows: list) -> None:
        """Добавляет записи в снимок индекса и сдвигает отметку синхронизации."""
        if not rows:
            return
        self._index = self._index.with_docs([self._document(r) for r in rows])
        self._seen.update({r.ticket_id: r.updated_at for r in rows})
        newest = max(r.updated_at for r in rows)
        self._watermark = newest if self._watermark is None else max(newest, self._watermark)

    def _schedule_rebuild(self, engine: Engine) -> None:
        """Запускает фоновую сборку при росте корпуса или по сроку сверки."""
        stale = time.monotonic() - self._last_full > RECONCILE_PERIOD
        if self._index.needs_refit or stale:
            self._rebuild_in_background(engine)

    def _is_newer(self, row) -> bool:  # noqa: ANN001 — строка результата SQLAlchemy
        """Запись новее уже применённой версии того же обращения."""
        seen = self._seen.get(row.ticket_id)
        return seen is None or row.updated_at > seen

    def warm_up(self, session: Session) -> None:
        """Строит индекс при запуске приложения (вызывается в фоне).

        Args:
            session: Сессия БД.
        """
        self.rebuild(self._engine(session))

    def snapshot(self, session: Session) -> LexicalIndex:
        """Догоняет индекс до БД и возвращает его текущий снимок.

        Используется и самим поисковиком, и гибридным режимом, который берёт
        из снимка лексическую составляющую.

        Args:
            session: Сессия БД.

        Returns:
            Неизменяемый снимок индекса.

        Raises:
            SearchUnavailableError: Индекс ещё строится (сразу после запуска).
        """
        engine = self._engine(session)
        if self._index is None:
            self._rebuild_in_background(engine)
            raise SearchUnavailableError("Поисковый индекс ещё строится, повторите позже")
        self._sync(engine)
        return self._index

    def search(self, session: Session, query: TicketQuery, limit: int) -> list[Candidate]:
        """Возвращает кандидатов по лексическому сходству.

        Args:
            session: Сессия БД.
            query: Данные нового обращения и видимость выдачи.
            limit: Максимальное число кандидатов.

        Returns:
            Кандидаты с S не ниже порога P2, по убыванию S.

        Raises:
            SearchUnavailableError: Индекс ещё строится (сразу после запуска).
        """
        snapshot = self.snapshot(session)
        result = []
        for doc, parts in snapshot.search(query, self.weights, self.pool):
            kind = self.classify(parts["score"])
            if kind is None:
                break
            components = {k: parts[k] for k in ("sem", "lex", "cat", "comp")}
            result.append(Candidate(doc.ticket_id, parts["score"], kind, components))
            if len(result) == limit:
                break
        return result
