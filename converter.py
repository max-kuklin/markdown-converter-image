import subprocess
import logging
import os
import re
import shutil
import signal
import sys
import threading
import time

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
PANDOC_EXTENSIONS = {".rtf", ".odt", ".txt", ".docx"}
MARKITDOWN_EXTENSIONS = {".pptx", ".xls", ".xlsx", ".pdf"}
# .doc is handled separately with a fallback chain (see convert())
SUPPORTED_EXTENSIONS = PANDOC_EXTENSIONS | MARKITDOWN_EXTENSIONS | {".doc"}

DEFAULT_TIMEOUT = 120
# Pandoc's RSS runs ~100 MB above its heap cap, so 230m stays under the 350 MB watchdog.
PANDOC_MAX_HEAP = os.environ.get("PANDOC_MAX_HEAP", "230m")

# Caps the streaming .xlsx fallback so sparse raw-data sheets can't flood the parent process.
XLSX_FALLBACK_MAX_OUTPUT_MB = 20
_OUTPUT_LIMIT_EXIT_CODE = 3

# RSS limit for converter subprocesses in MB.  The parent polls the child's
# RSS and kills it (SIGKILL) if it exceeds this threshold.
# Set to 0 to disable.  Only enforced on Linux (inside the container).
# For a 512 MB container, 350 MB leaves ~130 MB for the parent process + OS.
SUBPROCESS_MEMORY_LIMIT_MB = int(os.environ.get("SUBPROCESS_MEMORY_LIMIT_MB", 350))

# How often (seconds) to check the child's RSS while it runs.
# procfs reads are negligible (~microseconds per PID in the container),
# so polling at 100ms adds no meaningful overhead.
_RSS_POLL_INTERVAL = 0.1


class MemoryLimitExceeded(Exception):
    """Raised when a conversion subprocess exceeds its memory limit."""


def _get_rss_bytes(pid: int) -> int | None:
    """Read resident set size of *pid* from /proc on Linux.

    Returns RSS in bytes, or None if unavailable (non-Linux / process gone).
    """
    try:
        with open(f"/proc/{pid}/statm", "rb") as f:
            # statm fields: size resident shared text lib data dt  (pages)
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


def _get_process_group_rss_bytes(pgid: int) -> int | None:
    """Sum RSS of all processes in process group *pgid* via /proc.

    This captures the direct child plus any grandchildren (e.g. if
    markitdown or pandoc spawns helper processes).
    Returns total RSS in bytes, or None if /proc is unavailable.
    """
    page_size = os.sysconf("SC_PAGE_SIZE")
    total = 0
    found = False
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as f:
                    stat_data = f.read()
                # Parse: pid (comm) state ppid pgrp ...
                # comm can contain spaces/parens, so find the last ')' first
                close_paren = stat_data.rindex(b")")
                fields_after = stat_data[close_paren + 2:].split()
                # fields_after[0]=state, [1]=ppid, [2]=pgrp
                pgrp = int(fields_after[2])
                if pgrp != pgid:
                    continue
                with open(f"/proc/{entry}/statm", "rb") as f:
                    pages = int(f.read().split()[1])
                total += pages * page_size
                found = True
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return None
    return total if found else None


