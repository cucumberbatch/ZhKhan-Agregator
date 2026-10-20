# ZhKhan-Agregator — образ для облачной платформы.
#
# Собирается под Linux (Cloud Run, Compute Engine, Oracle A1).
# Локально на Windows: docker build -t zhkhan .
#
# Внутри: Python 3.10, Chromium для Playwright, OCR-модель ddddocr.

FROM python:3.10-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Chromium ищет браузер здесь; без этой переменной Playwright
    # ищет его в домашнем каталоге, которого у контейнера нет.
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    # База лежит на подключённом томе, а не в слое контейнера:
    # иначе данные исчезают при каждом пересоздании инстанса.
    MAPTOOL_DB=/data/maptool.db

WORKDIR /app

# Сначала зависимости отдельным слоем: пересборка кода не должна
# тянуть за собой переустановку Chromium.
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    # zoneinfo ищет базу часовых поясов в системе, а в slim-образе
    # её нет: без этого Asia/Almaty не находится, и расписание
    # обновления считается в UTC. Пакет tzdata — запасной источник.
    && pip install tzdata

# Chromium с системными библиотеками. --with-deps тянет ~500 МБ
# apt-пакетов, поэтому отдельным слоем.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# Каталог для данных: на Cloud Run это смонтированный том,
# в Docker — обычный volume.
RUN mkdir -p /data && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8080

# Cloud Run проверяет живость через HTTP, поэтому нужен именно HTTP.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/telegram',timeout=4).status==200 else 1)"

# Порт берётся из PORT: Cloud Run задаёт 8080.
CMD ["python", "server.py"]
