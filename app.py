import logging
import os
import re
import shutil
import subprocess
import tempfile

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse

from converter import SUPPORTED_EXTENSIONS, convert, get_converter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("converter")

MAX_UPLOAD_SIZE = int(os.environ.get("MAX_UPLOAD_SIZE", 50 * 1024 * 1024))  # 50MB
CONVERSION_TIMEOUT = int(os.environ.get("CONVERSION_TIMEOUT", 120))

app = FastAPI(title="Markdown Converter Sidecar")

SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent path traversal and injection."""
    name = os.path.basename(filename)
    if not name or not SAFE_FILENAME_RE.match(name):
        # Strip unsafe characters, keep only safe ones
        name = re.sub(r"[^a-zA-Z0-9._-]", "_", name)
    if not name:
        raise ValueError("Invalid filename")
    return name


@app.get("/health")
async def health():
    pandoc_ok = shutil.which("pandoc") is not None
    try:
        from markitdown import MarkItDown  # noqa: F811
        markitdown_ok = True
    except ImportError:
        markitdown_ok = False

    return {"status": "ok", "pandoc": pandoc_ok, "markitdown": markitdown_ok}


@app.post("/convert")
async def convert_file(
    file: UploadFile = File(...),
    filename: str = Form(...),
):
    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename")

    try:
        safe_name = sanitize_filename(filename)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")

    _, ext = os.path.splitext(safe_name)
    ext = ext.lower()

    if not ext or ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file extension: {ext or '(none)'}",
        )

    # Check file size
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large")

    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, safe_name)

    try:
        with open(tmp_path, "wb") as f:
            f.write(content)

        logger.info("[Converter] Converting %s (%s, %d bytes)", safe_name, ext, len(content))
        markdown = convert(tmp_path, ext, timeout=CONVERSION_TIMEOUT)

        return PlainTextResponse(
            content=markdown,
            media_type="text/markdown; charset=utf-8",
        )

    except ValueError:
        raise HTTPException(status_code=415, detail=f"Unsupported extension: {ext}")
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Conversion timed out")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Conversion timed out")
    except Exception as e:
        logger.error("[Converter] Conversion failed for %s: %s", safe_name, str(e))
        raise HTTPException(status_code=422, detail=f"Conversion failed: {str(e)}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