def _run_with_memory_limit(
    cmd: list,
    timeout: int,
    converter_name: str,
) -> subprocess.CompletedProcess:
    """Run *cmd* as a subprocess, killing it if RSS exceeds the limit.

    The child is started in its own process group (``start_new_session``),
    so the watchdog sums RSS across the whole group — covering any
    grandchild processes the converter may spawn.

    On non-Linux or when the limit is disabled this falls back to plain
    subprocess.run() with the same timeout.
    """
    limit_bytes = SUBPROCESS_MEMORY_LIMIT_MB * 1024 * 1024
    use_watchdog = (
        sys.platform != "win32"
        and limit_bytes > 0
        and os.path.isdir("/proc")
    )

    if not use_watchdog:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        _check_memory_failure(result, converter_name)
        return result

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pgid = os.getpgid(proc.pid)
    killed_for_memory = False
    deadline = time.monotonic() + timeout

    # Drain stdout/stderr in background threads to prevent pipe-buffer
    # deadlock.  Without this, a child that writes more than the OS pipe
    # buffer (~64 KB on Linux) blocks on write() while the parent blocks
    # on wait() — neither side makes progress.
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    def _drain(pipe, sink: list[bytes]):
        while True:
            chunk = pipe.read(65536)
            if not chunk:
                break
            sink.append(chunk)

    stdout_thread = threading.Thread(target=_drain, args=(proc.stdout, stdout_chunks))
    stderr_thread = threading.Thread(target=_drain, args=(proc.stderr, stderr_chunks))
    stdout_thread.daemon = True
    stderr_thread.daemon = True
    stdout_thread.start()
    stderr_thread.start()

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.killpg(pgid, signal.SIGKILL)
                proc.wait()
                raise subprocess.TimeoutExpired(cmd, timeout)

            try:
                proc.wait(timeout=min(_RSS_POLL_INTERVAL, remaining))
                break  # process finished
            except subprocess.TimeoutExpired:
                pass  # still running — check RSS

            rss = _get_process_group_rss_bytes(pgid)
            if rss is not None and rss > limit_bytes:
                logger.warning(
                    "[Converter] %s exceeded RSS limit (%d MB > %d MB), killing",
                    converter_name, rss // (1024 * 1024),
                    SUBPROCESS_MEMORY_LIMIT_MB,
                )
                os.killpg(pgid, signal.SIGKILL)
                proc.wait()
                killed_for_memory = True
                break
    except Exception:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.wait()
        raise

    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    stdout = b"".join(stdout_chunks)
    stderr = b"".join(stderr_chunks)
    result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)

    if killed_for_memory:
        raise MemoryLimitExceeded(
            f"{converter_name}: process killed (RSS exceeded "
            f"{SUBPROCESS_MEMORY_LIMIT_MB} MB limit)"
        )

    _check_memory_failure(result, converter_name)
    return result


def _check_memory_failure(result: subprocess.CompletedProcess,
                          converter_name: str) -> None:
    """Raise MemoryLimitExceeded if the subprocess died from memory exhaustion."""
    if result.returncode == 0:
        return
    # Killed by signal 9 (OOM killer)
    if result.returncode in (-9, 137):
        raise MemoryLimitExceeded(
            f"{converter_name}: process killed (out of memory)"
        )
    stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
    if "MemoryError" in stderr or "Cannot allocate memory" in stderr:
        raise MemoryLimitExceeded(f"{converter_name}: out of memory")
    if "Heap exhausted" in stderr:
        raise MemoryLimitExceeded(
            f"{converter_name}: out of memory (heap exhausted)"
        )


