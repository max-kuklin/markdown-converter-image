import subprocess
import logging
import sys
import tempfile
import os

logger = logging.getLogger("converter")

# Extension-to-converter routing table
PANDOC_EXTENSIONS = {".docx", ".rtf", ".odt", ".txt"}
MARKITDOWN_EXTENSIONS = {".pptx", ".xlsx", ".pdf"}
SUPPORTED_EXTENSIONS = PANDOC_EXTENSIONS | MARKITDOWN_EXTENSIONS

DEFAULT_TIMEOUT = 120


def pandoc_to_markdown(input_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Convert a document to Markdown using Pandoc CLI."""
    # Stream pandoc stdout to a temp file to avoid buffering the entire output in memory.
    # Use +RTS -M32m -H8m -RTS to limit Pandoc's heap to 32MB with an 8MB initial allocation.
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".md", delete=False) as out_f:
        out_path = out_f.name
    try:
        result = subprocess.run(
            ["pandoc", "+RTS", "-M32m", "-H8m", "-RTS", input_path,
             "-t", "markdown", "--wrap=none", "-o", out_path],
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Pandoc conversion failed: {stderr}")
        del result
        with open(out_path, "r", encoding="utf-8") as f:
            return f.read()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def markitdown_to_markdown(input_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Convert a document to Markdown using MarkItDown in a subprocess.

    Running in a subprocess ensures all memory is returned to the OS when
    the conversion finishes, instead of fragmenting the main process heap.
    """
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".md", delete=False) as out_f:
        out_path = out_f.name
    try:
        result = subprocess.run(
            [
                sys.executable, "-c",
                "import sys, os; "
                "from markitdown import MarkItDown; "
                "md = MarkItDown(); "
                "r = md.convert(sys.argv[1]); "
                "open(sys.argv[2], 'w', encoding='utf-8').write(r.text_content)",
                input_path,
                out_path,
            ],
            capture_output=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"MarkItDown conversion failed: {stderr}")
        del result
        with open(out_path, "r", encoding="utf-8") as f:
            return f.read()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def get_converter(extension: str) -> str | None:
    """Return the converter name for a given extension, or None if unsupported."""
    ext = extension.lower()
    if ext in PANDOC_EXTENSIONS:
        return "pandoc"
    if ext in MARKITDOWN_EXTENSIONS:
        return "markitdown"
    return None


def convert(input_path: str, extension: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Route to the appropriate converter based on file extension."""
    converter = get_converter(extension)
    if converter == "pandoc":
        logger.info("[Converter] Using Pandoc for %s", extension)
        return pandoc_to_markdown(input_path, timeout=timeout)
    elif converter == "markitdown":
        logger.info("[Converter] Using MarkItDown for %s", extension)
        return markitdown_to_markdown(input_path, timeout=timeout)
    else:
        raise ValueError(f"Unsupported extension: {extension}")
