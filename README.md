# Markdown Converter Sidecar

A lightweight HTTP sidecar that converts documents to Markdown using [Pandoc](https://pandoc.org/) and [MarkItDown](https://github.com/microsoft/markitdown). Designed to run alongside a Node.js application as a sidecar container.

## Supported Formats

| Extension | Converter |
|-----------|-----------|
| `.docx`, `.rtf`, `.odt`, `.txt` | Pandoc |
| `.doc` | Auto-detected: RTF → Pandoc, OLE2 binary → antiword → MarkItDown → Pandoc fallback chain |
| `.pptx` | MarkItDown |
| `.xls` | python-calamine (direct) |
| `.xlsx` | openpyxl (direct) |
| `.pdf` | MarkItDown |

Password-protected Office files (`.docx`, `.xlsx`, `.pptx`) are detected and rejected early.

## API

**`POST /convert`** — Convert a document to Markdown

```bash
curl -F "file=@document.docx" -F "filename=document.docx" http://localhost:8100/convert
```

Returns `text/markdown` on success.

| Status | Meaning |
|--------|---------|
| `200` | Success |
| `400` | Missing or invalid filename |
| `413` | File too large |
| `415` | Unsupported format or password-protected file |
| `422` | Conversion failed |
| `429` | Too many conversion requests queued |
| `504` | Conversion timed out |

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
| `MAX_CONCURRENT_CONVERSIONS` | `2` | Maximum parallel conversions |
| `MAX_QUEUED_CONVERSIONS` | `5` | Maximum requests waiting in queue |
| `PANDOC_MAX_HEAP` | `64m` | Pandoc RTS max heap size (`-M`) |
| `PANDOC_INITIAL_HEAP` | `32m` | Pandoc RTS initial heap hint (`-H`) |

For default values above, container memory limits should be set to at least 512MB to avoid OOM errors.

## Testing

```bash
pip install pytest httpx
python -m pytest test_converter.py -v
```

## Tech Stack

Python 3.12 · FastAPI · Pandoc · MarkItDown · antiword
