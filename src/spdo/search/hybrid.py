"""Основной режим: гибридное ранжирование.

S = w1·S_sem + w2·S_lex + w3·S_cat + w4·S_comp, где S_sem — косинус
векторных представлений «заголовок + описание» (pgvector), S_lex — косинус
TF-IDF, S_cat и S_comp — совпадение категории и компонента.

Кандидаты — объединение двух списков: ближайшие по вектору (запрос к
pgvector с HNSW-индексом и фильтром видимости) и ближайшие лексически (из
снимка лексического индекса). Для объединения вычисляется полный S той же
функцией :func:`combine`, что и при офлайн-оценке качества; лексическая
составляющая всех кандидатов считается по их тексту из БД словарём снимка.

HNSW — приближённый поиск, и фильтр видимости применяется после обхода
индекса. Поэтому для транзакции подбора задаются ``hnsw.ef_search`` не
меньше удвоенного пула и итеративное сканирование (pgvector ≥ 0.8): обход
продолжается, пока фильтр не наберёт нужное число строк. Офлайн-оценка
использует точный поиск ближайших — небольшое расхождение с приложением
возможно только из-за приближённости HNSW.
"""

import dataclasses
import logging
from dataclasses import dataclass

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from spdo.search.base import Candidate, IndexedTicket, SimilaritySearcher, TicketQuery
from spdo.search.embeddings import Encoder
from spdo.search.lexical import ALGORITHM_VERSION, LexicalSearcher, LexicalWeights
from spdo.search.models import SearchIndexEntry

logger = logging.getLogger(__name__)

#: Веса лексического списка кандидатов: только текстовое сходство.
_LEXICAL_ONLY = LexicalWeights(1.0, 0.0, 0.0)
#: Наименьшее значение ``hnsw.ef_search`` для транзакции подбора.
MIN_EF_SEARCH = 100


def same(a: int | None, b: int | None) -> float:
    """Признак совпадения значения справочника: 1, если оба заданы и равны.

    Args:
        a: Значение у запроса.
        b: Значение у кандидата.

    Returns:
        1.0 или 0.0.
    """
    return float(a is not None and a == b)


@dataclass(frozen=True, slots=True)
class HybridWeights:
    """Веса w1–w4 составляющих S.

    Attributes:
        semantic: w1 — семантическое сходство.
        lexical: w2 — лексическое сходство.
        category: w3 — совпадение категории.
        component: w4 — совпадение компонента.
    """

    semantic: float
    lexical: float
    category: float
    component: float


def combine(w: HybridWeights, sem: float, lex: float, cat: float, comp: float) -> float:
    """Вычисляет итоговый коэффициент сходства S.

    Косинус векторов может быть отрицательным; составляющая ограничивается
    снизу нулём, итог — отрезком [0, 1].

    Args:
        w: Веса.
        sem: Семантическое сходство.
        lex: Лексическое сходство.
        cat: Совпадение категории (0 или 1).
        comp: Совпадение компонента (0 или 1).

    Returns:
        S в [0, 1].
    """
    score = w.semantic * max(sem, 0.0) + w.lexical * lex + w.category * cat + w.component * comp
    return min(max(score, 0.0), 1.0)


