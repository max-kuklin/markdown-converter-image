# PRD: Markdown Converter Image

## Goal

Replace the in-process Pandoc CLI wrapper with a dedicated sidecar container that hosts both Pandoc and MarkItDown behind a single HTTP API. This unblocks XLS/XLSX support (currently excluded because Pandoc cannot handle them) and improves PDF text extraction quality, while keeping the Node.js image slim and decoupling converter lifecycle from application deployments.

## Scope

### Included
- Python-based HTTP service (FastAPI) hosting Pandoc and MarkItDown converters
- Single `/convert` endpoint accepting file bytes and returning Markdown text
- Extension-based routing to the appropriate converter inside the sidecar
- Dockerfile for the sidecar container (`Dockerfile.converter`)
- Sidecar entry in `docker-compose.test.yml` for local development and integration testing
- `common/services/document-converter.js` — new Node.js HTTP client replacing direct Pandoc subprocess calls
- Deprecation and removal of `common/services/pandoc.js` subprocess wrapper
- Enable `.xls` and `.xlsx` in the discovery `ALLOWED_EXTENSIONS`
- Configuration for the converter service URL and timeouts
- Health check endpoint on the sidecar (`GET /health`)

### Excluded
- Kubernetes manifests (out of scope — infrastructure team handles K8s sidecar injection)
- Authentication between Node.js and sidecar (localhost-only in both K8s pod and Docker Compose)
- OCR support for scanned PDFs (future enhancement)
- Fallback chains (if primary converter fails, candidate is marked `conversion_failed` — same as today)
- Changes to the extraction loop logic in `extraction.service.js` beyond swapping the converter import

## Implementation Details

### Sidecar Container

**Tech stack:** Python 3.12-slim, FastAPI, uvicorn, Pandoc (apt), `markitdown[pdf,xlsx,pptx,docx]`

Using selective MarkItDown extras (`pdf,xlsx,pptx,docx`) instead of `markitdown[all]` to minimize image size — avoids pulling in unnecessary dependencies like audio/video transcription libraries.

**`POST /convert`**

Request:
- `Content-Type: multipart/form-data`
- Field `file`: the document bytes
- Field `filename`: original filename (used for extension-based routing)

Response:
- `200 OK` with `Content-Type: text/markdown; charset=utf-8` — Markdown text in body
- `400 Bad Request` — missing file or filename
- `415 Unsupported Media Type` — extension not supported by any converter
- `422 Unprocessable Entity` — conversion failed (corrupt file, password-protected, etc.)
- `504 Gateway Timeout` — conversion exceeded the server-side timeout

**`GET /health`**

Response: `200 OK` with `{"status": "ok", "pandoc": true, "markitdown": true}`

Validates both tools are functional at startup and on each health check by checking CLI/library availability.

**Extension routing inside the sidecar:**

| Extension | Converter | Rationale |
|-----------|-----------|-----------|
| `.docx`, `.doc`, `.rtf`, `.odt`, `.ods`, `.txt` | Pandoc | Well-supported text/markup formats |
| `.pptx`, `.ppt` | MarkItDown | MarkItDown extracts slide text more reliably than Pandoc |
| `.xls`, `.xlsx` | MarkItDown | Pandoc has no spreadsheet support |
| `.pdf` | MarkItDown | Uses pdfminer — better text extraction than Pandoc for PDFs |

The routing table is defined in the sidecar code. The Node.js side does not need to know which converter is used — it sends the file and gets Markdown back.

**Temp file handling:**
- Incoming file bytes are written to a temp directory
- Converter runs against the temp file
- Temp files are cleaned up in a `finally` block after every request
- Temp directory is pod-ephemeral storage — no persistent disk needed

**Timeout:**
- Uvicorn request timeout configured via `--timeout-keep-alive`
- Per-conversion subprocess timeout enforced inside the Python handler (configurable, default 120s)
- The handler kills the converter subprocess if it exceeds the timeout and returns 504

### Sidecar Source Location

```
Dockerfile
app.py            # FastAPI application
converter.py      # Pandoc and MarkItDown wrapper functions
requirements.txt  # Python dependencies
test_converter.py # pytest unit tests
```

Placed at the repository root alongside `Dockerfile` (the main Node.js app). The sidecar is an independent build artifact.

### `app.py` — API structure

- FastAPI app with `/convert` and `/health` endpoints
- Accepts multipart upload, extracts extension from filename, routes to converter
- Returns Markdown or appropriate error status
- Structured logging with `[Converter]` prefix to match project conventions

### `converter.py` — Converter functions

Two functions:
- `pandoc_to_markdown(input_path: str, timeout: int) -> str` — calls `pandoc` CLI via `subprocess.run` with `capture_output=True` and `timeout`
- `markitdown_to_markdown(input_path: str) -> str` — uses MarkItDown Python API (`markitdown.MarkItDown().convert(input_path).text_content`)

Both raise on failure. The API layer catches and returns appropriate HTTP status.

