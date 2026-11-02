# Метрики кода: metrics-before

| Показатель | Значение |
|---|---|
| Функций и методов | 254 |
| Цикломатическая сложность, средняя | 2.88 |
| Цикломатическая сложность, максимальная | 13 |
| Функций с CC ≥ 6 | 29 |
| Индекс сопровождаемости, минимальный по модулю | 40.9 |
| Индекс сопровождаемости, средний | 78.8 |
| Строк всего (LOC) | 7939 |
| Строк кода (SLOC) | 3727 |
| Логических строк (LLOC) | 3350 |
| Доля документации и комментариев, % | 33.2 |
| Объём Холстеда, суммарный | 9249 |
| Трудность Холстеда, максимальная по модулю | 11.9 |
| Ожидаемые ошибки Холстеда, сумма | 3.08 |
| Исходящая связность Ce, средняя | 3.20 |
| Исходящая связность Ce, максимальная | 21 |
| Глубина наследования с учётом библиотек, максимальная | 5 |
| Глубина собственной иерархии классов, максимальная | 2 |

## Проблемные места

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

