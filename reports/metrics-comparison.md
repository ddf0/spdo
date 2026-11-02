# Метрики кода: до и после рефакторинга

| Показатель | До | После | Изменение |
|---|---|---|---|
| Функций и методов | 254 | 269 | +15 |
| Цикломатическая сложность, средняя | 2.88 | 2.74 | -0.14 |
| Цикломатическая сложность, максимальная | 13 | 9 | -4 |
| Функций с CC ≥ 6 | 29 | 26 | -3 |
| Индекс сопровождаемости, минимальный по модулю | 40.9 | 50.3 | +9.4 |
| Индекс сопровождаемости, средний | 78.8 | 78.9 | +0.1 |
| Строк всего (LOC) | 7939 | 8127 | +188 |
| Строк кода (SLOC) | 3727 | 3830 | +103 |
| Логических строк (LLOC) | 3350 | 3399 | +49 |
| Доля документации и комментариев, % | 33.2 | 32.8 | -0.4 |
| Объём Холстеда, суммарный | 9249 | 8582 | -668 |
| Трудность Холстеда, максимальная по модулю | 11.9 | 9.4 | -2.5 |
| Ожидаемые ошибки Холстеда, сумма | 3.08 | 2.86 | -0.22 |
| Исходящая связность Ce, средняя | 3.20 | 3.19 | -0.02 |
| Исходящая связность Ce, максимальная | 21 | 21 | 0 |
| Глубина наследования с учётом библиотек, максимальная | 5 | 5 | 0 |
| Глубина собственной иерархии классов, максимальная | 2 | 2 | 0 |

## Проблемные места до

**Цикломатическая сложность ≥ 6**

- `lexical.LexicalSearcher._sync` — 13
- `dataset._check_groups` — 12
- `service.update_user` — 12
- `evaluation.evaluate` — 11
- `evaluation.tune_hybrid` — 11
- `evaluation.best_threshold` — 10
- `dataset.holdout_checksum` — 9
- `loader._dictionary_ids` — 9
- `evaluation.labelled_scores` — 9
- `service.run_search` — 9
- `service.register` — 9
- `hybrid.HybridSearcher._candidates` — 9
- `config.Settings._check_search_params` — 8
- `cli.cmd_close_expired` — 8
- `dataset._check_ticket` — 8
- `dataset.write` — 8
- `sodd.classify` — 8
- `sodd.convert` — 8
- `evaluation.tune_lexical` — 8
- `drafts.save_draft` — 8
- `deps.verify_csrf` — 8
- `evaluation._accumulate` — 7
- `service.create_link` — 7
- `lexical.LexicalIndex.with_docs` — 7
- `lexical.LexicalIndex.search` — 7
- `service.explain` — 6
- `service._accessible_session` — 6
- `lexical.LexicalIndex.build` — 6
- `service.get_card` — 6

**Индекс сопровождаемости < 65**

- `spdo/analytics/evaluation.py` — 40.9
- `spdo/search/lexical.py` — 44.9
- `spdo/cli.py` — 50.3
- `spdo/tickets/service.py` — 50.8
- `spdo/recommendations/service.py` — 53.6
- `spdo/analytics/dataset.py` — 55.3
- `spdo/search/base.py` — 58.4
- `spdo/search/hybrid.py` — 60.6
- `spdo/analytics/sodd.py` — 61.6
- `spdo/tickets/access.py` — 62.1
- `spdo/tickets/dictionaries.py` — 63.5
- `spdo/recommendations/schemas.py` — 63.9
- `spdo/users/service.py` — 63.9
- `spdo/relations/service.py` — 64.1

**Исходящая связность Ce ≥ 8**

- `spdo.cli` — 21
- `spdo.main` — 13
- `spdo.recommendations.service` — 13
- `spdo.web.routes` — 10
- `spdo.recommendations.routes` — 8
- `spdo.relations.service` — 8

**Глубина собственной иерархии ≥ 3**

- нет

## Проблемные места после

**Цикломатическая сложность ≥ 6**

- `dataset.holdout_checksum` — 9
- `loader._dictionary_ids` — 9
- `tuning.labelled_scores` — 9
- `service.run_search` — 9
- `service.register` — 9
- `hybrid.HybridSearcher._candidates` — 9
- `config.Settings._check_search_params` — 8
- `cli.cmd_close_expired` — 8
- `dataset._check_ticket` — 8
- `dataset.write` — 8
- `sodd.classify` — 8
- `sodd.convert` — 8
- `quality.evaluate` — 8
- `drafts.save_draft` — 8
- `deps.verify_csrf` — 8
- `dataset._collect_groups` — 7
- `quality._accumulate` — 7
- `service.create_link` — 7
- `lexical_index.LexicalIndex.with_docs` — 7
- `lexical_index.LexicalIndex.search` — 7
- `tuning.best_threshold` — 6
- `service.explain` — 6
- `service._accessible_session` — 6
- `lexical.LexicalSearcher._apply` — 6
- `lexical_index.LexicalIndex.build` — 6
- `service.get_card` — 6

**Индекс сопровождаемости < 65**

- `spdo/cli.py` — 50.3
- `spdo/tickets/service.py` — 50.8
- `spdo/recommendations/service.py` — 53.6
- `spdo/analytics/dataset.py` — 55.0
- `spdo/search/lexical_index.py` — 56.3
- `spdo/search/base.py` — 58.4
- `spdo/search/lexical.py` — 59.3
- `spdo/analytics/quality.py` — 60.1
- `spdo/analytics/tuning.py` — 60.3
- `spdo/search/hybrid.py` — 60.6
- `spdo/analytics/sodd.py` — 61.6
- `spdo/tickets/access.py` — 62.1
- `spdo/users/service.py` — 63.4
- `spdo/tickets/dictionaries.py` — 63.5
- `spdo/recommendations/schemas.py` — 63.9
- `spdo/relations/service.py` — 64.1

**Исходящая связность Ce ≥ 8**

- `spdo.cli` — 21
- `spdo.main` — 13
- `spdo.recommendations.service` — 13
- `spdo.web.routes` — 10
- `spdo.recommendations.routes` — 8
- `spdo.relations.service` — 8

**Глубина собственной иерархии ≥ 3**

- нет