### `Dockerfile`

```dockerfile
FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends pandoc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8100
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100", "--timeout-keep-alive", "130"]
```

### `requirements.txt`

```
fastapi>=0.115,<1
uvicorn[standard]>=0.34,<1
python-multipart>=0.0.18
markitdown[pdf,xlsx,pptx,docx]>=0.1,<1
```

### Docker Compose — Local Development

Add to `docker-compose.test.yml`:

```yaml
converter:
  build:
    context: ./converter
    dockerfile: Dockerfile
  container_name: converter
  ports:
    - "8100:8100"
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8100/health"]
    interval: 10s
    timeout: 5s
    retries: 3
    start_period: 10s
```

For local dev without Docker, developers can run the converter directly:
```
cd converter && pip install -r requirements.txt && uvicorn app:app --port 8100
```

### Kubernetes Deployment Notes (Reference Only)

For the infrastructure team — not implemented by this PRD:

- Deploy as a sidecar container in the same pod as the Node.js app
- Port 8100, no external ingress needed
- Resource suggestions: `requests: 256Mi/200m`, `limits: 1Gi/1000m` (conversion is CPU-heavy)
- Liveness probe: `GET /health` every 30s
- Readiness probe: `GET /health` every 10s
- Ephemeral storage for temp files — no PVC needed

## Acceptance Criteria

- The converter sidecar starts and responds to `GET /health` with status 200
- `POST /convert` with a `.docx` file returns Markdown via Pandoc
- `POST /convert` with a `.xlsx` file returns Markdown via MarkItDown
- `POST /convert` with a `.pdf` file returns Markdown via MarkItDown
- `POST /convert` with a `.pptx` file returns Markdown via MarkItDown
- `POST /convert` with an unsupported extension (e.g., `.zip`) returns 415
- `POST /convert` with a corrupt file returns 422
- Temp files are cleaned up after every conversion (success and failure)
- The Node.js `document-converter.js` client successfully calls the sidecar and returns Markdown
- The Node.js client throws with a clear error message on HTTP failures
- The extraction service uses `document-converter.js` instead of `pandoc.js`
- `.xls` and `.xlsx` appear in discovery `ALLOWED_EXTENSIONS`
- `common/services/pandoc.js` and `common/services/pandoc.test.js` are removed
- The main `Dockerfile` has no Python or Pandoc dependencies
- `docker-compose.test.yml` includes the converter service and it starts successfully
- Conversion timeout is enforced — a hung conversion returns 504 and does not leak processes

## Testing Plan

### Test scope: Unit + Curl Validation

**Unit tests (Python — `converter/test_converter.py`):**
- Test extension-to-converter routing (each extension maps correctly)
- Test unsupported extension returns 415
- Test health endpoint returns expected structure
- Mock subprocess/MarkItDown calls to test error handling paths
- Test temp file cleanup on success and failure

**Curl validation (manual — after sidecar is running):**
1. Start converter via `docker compose up converter`
2. Health: `curl http://localhost:8100/health` → 200, JSON with status ok
3. DOCX: `curl -F "file=@sample.docx" -F "filename=sample.docx" http://localhost:8100/convert` → 200, Markdown
4. XLSX: `curl -F "file=@sample.xlsx" -F "filename=sample.xlsx" http://localhost:8100/convert` → 200, Markdown
5. PDF: `curl -F "file=@sample.pdf" -F "filename=sample.pdf" http://localhost:8100/convert` → 200, Markdown
6. Unsupported: `curl -F "file=@sample.zip" -F "filename=sample.zip" http://localhost:8100/convert` → 415

## Security Review

- **No authentication on sidecar** — acceptable because it only binds to localhost (K8s pod network / Docker Compose internal network). Not exposed via ingress.
- **Temp file exposure** — files are written to the container's ephemeral tmpdir and cleaned up in `finally` blocks. The sidecar container runs as a non-root user (add `USER` directive to Dockerfile).
- **Subprocess injection** — Pandoc is invoked via `subprocess.run` with an explicit argument list (not shell=True). Filenames are sanitized (alphanumeric, dots, hyphens, underscores only) before use in temp paths.
- **Path traversal** — filename is sanitized server-side before constructing temp file paths. `os.path.basename()` + regex sanitization prevents directory traversal.
- **Denial of service** — large files could exhaust memory or disk. Enforce max upload size in FastAPI (configurable, default 50MB). Subprocess timeout kills hung conversions.
- **Dependency supply chain** — pin major versions in `requirements.txt`. Use `pip install --no-cache-dir` to avoid stale caches. Image built from `python:3.12-slim` (official, maintained).
- **No secrets** — the sidecar has no access to `.env`, database, or external APIs. It only transforms documents.

## References

- [MarkItDown (Microsoft)](https://github.com/microsoft/markitdown) — Python library for converting documents to Markdown
- [Pandoc](https://pandoc.org/) — universal document converter
