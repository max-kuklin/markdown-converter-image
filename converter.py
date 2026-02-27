import subprocess
import logging
import os
import re
import shutil
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
_RTF_MAGIC = b'{\\rtf'
# Modern Office formats (.xlsx, .pptx, .docx) are ZIP-based.
# When password-protected, Office wraps them in an OLE2 encrypted container.
_ZIP_BASED_EXTENSIONS = {".xlsx", ".pptx", ".docx"}

# Extension-to-converter routing table
PANDOC_EXTENSIONS = {".docx", ".rtf", ".odt", ".txt"}
MARKITDOWN_EXTENSIONS = {".pptx", ".xls", ".xlsx", ".pdf"}
# .doc is handled separately with a fallback chain (see convert())
SUPPORTED_EXTENSIONS = PANDOC_EXTENSIONS | MARKITDOWN_EXTENSIONS | {".doc"}

DEFAULT_TIMEOUT = 120
PANDOC_MAX_HEAP = os.environ.get("PANDOC_MAX_HEAP", "64m")
PANDOC_INITIAL_HEAP = os.environ.get("PANDOC_INITIAL_HEAP", "32m")


def antiword_to_markdown(input_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Convert a legacy .doc file to plain text using antiword CLI."""
    result = subprocess.run(
        ["antiword", input_path],
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"antiword conversion failed: {stderr}")
    return result.stdout.decode("utf-8", errors="replace")


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


def xls_to_markdown(input_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Convert a legacy .xls file to Markdown using python-calamine in a subprocess.

    xlrd 2.x rejects some .xls files with OLE2 FAT chain issues;
    python-calamine (Rust-based) is more tolerant and faster.
    """
    script = r'''
import sys
from python_calamine import CalamineWorkbook

path = sys.argv[1]
wb = CalamineWorkbook.from_path(path)
parts = []
for name in wb.sheet_names:
    data = wb.get_sheet_by_name(name).to_python()
    if not data:
        continue
    parts.append(f"## {name}")
    for ri, row in enumerate(data):
        cells = [str(c) if c is not None else "" for c in row]
        parts.append("| " + " | ".join(cells) + " |")
        if ri == 0:
            parts.append("| " + " | ".join("---" for _ in cells) + " |")
    parts.append("")
sys.stdout.buffer.write("\n".join(parts).encode("utf-8"))
'''
    result = subprocess.run(
        [sys.executable, "-c", script, input_path],
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        clean_msg = _extract_exception_message(stderr)
        logger.error("[Converter] xls_to_markdown stderr: %s", stderr)
        raise RuntimeError(f"XLS conversion failed: {clean_msg}")
    return result.stdout.decode("utf-8", errors="replace")


def xlsx_to_markdown(input_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Convert an .xlsx file to Markdown using openpyxl directly in a subprocess.

    Avoids MarkItDown's xlsx→HTML→BeautifulSoup pipeline, which hangs on
    spreadsheets with many empty trailing columns (e.g. 16 000+ cols).
    We read only the actual data extent per sheet and emit Markdown tables.
    """
    script = r'''
import sys, openpyxl

path = sys.argv[1]
wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
parts = []
for ws in wb.worksheets:
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        continue
    ncols = max(len(r) for r in rows) if rows else 0
    if ncols == 0:
        continue
    # Count how many rows have data in each column
    col_count = [0] * ncols
    for row in rows:
        for i, c in enumerate(row):
            if c is not None and str(c).strip():
                col_count[i] += 1
    # Find the effective max column: ignore stray outliers separated
    # by a gap of 10+ consecutive empty columns from the main data block.
    max_col = 0
    gap = 0
    for i in range(ncols):
        if col_count[i] > 0:
            max_col = i + 1
            gap = 0
        else:
            gap += 1
            if gap >= 10 and max_col > 0:
                break
    if max_col == 0:
        continue
    # Trim trailing fully-empty rows
    while rows and all(
        (c is None or str(c).strip() == "") for c in rows[-1][:max_col]
    ):
        rows.pop()
    if not rows:
        continue
    parts.append(f"## {ws.title}")
    for ri, row in enumerate(rows):
        cells = [str(c) if c is not None else "" for c in row[:max_col]]
        parts.append("| " + " | ".join(cells) + " |")
        if ri == 0:
            parts.append("| " + " | ".join("---" for _ in cells) + " |")
parts.append("")
wb.close()
sys.stdout.buffer.write("\n".join(parts).encode("utf-8"))
'''
    result = subprocess.run(
        [sys.executable, "-c", script, input_path],
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        clean_msg = _extract_exception_message(stderr)
        logger.error("[Converter] xlsx_to_markdown stderr: %s", stderr)
        raise RuntimeError(f"XLSX conversion failed: {clean_msg}")
    return result.stdout.decode("utf-8", errors="replace")


def get_converter(extension: str) -> str | None:
    """Return the converter name for a given extension, or None if unsupported."""
    ext = extension.lower()
    if ext in PANDOC_EXTENSIONS:
        return "pandoc"
    if ext == ".xlsx":
        return "xlsx"
    if ext == ".xls":
        return "xls"
    if ext in MARKITDOWN_EXTENSIONS or ext == ".doc":
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


def _detect_doc_format(input_path: str) -> str:
    """Sniff the actual format of a .doc file.

    Returns 'rtf', 'ole2', or 'unknown'.
    Many .doc files are actually RTF saved with a .doc extension.
    True legacy Word documents use the OLE2 binary format.
    """
    try:
        with open(input_path, 'rb') as f:
            header = f.read(8)
    except OSError:
        return 'unknown'
    if header.startswith(_RTF_MAGIC):
        return 'rtf'
    if header.startswith(_OLE2_MAGIC):
        return 'ole2'
    return 'unknown'


def _convert_doc(input_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Convert a .doc file with format detection and fallback.

    .doc files can be RTF (Pandoc handles well) or OLE2 binary Word
    (MarkItDown may handle).  We sniff the content and try the best
    converter first, falling back to the other if it fails.
    """
    fmt = _detect_doc_format(input_path)

    if fmt == 'rtf':
        # RTF masquerading as .doc — Pandoc handles this natively
        logger.info("[Converter] .doc is RTF, using Pandoc")
        return pandoc_to_markdown(input_path, timeout=timeout)

    # OLE2 binary or unknown — try antiword first (purpose-built for .doc),
    # then MarkItDown, then Pandoc as final fallback.
    if shutil.which("antiword"):
        logger.info("[Converter] .doc is %s format, trying antiword", fmt)
        try:
            return antiword_to_markdown(input_path, timeout=timeout)
        except RuntimeError as e:
            logger.warning("[Converter] antiword failed for .doc: %s", e)

    logger.info("[Converter] .doc is %s format, trying MarkItDown", fmt)
    try:
        return markitdown_to_markdown(input_path, timeout=timeout)
    except RuntimeError as e:
        logger.warning("[Converter] MarkItDown failed for .doc, trying Pandoc fallback: %s", e)
    try:
        return pandoc_to_markdown(input_path, timeout=timeout)
    except RuntimeError:
        pass

    raise RuntimeError(
        "Unable to convert .doc file. The legacy binary Word format (.doc) "
        "has limited conversion support. Try re-saving as .docx."
    )


def convert(input_path: str, extension: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Route to the appropriate converter based on file extension."""
    _check_password_protected(input_path, extension)
    ext = extension.lower()

    if ext == ".doc":
        return _convert_doc(input_path, timeout=timeout)

    converter = get_converter(ext)
    if converter == "pandoc":
        logger.info("[Converter] Using Pandoc for %s", extension)
        return pandoc_to_markdown(input_path, timeout=timeout)
    elif converter == "xlsx":
        logger.info("[Converter] Using openpyxl for %s", extension)
        return xlsx_to_markdown(input_path, timeout=timeout)
    elif converter == "xls":
        logger.info("[Converter] Using calamine for %s", extension)
        return xls_to_markdown(input_path, timeout=timeout)
    elif converter == "markitdown":
        logger.info("[Converter] Using MarkItDown for %s", extension)
        return markitdown_to_markdown(input_path, timeout=timeout)
    else:
        raise ValueError(f"Unsupported extension: {extension}")
