"""Индекс TF-IDF в памяти для лексического сходства.

Символьные 3–5-граммы в границах слов частично заменяют морфологический
анализ русского языка: «принтер», «принтера» и «принтеру» делят большинство
n-грамм, поэтому словоформы сближаются без лемматизатора. Косинус векторов
TF-IDF лежит в [0, 1] и не требует дополнительной нормализации.

Индекс не обращается к БД: его использует и поисковик приложения
(:mod:`spdo.search.lexical`), и офлайн-оценка качества на эталонных наборах.
"""

import re
from dataclasses import dataclass

import numpy as np
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer

from spdo.search.base import SearchUnavailableError, TicketQuery

#: Доля новых документов от размера корпуса, после которой словарь
#: и веса IDF пересчитываются заново.
REFIT_GROWTH = 0.2


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
