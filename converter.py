import subprocess
import logging
import os
import re
import sys

logger = logging.getLogger("converter")


def _extract_exception_message(stderr: str) -> str:
    """Extract the final exception message from a Python traceback.

    Returns the human-readable error (e.g. 'File is not a zip file')
    instead of the full stack trace.  Captures multi-line messages like
    FileConversionException that list individual converter failures.
    """
    lines = stderr.strip().splitlines()
    # Find the last exception line and return everything from it onwards
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r'^[\w.]+(?:Error|Exception|Failure):\s', stripped):
            # Take this line + any continuation lines that follow
            _, _, message = stripped.partition(': ')
            tail = '\n'.join(l.strip() for l in lines[i + 1:] if l.strip())
            full = f"{message}\n{tail}".strip() if tail else (message or stripped)
            return full
    # Fallback: return last non-empty line
    non_empty = [l.strip() for l in lines if l.strip()]
    return non_empty[-1] if non_empty else stderr.strip()

# File magic bytes
_OLE2_MAGIC = b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1'
_ZIP_MAGIC = b'PK\x03\x04'
# Modern Office formats (.xlsx, .pptx, .docx) are ZIP-based.
# When password-protected, Office wraps them in an OLE2 encrypted container.
_ZIP_BASED_EXTENSIONS = {".xlsx", ".pptx", ".docx"}

# Extension-to-converter routing table
PANDOC_EXTENSIONS = {".docx", ".rtf", ".odt", ".txt"}
MARKITDOWN_EXTENSIONS = {".doc", ".pptx", ".xls", ".xlsx", ".pdf"}
SUPPORTED_EXTENSIONS = PANDOC_EXTENSIONS | MARKITDOWN_EXTENSIONS

DEFAULT_TIMEOUT = 120
PANDOC_MAX_HEAP = os.environ.get("PANDOC_MAX_HEAP", "64m")
PANDOC_INITIAL_HEAP = os.environ.get("PANDOC_INITIAL_HEAP", "32m")


def pandoc_to_markdown(input_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Convert a document to Markdown using Pandoc CLI."""
    # -M sets the max heap ceiling (not a reservation); -H sets the initial allocation hint.
    result = subprocess.run(
        ["pandoc", "+RTS", f"-M{PANDOC_MAX_HEAP}", f"-H{PANDOC_INITIAL_HEAP}", "-RTS",
         input_path, "-t", "markdown", "--wrap=none"],
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Pandoc conversion failed: {stderr}")
    return result.stdout.decode("utf-8", errors="replace")


def markitdown_to_markdown(input_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Convert a document to Markdown using MarkItDown in a subprocess.

    Running in a subprocess ensures all memory is returned to the OS when
    the conversion finishes, instead of fragmenting the main process heap.
    """
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; "
            "from markitdown import MarkItDown; "
            "md = MarkItDown(); "
            "r = md.convert(sys.argv[1]); "
            "sys.stdout.buffer.write(r.text_content.encode('utf-8'))",
            input_path,
        ],
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        clean_msg = _extract_exception_message(stderr)
        logger.error("[Converter] MarkItDown stderr: %s", stderr)
        raise RuntimeError(f"MarkItDown conversion failed: {clean_msg}")
    return result.stdout.decode("utf-8", errors="replace")


def get_converter(extension: str) -> str | None:
    """Return the converter name for a given extension, or None if unsupported."""
    ext = extension.lower()
    if ext in PANDOC_EXTENSIONS:
        return "pandoc"
    if ext in MARKITDOWN_EXTENSIONS:
        return "markitdown"
    return None


def _check_password_protected(input_path: str, extension: str) -> None:
    """Raise early if the file appears to be password-protected."""
    ext = extension.lower()
    try:
        with open(input_path, 'rb') as f:
            header = f.read(8)
    except OSError:
        return  # let the converter deal with unreadable files

    if ext in _ZIP_BASED_EXTENSIONS and header.startswith(_OLE2_MAGIC):
        # Password-protected Office files get encrypted into an OLE2 container,
        # so a .xlsx/.pptx/.docx that starts with OLE2 magic instead of ZIP
        # magic (PK) is almost certainly encrypted.  Detecting this upfront
        # avoids the confusing "File is not a zip file" / "Can't find workbook
        # in OLE2 compound document" errors from downstream parsers.
        raise ValueError(
            f"File appears to be password-protected (encrypted Office document)"
        )


def convert(input_path: str, extension: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Route to the appropriate converter based on file extension."""
    _check_password_protected(input_path, extension)
    converter = get_converter(extension)
    if converter == "pandoc":
        logger.info("[Converter] Using Pandoc for %s", extension)
        return pandoc_to_markdown(input_path, timeout=timeout)
    elif converter == "markitdown":
        logger.info("[Converter] Using MarkItDown for %s", extension)
        return markitdown_to_markdown(input_path, timeout=timeout)
    else:
        raise ValueError(f"Unsupported extension: {extension}")
