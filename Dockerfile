FROM python:3.12-slim

ARG PANDOC_VERSION=3.6.4
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    curl -fsSL "https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-linux-amd64.tar.gz" \
    | tar xz --strip-components=2 -C /usr/local/bin pandoc-${PANDOC_VERSION}/bin/pandoc && \
    apt-get purge -y --auto-remove curl && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --system app && useradd --system --gid app app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py converter.py ./

RUN chown -R app:app /app
USER app

EXPOSE 8100
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100", "--timeout-keep-alive", "130"]
