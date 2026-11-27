"""The only code that opens an untrusted file. Runs in the sandbox child, never elsewhere.

Each job takes the path of one file and returns a small dictionary of plain values: codes
from a fixed set and numbers. No text from the file, no path, no key: the parent validates
the result against the job's model (`runner.RESULTS`) and uses nothing else.

- `inspect` (build step 6, second half, part 1): is this a PDF that opens, how many pages
  has it, does it need a password? It moved here from the fingerprint Lambda, which has one
  user and its credentials in its environment, and so must never open a PDF.
- `selftest`: reports what the child can see and do, and can misbehave on request. The image
  tests and the isolation run in AWS use it to show the confinement holds where it runs.

Two jobs send more than a small result (build step 6, second half, stage 2). They write to
the **page channel**, a pipe the parent opened for them, one JSON record per line, and still
print only numbers on standard output. The child writes no file for either:

- `text`: for each page of a range, the text of its text layer, where each word is on the
  page, and a few numbers the parent scores the page with (how much of the page images cover,
  how many annotations draw something, how many objects it has).
- `render`: each listed page drawn as a greyscale PNG, as a person would see it (form fields
  and annotations included), for the parent to send to Textract. The PNG is written with
  `zlib` alone; there is no image library here.
- `pageprobe`: misbehaves on the page channel on request, for the same tests as `selftest`.

Everything in a record is untrusted when it reaches the parent, which checks it against a
strict model and its own caps (`runner`). The caps here only keep an honest child inside
them.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import signal
import socket
import sys
import time
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

PDF_MAGIC = b"%PDF-"
# The PDF header may follow a little leading junk; PDF readers accept up to 1 KB of it.
HEADER_WINDOW = 1024
# pypdfium2.raw.FPDF_ERR_PASSWORD: the document needs a password to open.
PDFIUM_PASSWORD_ERROR = 4

NOT_A_PDF = "not_a_pdf"
PDF_ENCRYPTED = "pdf_encrypted"
# Libraries the child must never have loaded: AWS clients and database drivers.
FORBIDDEN_MODULES = ("boto3", "botocore", "psycopg", "sqlalchemy", "assay_db", "assay_tss")


def inspect(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        head = handle.read(HEADER_WINDOW)
    if PDF_MAGIC not in head:
        return {"problem": NOT_A_PDF, "pages": None}
    import pypdfium2

    try:
        pdf = pypdfium2.PdfDocument(path)
    except pypdfium2.PdfiumError as exc:
        if getattr(exc, "err_code", None) == PDFIUM_PASSWORD_ERROR:
            return {"problem": PDF_ENCRYPTED, "pages": None}
        return {"problem": NOT_A_PDF, "pages": None}
    try:
        return {"problem": None, "pages": len(pdf)}
    finally:
        pdf.close()


# --- selftest -----------------------------------------------------------------------------


def _attempt(action: Any) -> str:
    """ "allowed", or the name of the error that stopped it."""
    try:
        action()
    except OSError as exc:
        return type(exc).__name__
    return "allowed"


def _read_one_byte(path: str) -> None:
    with Path(path).open("rb") as handle:
        handle.read(1)


def _open_socket() -> None:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()


def _read_parent_memory() -> None:
    """process_vm_readv on the parent: refused by the filter, and by the kernel's own rule
    that one user may not read another's process."""

    class IoVec(ctypes.Structure):
        _fields_ = (("base", ctypes.c_void_p), ("len", ctypes.c_size_t))

    libc = ctypes.CDLL(None, use_errno=True)
    buffer = ctypes.create_string_buffer(8)
    local = IoVec(ctypes.cast(buffer, ctypes.c_void_p), 8)
    remote = IoVec(ctypes.c_void_p(0x400000), 8)
    result = libc.process_vm_readv(os.getppid(), ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
    if result < 0:
        raise OSError(ctypes.get_errno(), "process_vm_readv refused")


def _status(field: str) -> int | None:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith(f"{field}:"):
            return int(line.split()[1])
    return None


def _report() -> dict[str, Any]:
    if sys.platform != "linux":
        raise OSError("the sandbox runs on Linux only")
    # Where a directory happens to be writable, the file-size limit (zero) stops the write.
    # Take that as an error to report, not as the signal that would end this process.
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    parent = os.getppid()
    return {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "groups": sorted(os.getgroups()),
        "environment": sorted(os.environ),
        "forbidden_modules": sorted(m for m in FORBIDDEN_MODULES if m in sys.modules),
        "socket": _attempt(_open_socket),
        "parent_environment": _attempt(lambda: _read_one_byte(f"/proc/{parent}/environ")),
        "parent_memory_file": _attempt(lambda: _read_one_byte(f"/proc/{parent}/mem")),
        "parent_memory_read": _attempt(_read_parent_memory),
        "exec": _attempt(lambda: os.execv("/bin/true", ["/bin/true"])),  # noqa: S606  # nosec B606
        "write_work": _attempt(lambda: Path("/work/sandbox-selftest").write_bytes(b"x")),
        "write_tmp": _attempt(lambda: Path("/tmp/sandbox-selftest").write_bytes(b"x")),  # noqa: S108  # nosec B108
        "no_new_privs": _status("NoNewPrivs"),
        "seccomp": _status("Seccomp"),
    }


def _spin() -> dict[str, Any]:
    """Uses the processor without end: only the CPU limit, or the wall clock, stops it."""
    while True:
        pass


def _spin_ignoring_the_warning() -> dict[str, Any]:
    """Ignores SIGXCPU and keeps going: the hard CPU limit kills it a moment later."""
    if sys.platform != "linux":
        raise OSError("the sandbox runs on Linux only")
    signal.signal(signal.SIGXCPU, signal.SIG_IGN)
    while True:
        pass


def _crash() -> dict[str, Any]:
    """Dies the way a parser does when it goes wrong: SIGABRT, not at any limit."""
    os.abort()


def _sleep() -> dict[str, Any]:
    """Waits without end and uses no processor time: only the parent's wall clock stops it."""
    while True:
        time.sleep(3600)


def _exhaust_memory() -> dict[str, Any]:
    hoard = []
    while True:
        hoard.append(bytearray(64 * 1024 * 1024))


def _flood() -> dict[str, Any]:
    chunk = "x" * 65536
    while True:
        sys.stdout.write(chunk)


def _fork_bomb() -> dict[str, Any]:
    if sys.platform != "linux":
        raise OSError("the sandbox runs on Linux only")
    children = 0
    try:
        while children < 1000:
            if os.fork() == 0:
                while True:
                    pass
            children += 1
    except OSError:
        pass
    return {"children": children}


SELFTESTS = {
    "report": _report,
    "spin": _spin,
    "stubborn": _spin_ignoring_the_warning,
    "crash": _crash,
    "sleep": _sleep,
    "memory": _exhaust_memory,
    "flood": _flood,
    "fork": _fork_bomb,
}


def selftest(path: Path) -> dict[str, Any]:
    """The probe's name is the file's name: `/dev/null/..` is never opened."""
    return SELFTESTS[path.name]()


JOBS = {"inspect": inspect, "selftest": selftest}


# --- The page channel: `text` and `render` ---------------------------------------------------

# One page's text, in UTF-8. A dense page of small print has under 20 KB.
MAX_PAGE_TEXT_BYTES = 256 * 1024
MAX_PAGE_CHARS = MAX_PAGE_TEXT_BYTES
# One page record, text and word positions together.
MAX_PAGE_RECORD_BYTES = 1024 * 1024
MAX_WORDS = 10_000
MAX_WORD_CHARS = 200
# Positions are fractions of the page, in ten-thousandths, measured from its top left corner
# as the page is displayed (its rotation applied): the form Textract's positions take.
UNITS = 10_000
# Objects and annotations looked at per page. A page with more is judged without counting.
MAX_OBJECTS = 20_000
MAX_ANNOTATIONS = 5_000
OBJECT_DEPTH = 4
# Image coverage is measured on a grid of this many cells a side.
COVERAGE_GRID = 64
# pypdfium2.raw: FPDF_PAGEOBJ_IMAGE.
PAGE_OBJECT_IMAGE = 3
# Annotations that draw content a person sees and the text layer does not hold: form fields
# (FPDF_ANNOT_WIDGET), free text, stamps and ink.
DRAWN_ANNOTATIONS = frozenset({20, 3, 13, 15})

# Rendering. Textract reads text 15 pixels high or more: 8-point type at 150 DPI.
RENDER_DPI = 200
MAX_RENDER_SIDE = 3300
# Textract's limit for one image is stated as 5 MB in one place and 10 MB in another; this
# stays under both.
MAX_IMAGE_BYTES = 5_000_000
# A second rendering, when the first PNG is too large, is sized so that even an image that
# does not compress at all fits.
MAX_RAW_PIXELS = 4_500_000
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

REPLACEMENT = "�"


def _emit(channel: BinaryIO, record: dict[str, Any]) -> None:
    """One record, one line. ASCII only: every other character is escaped."""
    channel.write(json.dumps(record, separators=(",", ":")).encode("ascii") + b"\n")
    channel.flush()


def _character(code: int) -> str | None:
    """The character PDFium reports at one position of the text layer, or None to leave it
    out. Nothing here judges the text; it only keeps it encodable."""
    if code in (0, 0x0D):
        return None  # nothing, and the carriage return of PDFium's own line ends
    if code == 0x02:
        return "-"  # PDFium's mark for a hyphen at a line break
    if 0xD800 <= code <= 0xDFFF or code in (0xFFFE, 0xFFFF) or code > 0x10FFFF:
        return REPLACEMENT
    return chr(code)


class _Device:
    """Page coordinates to positions on the displayed page, in `UNITS`."""

    def __init__(self, pdfium: Any, page: Any) -> None:
        self._pdfium = pdfium
        self._page = page.raw
        self._x = ctypes.c_int()
        self._y = ctypes.c_int()

    def point(self, x: float, y: float) -> tuple[int, int]:
        self._pdfium.FPDF_PageToDevice(
            self._page, 0, 0, UNITS, UNITS, 0, x, y, ctypes.byref(self._x), ctypes.byref(self._y)
        )
        return self._x.value, self._y.value

    def box(self, left: float, bottom: float, right: float, top: float) -> tuple[int, ...]:
        """(left, top, width, height) of a rectangle given in page coordinates, clamped to
        the page."""
        x1, y1 = self.point(left, bottom)
        x2, y2 = self.point(right, top)
        low_x, high_x = sorted((min(max(x1, 0), UNITS), min(max(x2, 0), UNITS)))
        low_y, high_y = sorted((min(max(y1, 0), UNITS), min(max(y2, 0), UNITS)))
        return low_x, low_y, high_x - low_x, high_y - low_y


def _text_and_words(pdfium: Any, textpage: Any, device: _Device) -> tuple[str, list[list[Any]]]:
    """The text layer's text, and each run of characters between spaces with its box."""
    count = pdfium.FPDFText_CountChars(textpage.raw)
    if count > MAX_PAGE_CHARS:
        raise OverflowError("more text than a page record holds")
    characters: list[str] = []
    words: list[list[Any]] = []
    word: list[str] = []
    bounds: list[float] = []

    def end_word() -> None:
        if word and len(words) < MAX_WORDS:
            words.append([*device.box(*bounds), "".join(word)[:MAX_WORD_CHARS]])
        word.clear()
        bounds.clear()

    for index in range(count):
        character = _character(pdfium.FPDFText_GetUnicode(textpage.raw, index))
        if character is None:
            continue
        characters.append(character)
        if character.isspace():
            end_word()
            continue
        word.append(character)
        left, bottom, right, top = textpage.get_charbox(index)
        if bounds:
            bounds[:] = (
                min(bounds[0], left),
                min(bounds[1], bottom),
                max(bounds[2], right),
                max(bounds[3], top),
            )
        else:
            bounds[:] = (left, bottom, right, top)
    end_word()
    return "".join(characters), words


def _objects_and_image_coverage(page: Any, device: _Device) -> tuple[int, float]:
    """How many objects the page has, and the share of the page its images cover (boxes on a
    grid, so overlapping images count once). A page with too many objects to look at is
    reported as covered: the parent then has it read from its picture."""
    cells = bytearray(COVERAGE_GRID * COVERAGE_GRID)
    count = 0
    for item in page.get_objects(max_depth=OBJECT_DEPTH):
        count += 1
        if count > MAX_OBJECTS:
            return MAX_OBJECTS, 1.0
        if item.type != PAGE_OBJECT_IMAGE:
            continue
        left, top, width, height = device.box(*item.get_bounds())
        first_column = left * COVERAGE_GRID // UNITS
        last_column = min((left + width) * COVERAGE_GRID // UNITS, COVERAGE_GRID - 1)
        first_row = top * COVERAGE_GRID // UNITS
        last_row = min((top + height) * COVERAGE_GRID // UNITS, COVERAGE_GRID - 1)
        if width <= 0 or height <= 0:
            continue
        for row in range(first_row, last_row + 1):
            start = row * COVERAGE_GRID
            cells[start + first_column : start + last_column + 1] = b"\x01" * (
                last_column - first_column + 1
            )
    return count, sum(cells) / len(cells)


def _drawn_annotations(pdfium: Any, page: Any) -> int:
    total = pdfium.FPDFPage_GetAnnotCount(page.raw)
    if total > MAX_ANNOTATIONS:
        return MAX_ANNOTATIONS
    drawn = 0
    for index in range(max(total, 0)):
        annotation = pdfium.FPDFPage_GetAnnot(page.raw, index)
        if not annotation:
            continue
        try:
            if pdfium.FPDFAnnot_GetSubtype(annotation) in DRAWN_ANNOTATIONS:
                drawn += 1
        finally:
            pdfium.FPDFPage_CloseAnnot(annotation)
    return drawn


def _page_record(pdfium: Any, page: Any, number: int) -> dict[str, Any]:
    width, height = page.get_size()
    device = _Device(pdfium, page)
    objects, coverage = _objects_and_image_coverage(page, device)
    record: dict[str, Any] = {
        "kind": "page",
        "page": number,
        "width": round(float(width), 2),
        "height": round(float(height), 2),
        "rotation": int(page.get_rotation()),
        "objects": objects,
        "image_coverage": round(coverage, 4),
        "annotations": _drawn_annotations(pdfium, page),
        "oversize": False,
        "text": "",
        "words": [],
    }
    textpage = page.get_textpage()
    try:
        text, words = _text_and_words(pdfium, textpage, device)
    except OverflowError:
        return record | {"oversize": True}
    finally:
        textpage.close()
    if len(text.encode("utf-8")) > MAX_PAGE_TEXT_BYTES:
        return record | {"oversize": True}
    record |= {"text": text, "words": words}
    if len(json.dumps(record, separators=(",", ":"))) > MAX_PAGE_RECORD_BYTES:
        # The text is what matters: it goes without its word positions.
        record["words"] = []
    if len(json.dumps(record, separators=(",", ":"))) > MAX_PAGE_RECORD_BYTES:
        return record | {"oversize": True, "text": ""}
    return record


def text(path: Path, channel: BinaryIO, first: str, last: str) -> dict[str, Any]:
    """Pages `first` to `last` (from 1): one page record each, in order."""
    import pypdfium2
    import pypdfium2.raw as pdfium

    start, end = int(first), int(last)
    pdf = pypdfium2.PdfDocument(path)
    try:
        if not 1 <= start <= end <= len(pdf):
            raise ValueError("not pages of this document")
        for number in range(start, end + 1):
            page = pdf[number - 1]
            try:
                _emit(channel, _page_record(pdfium, page, number))
            finally:
                page.close()
    finally:
        pdf.close()
    return {"pages": end - start + 1}


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return len(data).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")


def png(width: int, height: int, stride: int, pixels: Any) -> bytes:
    """An 8-bit greyscale PNG from rows of one byte a pixel. `zlib` is all it needs."""
    view = memoryview(pixels).cast("B")
    rows = bytearray()
    for row in range(height):
        rows += b"\x00"  # this row is not filtered
        rows += view[row * stride : row * stride + width]
    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes((8, 0, 0, 0, 0))
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 6))
        + _chunk(b"IEND", b"")
    )


