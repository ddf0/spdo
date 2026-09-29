# Эталонный набор обращений

Файлы:

- `tickets.csv` — обращения: `id, title, description, category, component, status, split`;
- `groups.csv` — эталонная разметка: `ticket_id, group_id, kind` (`duplicate` | `related`);
- `checksum.txt` — SHA-256 проверочной части (`split = holdout`), фиксируется при заморозке.

Состав: 15–20 проблем × 4–6 формулировок, 20–30 тематически близких,
не менее 20 независимых. `split`: `tune` — настройка порогов и весов,
`holdout` — только измерение recall@3, не изменяется между релизами.
