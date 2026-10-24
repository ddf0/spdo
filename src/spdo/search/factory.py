"""Создание поисковика по конфигурации.

Поисковик создаётся один раз на процесс: лексический держит индекс в
памяти, гибридный — загруженную модель. Бизнес-модули получают его через
:func:`get_searcher` и работают с ним только через интерфейс
:class:`~spdo.search.base.SimilaritySearcher`.
"""

from functools import lru_cache

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


@lru_cache(maxsize=1)
def get_searcher() -> SimilaritySearcher:
    """Возвращает поисковик процесса, созданный по текущей конфигурации.

    Returns:
        Поисковик.
    """
    return build_searcher(settings)
