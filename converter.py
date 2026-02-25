import subprocess
import logging
import os
import sys

logger = logging.getLogger("converter")

# Extension-to-converter routing table
PANDOC_EXTENSIONS = {".docx", ".rtf", ".odt", ".txt"}
MARKITDOWN_EXTENSIONS = {".pptx", ".xlsx", ".pdf"}
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
        raise RuntimeError(f"MarkItDown conversion failed: {stderr}")
    return result.stdout.decode("utf-8", errors="replace")


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
