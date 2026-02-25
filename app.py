import asyncio
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import tempfile

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse

from converter import SUPPORTED_EXTENSIONS, convert, get_converter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("converter")

MAX_UPLOAD_SIZE = int(os.environ.get("MAX_UPLOAD_SIZE", 50 * 1024 * 1024))  # 50MB
CONVERSION_TIMEOUT = int(os.environ.get("CONVERSION_TIMEOUT", 120))
MAX_CONCURRENT_CONVERSIONS = int(os.environ.get("MAX_CONCURRENT_CONVERSIONS", 2))
MAX_QUEUED_CONVERSIONS = int(os.environ.get("MAX_QUEUED_CONVERSIONS", 5))

conversion_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CONVERSIONS)
# Bounds total in-flight requests (active + queued)
_queue_slots = asyncio.Semaphore(MAX_CONCURRENT_CONVERSIONS + MAX_QUEUED_CONVERSIONS)

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
    markitdown_ok = importlib.util.find_spec("markitdown") is not None

    return {"status": "ok", "pandoc": pandoc_ok, "markitdown": markitdown_ok}


@app.post("/convert")
async def convert_file(
    request: Request,
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

    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, safe_name)

    try:
        # Stream the file to disk to avoid loading it entirely into memory
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        # Check file size after writing
        file_size = os.path.getsize(tmp_path)
        if file_size > MAX_UPLOAD_SIZE:
            raise HTTPException(status_code=413, detail="File too large")

        logger.info("[Converter] Converting %s (%s, %d bytes)", safe_name, ext, file_size)

        # Reject immediately if the queue is full (active + waiting >= limit)
        if _queue_slots.locked():
            raise HTTPException(status_code=429, detail="Too many conversion requests queued")

        await _queue_slots.acquire()
        try:
            # Wait for a conversion slot; check for client disconnect while queued
            while True:
                try:
                    await asyncio.wait_for(conversion_semaphore.acquire(), timeout=1.0)
                    break
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        logger.info("[Converter] Client disconnected while queued: %s", safe_name)
                        return PlainTextResponse(content="", status_code=499)

            try:
                # Run conversion in a thread; periodically check for disconnect
                loop = asyncio.get_event_loop()
                task = loop.run_in_executor(None, convert, tmp_path, ext, CONVERSION_TIMEOUT)
                while True:
                    done, _ = await asyncio.wait({task}, timeout=2.0)
                    if done:
                        markdown = task.result()
                        break
                    if await request.is_disconnected():
                        logger.info("[Converter] Client disconnected during conversion: %s", safe_name)
                        return PlainTextResponse(content="", status_code=499)
            finally:
                conversion_semaphore.release()
        finally:
            _queue_slots.release()

        # Explicitly delete the file before returning the response to free disk space immediately
        shutil.rmtree(tmp_dir, ignore_errors=True)

        return PlainTextResponse(
            content=markdown,
            media_type="text/markdown; charset=utf-8",
        )

    except ValueError as e:
        detail = str(e) if str(e) else f"Unsupported extension: {ext}"
        raise HTTPException(status_code=415, detail=detail)
    except HTTPException:
        raise
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Conversion timed out")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Conversion timed out")
    except Exception as e:
        logger.error("[Converter] Conversion failed for %s: %s", safe_name, str(e))
        raise HTTPException(status_code=422, detail=f"Conversion failed: {str(e)}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
