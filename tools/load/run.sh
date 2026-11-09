#!/usr/bin/env bash
# Нагрузочные испытания СПДО сценариями wrk.
#
# Использование:
#   SPDO_LOAD_PASSWORD=... tools/load/run.sh <адрес> <каталог отчёта> [соединения...]
#
# Переменные окружения:
#   SPDO_LOAD_PASSWORD  пароль пользователей сценариев (обязательно);
#   SPDO_LOAD_USER      пользователь для подбора (по умолчанию user1);
#   SPDO_LOAD_OPERATOR  оператор для реестра, карточек, инцидентов (operator);
#   SPDO_LOAD_DURATION  длительность каждого прогона (30s);
#   SPDO_LOAD_SCENARIOS сценарии (search registry card incidents);
#   SPDO_MAX_TICKET     наибольший номер обращения для card.lua (5000).
#
# Каждый сценарий запускается при каждом числе соединений (по умолчанию
# 10 20 50: ТЗ требует не менее 10 одновременных пользователей); вывод wrk
# сохраняется в <каталог>/<сценарий>-c<N>.txt, итоговые строки — в
# <каталог>/summary.txt.
set -euo pipefail

if [ $# -lt 2 ] || [ -z "${SPDO_LOAD_PASSWORD:-}" ]; then
  sed -n '5s/^# */Использование: /p' "$0" >&2
  exit 2
fi
BASE=${1%/}
OUT=$2
shift 2
CONNECTIONS=${*:-10 20 50}
DURATION=${SPDO_LOAD_DURATION:-30s}
SCENARIOS=${SPDO_LOAD_SCENARIOS:-search registry card incidents}
HERE=$(cd "$(dirname "$0")" && pwd)

docker build -q -t spdo-wrk "$HERE" >/dev/null
mkdir -p "$OUT"
: >"$OUT/summary.txt"

for scenario in $SCENARIOS; do
  if [ "$scenario" = search ]; then
    user=${SPDO_LOAD_USER:-user1}
  else
    user=${SPDO_LOAD_OPERATOR:-operator}
  fi
  # Новый вход перед каждым сценарием: сеанс не зависит от предыдущих прогонов.
  eval "$(python3 "$HERE/session.py" "$BASE" "$user")"
  export SPDO_COOKIE SPDO_CSRF SPDO_MAX_TICKET=${SPDO_MAX_TICKET:-5000}
  for c in $CONNECTIONS; do
    threads=$((c < 4 ? c : 4))
    log="$OUT/$scenario-c$c.txt"
    echo "== $scenario, соединений: $c, пользователь: $user" | tee "$log"
    docker run --rm --network host \
      -e SPDO_COOKIE -e SPDO_CSRF -e SPDO_MAX_TICKET \
      spdo-wrk -t"$threads" -c"$c" -d"$DURATION" --timeout 10s --latency \
      -s "/scripts/$scenario.lua" "$BASE" 2>&1 | tee -a "$log"
    # Ошибка в сценарии не прерывает wrk: он шлёт GET / без сценария.
    if ! result=$(grep '^RESULT' "$log"); then
      echo "Ошибка: сценарий $scenario не выполнен, см. $log" >&2
      exit 1
    fi
    echo "$scenario c=$c ${result#RESULT }" >>"$OUT/summary.txt"
  done
done
column -t "$OUT/summary.txt"
if grep -qE 'requests=0 |errors=[1-9]|non_ok=[1-9]' "$OUT/summary.txt"; then
  echo "Внимание: есть прогоны без ответов, ошибки или ответы, отличные от 200, — замер недостоверен" >&2
  exit 3
fi
