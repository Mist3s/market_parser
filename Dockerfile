# Два образа из одного файла, и это не удобство сборки, а инвариант.
#
# Запрет браузера на пути запроса — единственная формулировка, которую можно
# проверить в CI: в целевом образе `api` нет ни camoufox, ни playwright,
# поэтому браузер там не импортируется, потому что его там нет. Гард в коде
# (assert_off_request_path) закрывает proxy6 полностью, а для браузера он
# неполон — разработчик, написавший запуск прямо в обработчике, его не задел
# бы. Упаковка закрывает и это.

# --- api: путь запроса, без браузера ------------------------------------
FROM python:3.12-slim AS api

WORKDIR /app
COPY pyproject.toml README.md ./
COPY mktlink ./mktlink
RUN pip install --no-cache-dir .
COPY deploy ./deploy
RUN groupadd --gid 10002 mktlink && useradd --uid 10002 --gid 10002 --no-create-home mktlink \
    && mkdir -p /app/data && chown 10002:10002 /app/data
LABEL org.opencontainers.image.source="https://github.com/Mist3s/market_parser"

ENV PYTHONUNBUFFERED=1 \
    MKTLINK_DB_PATH=/app/data/mktlink.sqlite \
    MKTLINK_FORGE_SOCKET=/app/data/forge.sock

# Проверка инварианта на этапе сборки: если браузер сюда просочился
# зависимостью, образ не соберётся, а не сломается в проде.
RUN python -c "\
import importlib.util as u, sys;\
bad=[m for m in ('camoufox','playwright') if u.find_spec(m)];\
sys.exit('browser leaked into the api image: %s' % bad) if bad else None"

# Том для /app/data: там SQLite с never_renew, тратами и jar. Инструкцию
# VOLUME не используем — часть билдеров её не поддерживает.
EXPOSE 8000
USER 10002:10002
CMD ["uvicorn", "mktlink.api.app:factory", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", \
     # 35 > RESPONSE_BUDGET_MAX_MS (30 с): keep-alive не влияет на запрос
     # в полёте, но держать его НИЖЕ потолка ответа — это противоречие в
     # конфигурации, которое однажды прочитают как настоящее ограничение.
     "--timeout-keep-alive", "35", "--workers", "1"]

# --- forge: браузер и владение proxy6 -----------------------------------
FROM mcr.microsoft.com/playwright/python:v1.48.0-noble AS forge

WORKDIR /app
COPY pyproject.toml README.md ./
COPY mktlink ./mktlink
RUN pip install --no-cache-dir ".[forge]"
# Качаем антибот-браузер на этапе сборки: минтинг не должен платить за
# загрузку при первом же запросе.
RUN python -m camoufox fetch

ENV PYTHONUNBUFFERED=1 \
    MKTLINK_DB_PATH=/app/data/mktlink.sqlite \
    MKTLINK_FORGE_SOCKET=/app/data/forge.sock

# Один экземпляр: forge — единственный писатель в proxy6, и процесс-локальный
# ограничитель 3 rps корректен только при этом условии.
CMD ["python", "-m", "mktlink.mint.main"]