class HybridSearcher(SimilaritySearcher):
    """Гибридный поиск похожих обращений.

    Производный класс :class:`~spdo.search.base.SimilaritySearcher`.
    Переопределяет :meth:`search`, :meth:`warm_up` и :meth:`index_values`:
    при индексации сохраняет вектор текста, лексическую составляющую берёт
    у встроенного :class:`~spdo.search.lexical.LexicalSearcher`.

    Attributes:
        weights: Весовые коэффициенты w1–w4, сумма равна 1.
        encoder: Кодировщик текстов в векторы.
        pool: Размер каждого из двух списков кандидатов.
    """

    mode = "hybrid"

    def __init__(
        self,
        threshold_duplicate: float,
        threshold_related: float,
        weights: HybridWeights,
        encoder: Encoder,
        pool: int,
    ) -> None:
        """Создаёт гибридный поисковик.

        Args:
            threshold_duplicate: Порог P1.
            threshold_related: Порог P2.
            weights: Весовые коэффициенты w1–w4.
            encoder: Кодировщик текстов.
            pool: Размер списков кандидатов.
        """
        super().__init__(threshold_duplicate, threshold_related)
        self.weights = weights
        self.encoder = encoder
        self.pool = pool
        self._lexical = LexicalSearcher(threshold_duplicate, threshold_related, _LEXICAL_ONLY, pool)

    @property
    def model_version(self) -> str:
        """Идентификатор модели векторов и версия лексического алгоритма.

        Это версия для сеанса поиска. В записи индекса (столбец
        ``model_version``) хранится только идентификатор модели, которой
        вычислен вектор, — по нему отбираются векторы текущей модели.
        """
        return f"{self.encoder.model_name}+{ALGORITHM_VERSION}"

    def params(self) -> dict[str, object]:
        """Пороги и веса w1–w4 для сеанса поиска.

        Returns:
            Пороги P1, P2 и веса составляющих.
        """
        return {**super().params(), "weights": dataclasses.asdict(self.weights)}

    def index_values(self, ticket: IndexedTicket) -> dict[str, object]:
        """Добавляет к записи индекса вектор текста и модель, которой он вычислен.

        Args:
            ticket: Индексируемое обращение.

        Returns:
            Поля ``embedding`` и ``model_version``.
        """
        vector = self.encoder.encode([ticket.text])[0]
        return {"embedding": vector.tolist(), "model_version": self.encoder.model_name}

    def index(self, session: Session, ticket: IndexedTicket) -> None:
        """Записывает обращение в индекс, пересчитывая вектор только при изменении текста.

        Переопределяет :meth:`SimilaritySearcher.index`: при смене признака
        конфиденциальности текст не меняется, и модель не вызывается.

        Args:
            session: Сессия БД.
            ticket: Индексируемое обращение.
        """
        row = session.execute(
            select(SearchIndexEntry.text, SearchIndexEntry.model_version).where(
                SearchIndexEntry.ticket_id == ticket.ticket_id,
                SearchIndexEntry.embedding.is_not(None),
            )
        ).first()
        unchanged = row is not None and row.text == ticket.text
        if unchanged and row.model_version == self.encoder.model_name:
            self._write(session, ticket, {})
        else:
            self._write(session, ticket, self.index_values(ticket))

    def warm_up(self, session: Session) -> None:
        """Загружает модель и строит лексический индекс при запуске.

        Если в индексе есть векторы другой модели, предупреждает: их
        семантическая составляющая равна нулю до ``spdo reindex``.

        Args:
            session: Сессия БД.
        """
        self.encoder.load()
        self._lexical.warm_up(session)
        stale = session.scalar(
            select(func.count()).where(
                SearchIndexEntry.embedding.is_not(None),
                SearchIndexEntry.model_version != self.encoder.model_name,
            )
        )
        if stale:
            logger.warning("В индексе %s векторов другой модели; выполните spdo reindex", stale)

    def _semantic(
        self, session: Session, vector: list[float], query: TicketQuery, ids: list[int] | None
    ) -> list:
        """Читает кандидатов с семантическим сходством.

        Без ``ids`` — ближайшие по вектору среди записей текущей модели;
        с ``ids`` — заданные обращения, у которых нет вектора текущей модели
        сходство равно нулю (они пришли из лексического списка). Видимость и
        исключения применяются в запросе.
        """
        distance = SearchIndexEntry.embedding.cosine_distance(vector)
        current = and_(
            SearchIndexEntry.embedding.is_not(None),
            SearchIndexEntry.model_version == self.encoder.model_name,
        )
        sem = case((current, 1 - distance), else_=0.0).label("sem")
        stmt = select(
            SearchIndexEntry.ticket_id,
            SearchIndexEntry.text,
            SearchIndexEntry.category_id,
            SearchIndexEntry.component_id,
            sem,
        )
        if query.exclude_ids:
            stmt = stmt.where(SearchIndexEntry.ticket_id.not_in(query.exclude_ids))
        if not query.scope.sees_confidential:
            stmt = stmt.where(
                or_(
                    SearchIndexEntry.confidential.is_(False),
                    SearchIndexEntry.author_id == query.scope.viewer_id,
                )
            )
        if ids is None:
            ef = max(2 * self.pool, MIN_EF_SEARCH)
            session.execute(select(func.set_config("hnsw.ef_search", str(ef), True)))
            session.execute(select(func.set_config("hnsw.iterative_scan", "relaxed_order", True)))
            stmt = stmt.where(current).order_by(distance).limit(self.pool)
        else:
            stmt = stmt.where(SearchIndexEntry.ticket_id.in_(ids))
        return list(session.execute(stmt))

    def _candidates(self, session: Session, query: TicketQuery) -> list[tuple[int, dict]]:
        """Объединяет семантический и лексический списки и считает составляющие."""
        snapshot = self._lexical.snapshot(session)
        vector = self.encoder.encode([query.text])[0].tolist()
        lexical_ids = [d.ticket_id for d, _ in snapshot.search(query, _LEXICAL_ONLY, self.pool)]
        rows = {r.ticket_id: r for r in self._semantic(session, vector, query, None)}
        missing = [tid for tid in lexical_ids if tid not in rows]
        if missing:
            rows.update({r.ticket_id: r for r in self._semantic(session, vector, query, missing)})
        candidates = list(rows.values())
        lex = snapshot.similarity_to(query.text, [r.text for r in candidates])
        return [
            (
                row.ticket_id,
                {
                    "sem": max(float(row.sem), 0.0),
                    "lex": float(lex[i]),
                    "cat": same(query.category_id, row.category_id),
                    "comp": same(query.component_id, row.component_id),
                },
            )
            for i, row in enumerate(candidates)
        ]

    def search(self, session: Session, query: TicketQuery, limit: int) -> list[Candidate]:
        """Возвращает кандидатов по гибридному сходству.

        Args:
            session: Сессия БД.
            query: Данные нового обращения и видимость выдачи.
            limit: Максимальное число кандидатов.

        Returns:
            Кандидаты с S не ниже порога P2, по убыванию S.

        Raises:
            SearchUnavailableError: Модель не загружена или индекс ещё строится.
        """
        scored = [
            (tid, combine(self.weights, **parts), parts)
            for tid, parts in self._candidates(session, query)
        ]
        scored.sort(key=lambda item: -item[1])
        result = []
        for ticket_id, score, parts in scored:
            kind = self.classify(score)
            if kind is None or len(result) == limit:
                break
            result.append(Candidate(ticket_id, score, kind, parts))
        return result
