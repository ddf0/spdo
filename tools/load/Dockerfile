# wrk из репозитория Debian со сценариями СПДО: нагрузочные испытания
# запускаются без установки wrk в систему. Сценарии копируются в образ, а
# не монтируются: так не требуется менять метки SELinux у файлов проекта.
# Сборка: docker build -t spdo-wrk tools/load
FROM debian:12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends wrk \
    && rm -rf /var/lib/apt/lists/*
COPY *.lua /scripts/
ENTRYPOINT ["wrk"]
