# --- builder stage: install Python deps with C extensions ---
FROM python:3.12-slim AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc g++ libxml2-dev libxslt1-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /build/wheels -r requirements.txt

# --- final stage: runtime only ---
FROM python:3.12-slim

ARG PANDOC_VERSION=3.6.4
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl antiword libxml2 libxslt1.1 && \
    curl -fsSL "https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-linux-amd64.tar.gz" \
    | tar xz --strip-components=2 -C /usr/local/bin pandoc-${PANDOC_VERSION}/bin/pandoc && \
    apt-get purge -y --auto-remove curl && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/wheels /tmp/wheels
RUN pip install --no-cache-dir --no-compile --no-index --find-links=/tmp/wheels /tmp/wheels/*.whl && \
    rm -rf /tmp/wheels

RUN groupadd --system app && useradd --system --gid app app

WORKDIR /app
COPY app.py converter.py ./

RUN chown -R app:app /app
USER app

EXPOSE 8100
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100", "--timeout-keep-alive", "130"]
