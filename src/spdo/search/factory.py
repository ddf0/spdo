"""Создание поисковика по конфигурации.

Поисковик создаётся один раз на процесс: лексический держит индекс в
памяти, гибридный — загруженную модель. Бизнес-модули получают его через
:func:`get_searcher` и работают с ним только через интерфейс
:class:`~spdo.search.base.SimilaritySearcher`.
"""

import threading

from spdo.config import SearchMode, Settings, settings
from spdo.search.base import SimilaritySearcher
from spdo.search.embeddings import Encoder
from spdo.search.hybrid import HybridSearcher, HybridWeights
from spdo.search.lexical import LexicalSearcher, LexicalWeights


def build_searcher(config: Settings, mode: SearchMode | None = None) -> SimilaritySearcher:
    """Создаёт поисковик заданного режима с параметрами из конфигурации.

    Args:
        config: Параметры приложения.
        mode: Режим поиска; по умолчанию — ``config.search_mode``.

    Returns:
        Поисковик.

    """
    mode = mode or config.search_mode
    if mode is SearchMode.LEXICAL:
        weights = LexicalWeights(
            config.lexical_w_lexical, config.lexical_w_category, config.lexical_w_component
        )
        return LexicalSearcher(
            config.lexical_threshold_duplicate,
            config.lexical_threshold_related,
            weights,
            config.candidate_pool,
        )
    weights = HybridWeights(
        config.w_semantic, config.w_lexical, config.w_category, config.w_component
    )
    encoder = Encoder(
        config.embedding_model,
        config.embedding_dim,
        config.embedding_device,
        config.embedding_max_tokens,
        config.embedding_threads,
        config.embedding_allow_download,
    )
    return HybridSearcher(
        config.threshold_duplicate,
        config.threshold_related,
        weights,
        encoder,
        config.candidate_pool,
    )


def reconfigure(searcher: SimilaritySearcher, config: Settings) -> None:
    """Обновляет пороги и веса работающего поисковика того же режима.

    Индекс в памяти и загруженная модель сохраняются.

    Args:
        searcher: Поисковик процесса.
        config: Параметры приложения.
    """
    if isinstance(searcher, LexicalSearcher):
        searcher.threshold_duplicate = config.lexical_threshold_duplicate
        searcher.threshold_related = config.lexical_threshold_related
        searcher.weights = LexicalWeights(
            config.lexical_w_lexical, config.lexical_w_category, config.lexical_w_component
        )
    elif isinstance(searcher, HybridSearcher):
        searcher.threshold_duplicate = config.threshold_duplicate
        searcher.threshold_related = config.threshold_related
        searcher.weights = HybridWeights(
            config.w_semantic, config.w_lexical, config.w_category, config.w_component
        )


#: Блокировка построения и замены поисковика процесса.
LOCK = threading.RLock()
_searcher: SimilaritySearcher | None = None


def get_searcher() -> SimilaritySearcher:
    """Возвращает поисковик процесса, созданный по текущей конфигурации.

    Поисковик строится под блокировкой :data:`LOCK`, поэтому не видит
    конфигурацию, которую в это время меняет администратор.

    Returns:
        Поисковик.
    """
    global _searcher  # noqa: PLW0603 — поисковик один на процесс
    searcher = _searcher
    if searcher is None:
        with LOCK:
            if _searcher is None:
                _searcher = build_searcher(settings)
            searcher = _searcher
    return searcher


def current_searcher() -> SimilaritySearcher | None:
    """Поисковик процесса, если он уже построен (без построения).

    Returns:
        Поисковик или ``None``.
    """
    return _searcher


def reset_searcher() -> None:
    """Сбрасывает поисковик процесса; следующий вызов построит новый."""
    global _searcher  # noqa: PLW0603
    with LOCK:
        _searcher = None


#: Совместимость с прежним интерфейсом кеша.
get_searcher.cache_clear = reset_searcher  # type: ignore[attr-defined]