def _draw(page: Any, scale: float) -> tuple[bytes, int, int]:
    bitmap = page.render(scale=scale, grayscale=True, may_draw_forms=True)
    try:
        if bitmap.n_channels != 1:
            raise ValueError("not a greyscale bitmap")
        return (
            png(bitmap.width, bitmap.height, bitmap.stride, bitmap.buffer),
            bitmap.width,
            bitmap.height,
        )
    finally:
        bitmap.close()


def render(path: Path, channel: BinaryIO, pages: str) -> dict[str, Any]:
    """Each listed page (from 1, separated by commas) as a PNG, in the order given."""
    import pypdfium2

    numbers = [int(number) for number in pages.split(",")]
    pdf = pypdfium2.PdfDocument(path)
    try:
        if not all(1 <= number <= len(pdf) for number in numbers):
            raise ValueError("not pages of this document")
        try:  # noqa: SIM105  (the child imports no `contextlib`: see its allow-list)
            # Form fields are drawn only once the document's form environment exists. A
            # document whose forms PDFium cannot set up is drawn without them.
            pdf.init_forms()
        except pypdfium2.PdfiumError:
            pass
        for number in numbers:
            page = pdf[number - 1]
            try:
                width, height = page.get_size()
                scale = min(RENDER_DPI / 72, MAX_RENDER_SIDE / max(width, height, 1))
                image, columns, rows = _draw(page, scale)
                if len(image) > MAX_IMAGE_BYTES:
                    scale *= (MAX_RAW_PIXELS / (columns * rows)) ** 0.5
                    image, columns, rows = _draw(page, scale)
                if len(image) > MAX_IMAGE_BYTES:
                    raise ValueError("a page's picture is too large")
            finally:
                page.close()
            _emit(
                channel,
                {
                    "kind": "image",
                    "page": number,
                    "width": columns,
                    "height": rows,
                    "dpi": max(round(scale * 72), 1),
                    "png": base64.b64encode(image).decode("ascii"),
                },
            )
    finally:
        pdf.close()
    return {"pages": len(numbers)}


