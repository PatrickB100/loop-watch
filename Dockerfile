FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV LOOPWATCH_INVENTORY=/app/inventory.yaml \
    LOOPWATCH_DB=/data/loopwatch.sqlite
VOLUME ["/data"]

CMD ["loop-watch", "watch"]
