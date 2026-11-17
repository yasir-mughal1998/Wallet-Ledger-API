# ---- api: the image that "docker compose up" runs -------------------------
FROM python:3.13-slim AS api

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh && useradd --system --no-create-home wallet
USER wallet

EXPOSE 8080
ENTRYPOINT ["./entrypoint.sh"]

# ---- tools: same code plus test / load-test dependencies -------------------
FROM api AS tools

USER root
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
COPY pytest.ini .
USER wallet

ENTRYPOINT []
CMD ["pytest"]