# --- pageprobe ------------------------------------------------------------------------------


def _channel_flood(channel: BinaryIO) -> dict[str, Any]:
    """Writes to the page channel without end and never ends a record."""
    block = b"x" * 65536
    while True:
        channel.write(block)
        channel.flush()


def _channel_wrong_page(channel: BinaryIO) -> dict[str, Any]:
    """A well-formed record for a page nobody asked for."""
    record = {
        "kind": "page",
        "page": 999,
        "width": 612.0,
        "height": 792.0,
        "rotation": 0,
        "objects": 0,
        "image_coverage": 0.0,
        "annotations": 0,
        "oversize": False,
        "text": "",
        "words": [],
    }
    _emit(channel, record)
    return {"pages": 1}


def _channel_not_a_record(channel: BinaryIO) -> dict[str, Any]:
    channel.write(b'{"kind":"page","path":"/etc/passwd"}\n')
    channel.flush()
    return {"pages": 1}


def _blank_page(objects: int) -> dict[str, Any]:
    return {
        "kind": "page",
        "page": 1,
        "width": 612.0,
        "height": 792.0,
        "rotation": 0,
        "objects": objects,
        "image_coverage": 0.0,
        "annotations": 0,
        "oversize": False,
        "text": "",
        "words": [],
    }


