"""Офлайн-оценка качества подбора и подбор параметров (ТЗ, п. 8.2).

Единая точка входа для команд ``spdo eval`` и ``spdo tune``; реализация
разнесена по модулям:

* :mod:`spdo.analytics.quality` — корпус набора и показатели качества
  (recall@3, hit@3, MRR, доля ложных «дубликатов»);
* :mod:`spdo.analytics.candidates` — кандидаты гибридного режима с кешем
  векторов;
* :mod:`spdo.analytics.tuning` — подбор весов и порогов.
"""

from spdo.analytics.candidates import CachedEncoder, HybridCandidates, hybrid_ranker
from spdo.analytics.quality import (
    Corpus,
    IdMap,
    K,
    Quality,
    Ranker,
    build_corpus,
    duplicates_map,
    evaluate,
    lexical_ranker,
    related_map,
    suggestion_accuracy,
)
from spdo.analytics.tuning import (
    LEXICAL_GRID,
    LEXICAL_SHARE_GRID,
    SEMANTIC_GRID,
    THRESHOLD_GRID,
    TUNE_DEPTH,
    HybridTuning,
    Tuning,
    best_threshold,
    labelled_scores,
    tune_hybrid,
    tune_lexical,
)

__all__ = [
    "K",
    "LEXICAL_GRID",
    "LEXICAL_SHARE_GRID",
    "SEMANTIC_GRID",
    "THRESHOLD_GRID",
    "TUNE_DEPTH",
    "CachedEncoder",
    "Corpus",
    "HybridCandidates",
    "HybridTuning",
    "IdMap",
    "Quality",
    "Ranker",
    "Tuning",
    "best_threshold",
    "build_corpus",
    "duplicates_map",
    "evaluate",
    "hybrid_ranker",
    "labelled_scores",
    "lexical_ranker",
    "related_map",
    "suggestion_accuracy",
    "tune_hybrid",
    "tune_lexical",
]
