"""Базовый режим: лексическое сходство (TF-IDF по символьным n-граммам).

Символьные 3–5-граммы в границах слов частично заменяют морфологический
анализ русского языка: «принтер», «принтера» и «принтеру» делят большинство
n-грамм, поэтому словоформы сближаются без лемматизатора. Косинус векторов
TF-IDF лежит в [0, 1] и не требует дополнительной нормализации.

Модуль состоит из двух частей:

* :class:`LexicalIndex` — индекс в памяти без обращений к БД; его же
  использует офлайн-оценка качества на эталонных наборах;
* :class:`LexicalSearcher` — поисковик приложения: держит индекс в памяти
  процесса и догоняет таблицу ``search_index`` по дате изменения записей.

Производительность на 10 000 обращений: подбор — десятки миллисекунд,
полная сборка индекса — десятки секунд. Поэтому подбор всегда читает
готовый неизменяемый снимок индекса, а полная сборка выполняется в фоне:
при запуске (:meth:`LexicalSearcher.warm_up`), при росте корпуса больше чем
на :data:`REFIT_GROWTH` и раз в :data:`RECONCILE_PERIOD` для сверки с БД.
Между сборками новые и изменённые обращения добавляются за миллисекунды.

Каждый рабочий процесс сервера держит свой индекс (около 200–400 МБ на
10 000 обращений), поэтому рекомендуется один процесс uvicorn.
"""

import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from spdo.search.base import (
    Candidate,
    SearchUnavailableError,
    SimilaritySearcher,
    TicketQuery,
)
from spdo.search.models import SearchIndexEntry

#: Версия алгоритма, записываемая в сеанс поиска.
ALGORITHM_VERSION = "tfidf-charwb-3-5-v1"
#: Доля новых документов от размера корпуса, после которой словарь
#: и веса IDF пересчитываются заново.
REFIT_GROWTH = 0.2
#: Окно повторного чтения изменённых записей: транзакция с более ранней
#: датой изменения может зафиксироваться позже предыдущей синхронизации.
SYNC_OVERLAP = timedelta(minutes=2)
#: Наименьший интервал между инкрементальными синхронизациями, секунды.
SYNC_INTERVAL = 1.0
#: Период полной сверки индекса с БД в фоне, секунды: подбирает записи,
#: пропущенные долгими транзакциями, и удалённые из индекса.
RECONCILE_PERIOD = 600.0


def normalize(text: str) -> str:
    """Приводит текст к виду для сравнения: нижний регистр, «ё» → «е», пробелы.

    Args:
        text: Исходный текст.

    Returns:
        Нормализованный текст.
    """
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е")).strip()


@dataclass(frozen=True, slots=True)
class Document:
    """Документ индекса.

    Attributes:
        ticket_id: Номер обращения.
        text: Текст «заголовок + описание».
        category_id: Категория.
        component_id: Компонент.
        author_id: Автор.
        confidential: Признак конфиденциальности.
    """

    ticket_id: int
    text: str
    category_id: int | None
    component_id: int | None
    author_id: int
    confidential: bool


@dataclass(frozen=True, slots=True)
class LexicalWeights:
    """Веса составляющих S в лексическом режиме (w1 = 0).

    Attributes:
        lexical: Вес лексического сходства.
        category: Вес совпадения категории.
        component: Вес совпадения компонента.
    """

    lexical: float
    category: float
    component: float


