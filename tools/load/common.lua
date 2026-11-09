-- Общие части сценариев wrk: учётные данные сессии и отчёт с p95.
-- Переменные окружения SPDO_COOKIE и SPDO_CSRF печатает session.py.
local M = {}

M.cookie = os.getenv("SPDO_COOKIE") or ""
M.csrf = os.getenv("SPDO_CSRF") or ""

-- Заголовки запроса от имени вошедшего пользователя.
function M.headers(extra)
  local h = { ["Cookie"] = M.cookie }
  for k, v in pairs(extra or {}) do h[k] = v end
  return h
end

-- Потоки wrk: в каждом свой интерпретатор Lua, счётчики собираются в done.
local threads = {}

function M.setup(thread)
  table.insert(threads, thread)
end

-- wrk считает ошибкой только статус >= 400; перенаправление на /login
-- (сеанс потерян) тоже нужно считать неудачей, поэтому учитывается всё,
-- кроме 200 и 201.
non_ok = 0

function M.response(status, headers, body)
  if status ~= 200 and status ~= 201 then
    non_ok = non_ok + 1
  end
end

-- Итоговая строка RESULT: wrk --latency не выводит 95-й процентиль,
-- а требование ТЗ задано именно для p95.
function M.report(summary, latency, requests)
  local e = summary.errors
  local seconds = summary.duration / 1e6
  local bad = 0
  for _, thread in ipairs(threads) do
    bad = bad + thread:get("non_ok")
  end
  io.write(string.format(
    "RESULT requests=%d duration_s=%.1f rps=%.2f errors=%d non_ok=%d "
      .. "p50_ms=%.1f p95_ms=%.1f p99_ms=%.1f max_ms=%.1f\n",
    summary.requests, seconds, summary.requests / seconds,
    e.connect + e.read + e.write + e.timeout, bad,
    latency:percentile(50) / 1000, latency:percentile(95) / 1000,
    latency:percentile(99) / 1000, latency.max / 1000))
end

-- Разные потоки wrk получают разные последовательности случайных чисел.
local thread_seed = 0
function M.seed()
  thread_seed = thread_seed + 1
  math.randomseed(os.time() + thread_seed * 7919 + math.floor(os.clock() * 1e6))
end

return M