def antiword_to_markdown(input_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Convert a legacy .doc file to plain text using antiword CLI."""
    result = _run_with_memory_limit(
        ["antiword", input_path],
        timeout=timeout,
        converter_name="antiword",
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"antiword conversion failed: {stderr}")
    return result.stdout.decode("utf-8", errors="replace")


def pandoc_to_markdown(input_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Convert a document to Markdown using Pandoc CLI."""
    result = _run_with_memory_limit(
        ["pandoc", "+RTS", f"-M{PANDOC_MAX_HEAP}", "-RTS",
         input_path, "-t", "markdown", "--wrap=none"],
        timeout=timeout,
        converter_name="pandoc",
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
    result = _run_with_memory_limit(
        [
            sys.executable, "-c",
            "import sys; "
            "from markitdown import MarkItDown; "
            "md = MarkItDown(); "
            "r = md.convert(sys.argv[1]); "
            "sys.stdout.buffer.write(r.text_content.encode('utf-8'))",
            input_path,
        ],
        timeout=timeout,
        converter_name="markitdown",
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
    result = _run_with_memory_limit(
        [sys.executable, "-c", script, input_path],
        timeout=timeout,
        converter_name="xls_to_markdown",
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        if "PasswordError" in stderr:
            raise ValueError("File appears to be password-protected (encrypted Office document)")
        clean_msg = _extract_exception_message(stderr)
        logger.error("[Converter] xls_to_markdown stderr: %s", stderr)
        raise RuntimeError(f"XLS conversion failed: {clean_msg}")
    return result.stdout.decode("utf-8", errors="replace")


def _xlsx_to_markdown_xml(input_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Fallback: stream .xlsx to Markdown using stdlib zipfile + xml.etree.iterparse.

    Used when calamine cannot parse the file or runs out of memory (calamine
    allocates the full used range, which a single stray cell in column XFD
    turns into millions of cells).  Rows are streamed, trailing empty cells
    trimmed and empty rows skipped, so memory stays flat.
    """
    script = r'''
import re, sys, zipfile, xml.etree.ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
WS_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
COL_RE = re.compile(r"[A-Z]+")

path, max_bytes, limit_exit_code = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
written = 0


def emit(line):
    global written
    data = (line + "\n").encode("utf-8")
    written += len(data)
    if written > max_bytes:
        sys.exit(limit_exit_code)
    sys.stdout.buffer.write(data)


def col_index(ref):
    n = 0
    for ch in COL_RE.match(ref)[0]:
        n = n * 26 + ord(ch) - 64
    return n - 1


def text_of(el):
    return "".join(t.text or "" for t in el.iter(NS + "t"))


with zipfile.ZipFile(path) as z:
    names = set(z.namelist())
    shared = []
    if "xl/sharedStrings.xml" in names:
        with z.open("xl/sharedStrings.xml") as fh:
            for _, el in ET.iterparse(fh):
                if el.tag == NS + "si":
                    shared.append(text_of(el))
                    el.clear()

    rels = ET.parse(z.open("xl/_rels/workbook.xml.rels"))
    rid_map = {}
    for rel in rels.iter(REL_NS + "Relationship"):
        if WS_TYPE in rel.get("Type", ""):
            t = rel.get("Target")
            if t.startswith("/"):
                t = t[1:]
            elif not t.startswith("xl/"):
                t = "xl/" + t
            rid_map[rel.get("Id")] = t

    wb = ET.parse(z.open("xl/workbook.xml"))
    for sheet_el in wb.iter(NS + "sheet"):
        sp = rid_map.get(sheet_el.get(R_ID))
        if not sp or sp not in names:
            continue
        header_done = False
        with z.open(sp) as fh:
            for _, el in ET.iterparse(fh):
                if el.tag != NS + "row":
                    continue
                cells = []
                for cell in el.iter(NS + "c"):
                    ref = cell.get("r")
                    if ref:
                        idx = col_index(ref)
                        if idx > len(cells):
                            cells.extend([""] * (idx - len(cells)))
                    val = ""
                    v_el = cell.find(NS + "v")
                    if v_el is not None and v_el.text:
                        if cell.get("t") == "s":
                            i = int(v_el.text)
                            val = shared[i] if i < len(shared) else ""
                        else:
                            val = v_el.text
                    is_el = cell.find(NS + "is")
                    if is_el is not None:
                        val = text_of(is_el)
                    cells.append(val)
                el.clear()
                while cells and not cells[-1].strip():
                    cells.pop()
                if not cells:
                    continue
                if not header_done:
                    emit(f"## {sheet_el.get('name')}")
                emit("| " + " | ".join(cells) + " |")
                if not header_done:
                    emit("| " + " | ".join("---" for _ in cells) + " |")
                    header_done = True
        if header_done:
            emit("")
'''
    result = _run_with_memory_limit(
        [sys.executable, "-c", script, input_path,
         str(XLSX_FALLBACK_MAX_OUTPUT_MB * 1024 * 1024), str(_OUTPUT_LIMIT_EXIT_CODE)],
        timeout=timeout,
        converter_name="xlsx_xml_fallback",
    )
    if result.returncode == _OUTPUT_LIMIT_EXIT_CODE:
        raise MemoryLimitExceeded(
            f"xlsx_xml_fallback: converted output exceeded {XLSX_FALLBACK_MAX_OUTPUT_MB} MB limit"
        )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        clean_msg = _extract_exception_message(stderr)
        logger.error("[Converter] xlsx XML fallback stderr: %s", stderr)
        raise RuntimeError(f"XLSX conversion failed: {clean_msg}")
    return result.stdout.decode("utf-8", errors="replace")


def xlsx_to_markdown(input_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Convert an .xlsx file to Markdown using python-calamine in a subprocess.

    Calamine (Rust-based) is fast, tolerant of unusual styles/fills that
    trip up openpyxl, and already used for .xls files.
    Falls back to a stdlib XML parser if calamine cannot handle the file.
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
    try:
        result = _run_with_memory_limit(
            [sys.executable, "-c", script, input_path],
            timeout=timeout,
            converter_name="xlsx_to_markdown",
        )
    except MemoryLimitExceeded as e:
        logger.warning("[Converter] calamine out of memory for .xlsx, trying XML fallback: %s", e)
        return _xlsx_to_markdown_xml(input_path, timeout=timeout)
    if result.returncode == 0:
        return result.stdout.decode("utf-8", errors="replace")

    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    logger.warning("[Converter] calamine failed for .xlsx, trying XML fallback: %s",
                   _extract_exception_message(stderr))
    return _xlsx_to_markdown_xml(input_path, timeout=timeout)


# Strips layout-only markup from word/document.xml.  Pandoc's docx reader holds
# the whole XML tree in memory, and survey questionnaires carry run-level font,
# size, colour, language and rsid noise on every run; dropping it roughly halves
# Pandoc's memory with byte-identical Markdown on the samples tested.
_DOCX_SLIM_SCRIPT = r'''
import re, sys, zipfile

NOISE = re.compile(
    rb"<w:(?:sz|szCs|color|lang|bCs|iCs|kern|proofErr|noProof|lastRenderedPageBreak|shd|"
    rb"position|snapToGrid|webHidden|spacing|w|cs|vAlign|top|left|bottom|right|start|end|"
    rb"insideH|insideV|keepNext|keepLines|widowControl|autoSpaceDE|autoSpaceDN|"
    rb"adjustRightInd|textAlignment|contextualSpacing|suppressAutoHyphens|tblLayout|tblInd|"
    rb"emboss|imprint|outline|shadow|effect|fitText|eastAsianLayout)\b[^>]*/>"
)
RSID = re.compile(rb' (?:w:rsid\w*|w14:paraId|w14:textId)="[^"]*"')
RFONTS = re.compile(rb"<w:rFonts\b[^>]*/>")
# Pandoc maps Symbol/Wingdings code points to Unicode, so those font hints stay.
SYMBOL_FONT = re.compile(rb"Symbol|Wingdings|Webdings", re.I)
EMPTY_RPR = re.compile(rb"<w:rPr>\s*</w:rPr>")

src, dst = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(src) as zi, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
    for info in zi.infolist():
        data = zi.read(info.filename)
        if info.filename == "word/document.xml":
            data = RSID.sub(b"", data)
            data = NOISE.sub(b"", data)
            data = RFONTS.sub(lambda m: m[0] if SYMBOL_FONT.search(m[0]) else b"", data)
            data = EMPTY_RPR.sub(b"", data)
        zo.writestr(info, data)
'''


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _slim_docx(input_path: str, timeout: int) -> str | None:
    """Write a copy of *input_path* without layout-only markup; return its path, or None on failure."""
    slim_path = os.path.splitext(input_path)[0] + ".slim.docx"
    try:
        result = _run_with_memory_limit(
            [sys.executable, "-c", _DOCX_SLIM_SCRIPT, input_path, slim_path],
            timeout=timeout,
            converter_name="docx_slim",
        )
        if result.returncode == 0:
            return slim_path
        logger.warning("[Converter] docx slimming failed: %s",
                       _extract_exception_message(result.stderr.decode("utf-8", errors="replace")))
    except (MemoryLimitExceeded, subprocess.TimeoutExpired) as e:
        logger.warning("[Converter] docx slimming failed: %s", e)
    _remove_quietly(slim_path)
    return None


def _docx_to_markdown_text(input_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Last-resort .docx fallback: stream paragraphs, headings and table rows as text.

    Loses inline formatting, list numbering and merged-cell layout, but runs in
    near-constant memory for documents Pandoc and MarkItDown cannot fit.
    """
    script = r'''
import re, sys, zipfile, xml.etree.ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
HEADING = re.compile(r"^[Hh]eading\s?(\d)$")
out = sys.stdout.buffer


def para_text(p):
    parts = []
    for el in p.iter():
        if el.tag == W + "t":
            parts.append(el.text or "")
        elif el.tag == W + "tab":
            parts.append("\t")
        elif el.tag in (W + "br", W + "cr"):
            parts.append(" ")
    return "".join(parts).strip()


def heading_level(p):
    style = p.find(f"{W}pPr/{W}pStyle")
    m = HEADING.match(style.get(W + "val", "")) if style is not None else None
    return int(m[1]) if m else 0


with zipfile.ZipFile(sys.argv[1]) as z, z.open("word/document.xml") as fh:
    depth = 0
    row = None
    for ev, el in ET.iterparse(fh, events=("start", "end")):
        tag = el.tag
        if ev == "start":
            if tag == W + "tbl":
                depth += 1
                if depth == 1:
                    out.write(b"\n")
            elif tag == W + "tr" and depth == 1:
                row = []
            continue
        if tag == W + "tc" and depth == 1:
            row.append(" ".join(t for t in (para_text(p) for p in el.iter(W + "p")) if t))
            el.clear()
        elif tag == W + "tr" and depth == 1:
            if any(row):
                out.write(("| " + " | ".join(row) + " |\n").encode("utf-8"))
            row = None
            el.clear()
        elif tag == W + "tbl":
            depth -= 1
            if depth == 0:
                out.write(b"\n")
                el.clear()
        elif tag == W + "p" and depth == 0:
            text = para_text(el)
            if text:
                level = heading_level(el)
                out.write((("#" * level + " " if level else "") + text + "\n\n").encode("utf-8"))
            el.clear()
'''
    result = _run_with_memory_limit(
        [sys.executable, "-c", script, input_path],
        timeout=timeout,
        converter_name="docx_text_fallback",
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        logger.error("[Converter] docx text fallback stderr: %s", stderr)
        raise RuntimeError(f"DOCX conversion failed: {_extract_exception_message(stderr)}")
    return result.stdout.decode("utf-8", errors="replace")


def _convert_docx(input_path: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """Convert .docx: Pandoc -> MarkItDown -> plain-text extraction, each on memory/time failure.

    Pandoc and MarkItDown read a slimmed copy (see _DOCX_SLIM_SCRIPT).  Pandoc
    gets half the budget so the fallbacks still have time.
    """
    start = time.monotonic()

    def remaining() -> int:
        return timeout - int(time.monotonic() - start)

    slim_path = _slim_docx(input_path, timeout=timeout // 4)
    source = slim_path or input_path
    try:
        try:
            return pandoc_to_markdown(source, timeout=timeout // 2)
        except (MemoryLimitExceeded, subprocess.TimeoutExpired, RuntimeError) as e:
            if isinstance(e, RuntimeError) and "Heap exhausted" not in str(e):
                raise
            if remaining() < 10:
                raise
            logger.warning("[Converter] Pandoc failed for .docx (%s), falling back to MarkItDown", e)
        try:
            return markitdown_to_markdown(source, timeout=remaining())
        except (MemoryLimitExceeded, subprocess.TimeoutExpired, RuntimeError) as e:
            if remaining() < 10:
                raise
            logger.warning("[Converter] MarkItDown failed for .docx (%s), falling back to text extraction", e)
        return _docx_to_markdown_text(input_path, timeout=remaining())
    finally:
        if slim_path:
            _remove_quietly(slim_path)


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
    if ext == ".docx":
        logger.info("[Converter] Using Pandoc for %s", extension)
        return _convert_docx(input_path, timeout=timeout)

    converter = get_converter(ext)
    if converter == "pandoc":
        logger.info("[Converter] Using Pandoc for %s", extension)
        return pandoc_to_markdown(input_path, timeout=timeout)
    elif converter == "xlsx":
        logger.info("[Converter] Using calamine for %s", extension)
        return xlsx_to_markdown(input_path, timeout=timeout)
    elif converter == "xls":
        logger.info("[Converter] Using calamine for %s", extension)
        return xls_to_markdown(input_path, timeout=timeout)
    elif converter == "markitdown":
        logger.info("[Converter] Using MarkItDown for %s", extension)
        return markitdown_to_markdown(input_path, timeout=timeout)
    else:
        raise ValueError(f"Unsupported extension: {extension}")