class LexicalIndex:
    """Индекс TF-IDF в памяти.

    Матрица хранится сегментами: базовый сегмент строится при пересчёте
    словаря, новые документы дописываются небольшими сегментами по уже
    построенному словарю. Заменённый документ помечается неактуальным, его
    новая версия дописывается в конец. Изменения не меняют объект, а
    возвращают новый (:meth:`with_docs`), поэтому читатели работают со
    снимком без блокировок, а ошибка посреди изменения не портит индекс.

    Attributes:
        size: Число актуальных документов.
        needs_refit: С последнего пересчёта словаря добавлено больше
            :data:`REFIT_GROWTH` от размера корпуса.
    """

    def __init__(self) -> None:
        """Создаёт пустой индекс."""
        self._vectorizer: TfidfVectorizer | None = None
        self._docs: list[Document] = []
        self._alive: list[bool] = []
        self._row: dict[int, int] = {}
        self._segments: list[sparse.csr_matrix] = []
        self._fitted_rows = 0

    @classmethod
    def build(cls, docs: list[Document]) -> "LexicalIndex":
        """Строит индекс заново: словарь и веса IDF по всем документам.

        Args:
            docs: Документы; повторяющийся номер — побеждает последний.

        Returns:
            Новый индекс.

        Raises:
            SearchUnavailableError: По текстам не удалось построить словарь.
        """
        index = cls()
        latest = list({d.ticket_id: d for d in docs}.values())
        if not latest:
            return index
        vectorizer = TfidfVectorizer(
            analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True, preprocessor=normalize
        )
        try:
            matrix = vectorizer.fit_transform([d.text for d in latest]).tocsr()
        except ValueError as exc:  # пустой словарь
            raise SearchUnavailableError(f"Не удалось построить индекс: {exc}") from exc
        index._vectorizer = vectorizer
        index._docs = latest
        index._alive = [True] * len(latest)
        index._row = {d.ticket_id: i for i, d in enumerate(latest)}
        index._segments = [matrix]
        index._fitted_rows = len(latest)
        return index

    @property
    def size(self) -> int:
        """Число актуальных документов."""
        return len(self._row)

    @property
    def needs_refit(self) -> bool:
        """Нужен ли пересчёт словаря из-за роста корпуса."""
        return len(self._docs) - self._fitted_rows > REFIT_GROWTH * max(self._fitted_rows, 1)

    def documents(self) -> list[Document]:
        """Возвращает актуальные документы в порядке добавления."""
        return [self._docs[r] for r in sorted(self._row.values())]

    def with_docs(self, docs: list[Document]) -> "LexicalIndex":
        """Возвращает новый индекс с добавленными или заменёнными документами.

        Пока словаря нет, индекс строится заново; иначе новые версии
        документов дописываются сегментом по существующему словарю.

        Args:
            docs: Документы; повторяющийся номер обращения заменяет запись.

        Returns:
            Новый индекс; исходный не меняется.
        """
        latest = {d.ticket_id: d for d in docs}
        if not latest:
            return self
        if self._vectorizer is None:
            return LexicalIndex.build([*self.documents(), *latest.values()])
        segment = self._vectorizer.transform([d.text for d in latest.values()]).tocsr()
        new = LexicalIndex()
        new._vectorizer = self._vectorizer
        new._docs = [*self._docs, *latest.values()]
        new._alive = [*self._alive, *([True] * len(latest))]
        new._row = dict(self._row)
        for offset, tid in enumerate(latest):
            old = new._row.get(tid)
            if old is not None:
                new._alive[old] = False
            new._row[tid] = len(self._docs) + offset
        new._segments = [*self._segments, segment]
        new._fitted_rows = self._fitted_rows
        return new

    def upsert(self, docs: list[Document]) -> None:
        """Добавляет или заменяет документы, пересчитывая словарь при росте.

        Удобная изменяемая форма для офлайн-оценки: состояние заменяется
        целиком только после успешного построения.

        Args:
            docs: Документы.
        """
        new = self.with_docs(docs)
        if new.needs_refit:
            new = LexicalIndex.build(new.documents())
        self.__dict__.update(new.__dict__)

    def similarities(self, text: str) -> np.ndarray:
        """Возвращает косинусное сходство текста со всеми строками индекса.

        Args:
            text: Текст запроса.

        Returns:
            Массив сходства в [0, 1] по всем строкам, включая неактуальные.
        """
        if self._vectorizer is None:
            return np.zeros(0)
        query = self._vectorizer.transform([text]).T
        return np.concatenate(
            [np.asarray((seg @ query).todense()).ravel() for seg in self._segments]
        )

    def similarity_to(self, query: str, texts: list[str]) -> np.ndarray:
        """Возвращает лексическое сходство запроса с произвольными текстами.

        Тексты преобразуются словарём индекса; так гибридный режим считает
        лексическую составляющую по актуальному тексту из БД, даже если
        снимок ещё не догнал последние изменения.

        Args:
            query: Текст запроса.
            texts: Тексты кандидатов.

        Returns:
            Косинусное сходство TF-IDF в порядке текстов.
        """
        if self._vectorizer is None or not texts:
            return np.zeros(len(texts))
        q = self._vectorizer.transform([query]).T
        return np.asarray((self._vectorizer.transform(texts) @ q).todense()).ravel()

    def scores(self, text: str) -> dict[int, float]:
        """Возвращает лексическое сходство текста с каждым актуальным документом.

        Args:
            text: Текст запроса.

        Returns:
            Номер обращения → косинусное сходство TF-IDF.
        """
        sims = self.similarities(text)
        return {self._docs[row].ticket_id: float(sims[row]) for row in self._row.values()}

    def search(
        self, query: TicketQuery, weights: LexicalWeights, pool: int
    ) -> list[tuple[Document, dict[str, float]]]:
        """Подбирает документы-кандидаты и считает составляющие сходства.

        Отбираются ``pool`` актуальных документов с наибольшим лексическим
        сходством среди видимых в области запроса; для них вычисляется S.

        Args:
            query: Запрос.
            weights: Веса составляющих.
            pool: Размер пула кандидатов до ранжирования по S.

        Returns:
            Пары (документ, составляющие и итог ``score``), по убыванию S.
        """
        sims = self.similarities(query.text)
        visible = [
            i
            for i, d in enumerate(self._docs)
            if self._alive[i]
            and d.ticket_id not in query.exclude_ids
            and query.scope.allows(d.author_id, d.confidential)
        ]
        if not visible:
            return []
        idx = np.asarray(visible)
        top = idx[np.argsort(-sims[idx], kind="stable")[:pool]]
        scored = [
            (self._docs[i], _components(self._docs[i], query, float(sims[i]), weights)) for i in top
        ]
        return sorted(scored, key=lambda item: -item[1]["score"])


def _components(
    doc: Document, query: TicketQuery, lex: float, w: LexicalWeights
) -> dict[str, float]:
    """Составляющие S и итоговое значение для одного документа."""
    cat = float(query.category_id is not None and doc.category_id == query.category_id)
    comp = float(query.component_id is not None and doc.component_id == query.component_id)
    score = w.lexical * lex + w.category * cat + w.component * comp
    return {"sem": 0.0, "lex": lex, "cat": cat, "comp": comp, "score": min(max(score, 0.0), 1.0)}


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
            if self._index is None or time.monotonic() - self._last_sync < SYNC_INTERVAL:
                return
            self._last_sync = time.monotonic()
            since = self._watermark - SYNC_OVERLAP if self._watermark else None
            fresh = [r for r in self._read(engine, since) if self._is_newer(r)]
            if fresh:
                self._index = self._index.with_docs([self._document(r) for r in fresh])
                self._seen.update({r.ticket_id: r.updated_at for r in fresh})
                newest = max(r.updated_at for r in fresh)
                self._watermark = max(filter(None, (newest, self._watermark)))
            stale = time.monotonic() - self._last_full > RECONCILE_PERIOD
            if self._index.needs_refit or stale:
                self._rebuild_in_background(engine)
        finally:
            self._sync_lock.release()

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
