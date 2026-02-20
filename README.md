# Markdown Converter Sidecar

A lightweight HTTP sidecar that converts documents to Markdown using [Pandoc](https://pandoc.org/) and [MarkItDown](https://github.com/microsoft/markitdown). Designed to run alongside a Node.js application as a sidecar container.

## Supported Formats

| Extension | Converter |
|-----------|-----------|
| `.docx`, `.doc`, `.rtf`, `.odt`, `.ods`, `.txt` | Pandoc |
| `.pptx`, `.ppt` | MarkItDown |
| `.xls`, `.xlsx` | MarkItDown |
| `.pdf` | MarkItDown |

## API

**`POST /convert`** — Convert a document to Markdown

```bash
curl -F "file=@document.docx" -F "filename=document.docx" http://localhost:8100/convert
```

Returns `text/markdown` on success. Status codes: `415` (unsupported format), `422` (conversion failed), `504` (timeout).

**`GET /health`** — Health check

```bash
curl http://localhost:8100/health
# {"status": "ok", "pandoc": true, "markitdown": true}
```

## Running

### Docker Compose

```bash
docker compose -f docker-compose.test.yml up
```

### Standalone

```bash
pip install -r requirements.txt
uvicorn app:app --port 8100
```

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `MAX_UPLOAD_SIZE` | `52428800` (50MB) | Maximum upload size in bytes |
| `CONVERSION_TIMEOUT` | `120` | Subprocess timeout in seconds |

## Testing

```bash
pip install pytest httpx
python -m pytest test_converter.py -v
```

## Tech Stack

Python 3.12 · FastAPI · Pandoc · MarkItDown