def _channel_descriptors(channel: BinaryIO) -> dict[str, Any]:
    """How many descriptors the child has open, sent as a page record's `objects`: its
    standard input, output and error, and the page channel. Nothing else of the parent's."""
    # Listing the directory opens one more for as long as the listing takes.
    _emit(channel, _blank_page(len(list(Path("/proc/self/fd").iterdir())) - 1))
    return {"pages": 1}


def _channel_write_a_file(channel: BinaryIO) -> dict[str, Any]:
    """Tries to leave a page's picture in a file instead of sending it. A page job writes no
    file: the limit is still zero. Reports whether it could, as `objects` (1 if it could)."""
    if sys.platform != "linux":
        raise OSError("the sandbox runs on Linux only")
    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    wrote = 0
    for target in ("/work/page.png", "/tmp/page.png"):  # noqa: S108  # nosec B108
        if _attempt(lambda target=target: Path(target).write_bytes(PNG_SIGNATURE)) == "allowed":
            wrote = 1
    _emit(channel, _blank_page(wrote))
    return {"pages": 1}


PAGE_PROBES = {
    "flood": _channel_flood,
    "wrong_page": _channel_wrong_page,
    "not_a_record": _channel_not_a_record,
    "descriptors": _channel_descriptors,
    "write_a_file": _channel_write_a_file,
}


def pageprobe(path: Path, channel: BinaryIO, *_: str) -> dict[str, Any]:
    """Like `selftest`: the probe's name is the file's name, and no file is opened."""
    return PAGE_PROBES[path.name](channel)


# Jobs that are given the page channel: `job(path, channel, *arguments)`.
PAGE_JOBS: dict[str, Callable[..., dict[str, Any]]] = {
    "text": text,
    "render": render,
    "pageprobe": pageprobe,
}
