"""Создание поисковика по конфигурации.

Поисковик создаётся один раз на процесс: лексический держит индекс в
памяти, гибридный — загруженную модель. Бизнес-модули получают его через
:func:`get_searcher` и работают с ним только через интерфейс
:class:`~spdo.search.base.SimilaritySearcher`.
"""

from functools import lru_cache

from spdo.config import SearchMode, Settings, settings
from spdo.search.base import SimilaritySearcher
from spdo.search.lexical import LexicalSearcher, LexicalWeights


def build_searcher(config: Settings, mode: SearchMode | None = None) -> SimilaritySearcher:
    """Создаёт поисковик заданного режима с параметрами из конфигурации.

    Args:
        config: Параметры приложения.
        mode: Режим поиска; по умолчанию — ``config.search_mode``.

    Returns:
        Поисковик.

    Raises:
        NotImplementedError: Режим ещё не реализован.
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
    raise NotImplementedError(f"Режим поиска «{mode.value}» ещё не реализован")


@lru_cache(maxsize=1)
def get_searcher() -> SimilaritySearcher:
    """Возвращает поисковик процесса, созданный по текущей конфигурации.

    Returns:
        Поисковик.
    """
    return build_searcher(settings)
