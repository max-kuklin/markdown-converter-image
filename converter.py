import subprocess
import logging

from markitdown import MarkItDown

logger = logging.getLogger("converter")

# Extension-to-converter routing table
PANDOC_EXTENSIONS = {".docx", ".doc", ".rtf", ".odt", ".ods", ".txt"}
MARKITDOWN_EXTENSIONS = {".pptx", ".ppt", ".xls", ".xlsx", ".pdf"}
SUPPORTED_EXTENSIONS = PANDOC_EXTENSIONS | MARKITDOWN_EXTENSIONS

DEFAULT_TIMEOUT = 120


def pandoc_to_markdown(input_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Convert a document to Markdown using Pandoc CLI."""
    # Use +RTS -M64m -RTS to limit Pandoc's memory usage to 64MB
    result = subprocess.run(
        ["pandoc", "+RTS", "-M64m", "-RTS", input_path, "-t", "markdown", "--wrap=none"],
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Pandoc conversion failed: {stderr}")
    
    # Decode the output and explicitly delete the result object to free memory
    markdown_output = result.stdout.decode("utf-8")
    del result
    return markdown_output


def markitdown_to_markdown(input_path: str) -> str:
    """Convert a document to Markdown using MarkItDown."""
    md = MarkItDown()
    result = md.convert(input_path)
    
    # Extract text and explicitly delete the result object to free memory
    markdown_output = result.text_content
    del result
    return markdown_output


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
        return markitdown_to_markdown(input_path)
    else:
        raise ValueError(f"Unsupported extension: {extension}")
