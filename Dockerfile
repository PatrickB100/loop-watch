FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV LOOPWATCH_INVENTORY=/data/inventory.yaml \
    LOOPWATCH_DB=/data/loopwatch.sqlite \
    LOOPWATCH_WEB_PORT=8080
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('LOOPWATCH_WEB_PORT', '8080')}/healthz\")"

CMD ["loop-watch", "watch"]
