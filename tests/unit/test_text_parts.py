"""The parts of the text step that need no database (build step 6, second half, stage 2):

- the quality score: what sends a page to Textract;
- the page channel: everything a sandbox child sends down it is untrusted, and the parent
  holds one record at a time;
- the picture a page is drawn as: a real PNG, written with `zlib` alone;
- the Textract client: which errors are tried again, for how long, and what is kept of one.
"""

from __future__ import annotations

import base64
import io
import json
import zlib
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError

from assay_workers.sandbox import child, jobs, runner
from assay_workers.sandbox.confine import Limits
from assay_workers.sandbox.runner import Channel, Completed
from assay_workers.worker import ocr, quality
from assay_workers.worker.__main__ import text_settings
from assay_workers.worker.context import TextSettings
from tests.unit.stage2 import in_process, test_files

make = test_files()
SOURCE = Path("/work/job/document.pdf")
ENGLISH = (
    "This agreement is made between the borrower and the lender on the date written below. "
    "The borrower promises to repay the principal amount together with interest at the annual "
    "rate stated in this note, in monthly payments, until the whole balance has been repaid. "
) * 2
THRESHOLD = 0.90


# --- The quality score -----------------------------------------------------------------------


def page(text: str, coverage: float = 0.0, annotations: int = 0, objects: int = 5) -> quality.Score:
    return quality.score(text, image_coverage=coverage, annotations=annotations, objects=objects)


def test_a_typed_page_passes_and_a_small_logo_does_not_change_that() -> None:
    assert page(ENGLISH).quality == 1.0 and not page(ENGLISH).fails(THRESHOLD)
    logo = page(ENGLISH, coverage=0.03)
    assert logo.quality == 0.97 and not logo.fails(THRESHOLD)


def test_a_scanned_page_fails_with_or_without_a_text_layer() -> None:
    # No text layer at all.
    bare = page("", coverage=1.0, objects=1)
    assert bare.quality == 0.0 and bare.fails(THRESHOLD) and not bare.layer_usable(THRESHOLD)
    # A typed line stamped on it, or a hidden text layer under it: the layer reads well, and
    # the page still goes to Textract, so its text is what a person sees.
    stamped = page("Envelope ID: 0123456789", coverage=1.0, objects=2)
    assert stamped.quality == 0.0 and stamped.fails(THRESHOLD) and stamped.layer_usable(THRESHOLD)


@pytest.mark.parametrize(
    ("coverage", "to_textract"), [(0.05, False), (0.10, False), (0.11, True), (0.4, True)]
)
def test_a_mixed_page_goes_to_textract_once_images_cover_more_than_a_tenth(
    coverage: float, to_textract: bool
) -> None:
    result = page(ENGLISH, coverage=coverage)
    assert result.fails(THRESHOLD) is to_textract
    assert result.layer_usable(THRESHOLD)  # its own text is fine either way


def test_a_page_with_form_fields_or_stamps_fails_whatever_its_text() -> None:
    result = page(ENGLISH, annotations=1)
    assert result.quality == 0.0 and result.fails(THRESHOLD) and result.layer_usable(THRESHOLD)


def test_a_blank_page_passes_and_a_page_with_content_and_no_text_fails() -> None:
    blank = page("", objects=0)
    assert blank.blank and blank.quality == 1.0 and not blank.fails(THRESHOLD)
    assert not blank.layer_usable(THRESHOLD)  # there is no text to keep
    # Lines or outlined letters, and nothing in the text layer.
    drawn = page("   \n ", objects=40)
    assert not drawn.blank and drawn.quality == 0.0 and drawn.fails(THRESHOLD)


def test_text_from_a_font_with_no_usable_character_map_fails() -> None:
    garbled = "".join(chr(0xE000 + index % 60) for index in range(300)) + " �� \x01\x02"
    result = page(garbled)
    assert result.legible < 0.05 and result.fails(THRESHOLD)
    assert not result.layer_usable(THRESHOLD)
    # A few odd characters in good text do not fail it.
    assert not page(ENGLISH + "�" * 5).fails(THRESHOLD)


def test_letters_that_are_not_language_fail_and_capitals_and_numbers_are_left_alone() -> None:
    # Real letters, shifted by a font's wrong map: words with no vowel in them.
    shifted = " ".join(["Thrgmnt", "brrwr", "lndr", "prncpl", "mnthly", "pymnts"] * 8)
    assert page(shifted).fails(THRESHOLD)
    # Abbreviations in capitals and a page of figures are not judged by their vowels.
    capitals = " ".join(["LLC", "APR", "SSN", "DTI", "HMDA", "TRID"] * 10)
    assert not page(capitals).fails(THRESHOLD)
    figures = " ".join(f"{n},{n:03d}.00" for n in range(100, 180))
    assert not page(figures).fails(THRESHOLD)
    # Too few words to judge: a short page passes.
    assert not page("Sgnd by Mr Flynn").fails(THRESHOLD)
    assert quality.MIN_WORDS_FOR_VOWELS == 30


def test_the_threshold_is_the_only_thing_that_moves_the_line() -> None:
    result = page(ENGLISH, coverage=0.2)
    assert result.quality == 0.8
    assert result.fails(0.90) and not result.fails(0.75)
    assert not result.fails(0.80)  # below, not at


# --- The page channel: the parent holds one record at a time -------------------------------------


def test_the_channel_gives_whole_lines_one_at_a_time_however_the_bytes_arrive() -> None:
    seen: list[bytes] = []
    channel = Channel(seen.append, max_line=100, max_total=10_000)
    for piece in (b"one\ntw", b"o\n", b"", b"three\n\nfive", b"\n"):
        channel.feed(piece)
    channel.finish()
    assert seen == [b"one", b"two", b"three", b"", b"five"]
    assert channel.total == 20


def test_the_channel_never_holds_more_than_one_record() -> None:
    """Each line is handed over before the next byte is looked at: at the hand-over the
    channel holds nothing but what followed that line in the same read."""
    held: list[int] = []
    channel: Channel

    def handle(line: bytes) -> None:
        held.append(len(channel._pending))

    channel = Channel(handle, max_line=2000, max_total=10_000_000)
    record = b"x" * 1000 + b"\n"
    for _ in range(500):
        channel.feed(record)
    assert held == [0] * 500


@pytest.mark.parametrize(
    "pieces",
    [
        [b"x" * 101],  # one line longer than a record may be, never ended
        [b"x" * 60, b"x" * 60],
        [b"x" * 101 + b"\n"],
    ],
)
def test_a_line_longer_than_a_record_is_an_overflow(pieces: list[bytes]) -> None:
    channel = Channel(lambda _line: None, max_line=100, max_total=10_000)
    with pytest.raises(runner.ChannelOverflow):
        for piece in pieces:
            channel.feed(piece)


def test_more_in_total_than_one_child_may_send_is_an_overflow() -> None:
    channel = Channel(lambda _line: None, max_line=100, max_total=250)
    channel.feed(b"x" * 99 + b"\n")
    channel.feed(b"x" * 99 + b"\n")
    with pytest.raises(runner.ChannelOverflow):
        channel.feed(b"x" * 99 + b"\n")


def test_a_line_the_child_never_finished_is_not_a_record() -> None:
    channel = Channel(lambda _line: None, max_line=100, max_total=10_000)
    channel.feed(b"complete\nhalf")
    with pytest.raises(runner.RecordRefused):
        channel.finish()


# --- The page channel: every record is untrusted --------------------------------------------------


def good_page(number: int = 1, **changes: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": "page",
        "page": number,
        "width": 612.0,
        "height": 792.0,
        "rotation": 0,
        "objects": 3,
        "image_coverage": 0.0,
        "annotations": 0,
        "oversize": False,
        "text": "Some text",
        "words": [[100, 100, 500, 120, "Some"], [700, 100, 400, 120, "text"]],
    }
    return record | changes


def sending(records: Sequence[Any], result: Any = None, returncode: int = 0) -> runner.ChannelSpawn:
    """A child that writes these to the page channel and prints `result`."""

    def spawn(
        command: Callable[[int], Sequence[str]], limits: Limits, channel: Channel
    ) -> Completed:
        refused = overflow = False
        try:
            for record in records:
                line = record if isinstance(record, bytes) else json.dumps(record).encode()
                channel.feed(line + b"\n")
            channel.finish()
        except runner.RecordRefused:
            refused = True
        except runner.ChannelOverflow:
            overflow = True
        printed = {"pages": len(records)} if result is None else result
        return Completed(
            returncode=-9 if (refused or overflow) else returncode,
            stdout=json.dumps(printed).encode(),
            refused=refused,
            overflow=overflow,
        )

    return spawn


def read(spawn: runner.ChannelSpawn, first: int = 1, last: int = 2) -> list[runner.PageRecord]:
    got: list[runner.PageRecord] = []
    runner.read_text(SOURCE, first, last, got.append, spawn=spawn)
    return got


def test_good_records_reach_the_handler_in_order() -> None:
    got = read(sending([good_page(1), good_page(2, text="", words=[])]))
    assert [record.page for record in got] == [1, 2]
    assert got[0].words[0] == (100, 100, 500, 120, "Some")


@pytest.mark.parametrize(
    "records",
    [
        # Not the pages that were asked for, or not in order, or one twice, or too many.
        [good_page(2), good_page(1)],
        [good_page(1), good_page(1)],
        [good_page(1), good_page(3)],
        [good_page(1), good_page(2), good_page(3)],
        [good_page(7)],
        # Not the shape: an extra key (a path), a missing one, the wrong type.
        [good_page(1, path="/etc/passwd"), good_page(2)],
        [{"kind": "page", "page": 1}, good_page(2)],
        [good_page(1, page="1"), good_page(2)],
        [good_page(1, kind="image"), good_page(2)],
        [good_page(1, rotation=45), good_page(2)],
        [good_page(1, image_coverage=1.5), good_page(2)],
        [good_page(1, annotations=-1), good_page(2)],
        [good_page(1, words=[[1, 2, 3, "a"]]), good_page(2)],
        [good_page(1, words=[[1, 2, 3, 10_001, "a"]]), good_page(2)],
        [good_page(1, words=[[1, 2, 3, 4, "w" * 201]]), good_page(2)],
        [good_page(1, words=[[0, 0, 1, 1, "w"]] * 10_001), good_page(2)],
        # Not JSON, or not an object.
        [b"not json", good_page(2)],
        [b"[1, 2, 3]", good_page(2)],
        [b'"text"', good_page(2)],
        # Text that cannot be stored: a NUL, a lone surrogate.
        [good_page(1, text="a\x00b"), good_page(2)],
        [
            b'{"kind":"page","page":1,"width":612.0,"height":792.0,"rotation":0,"objects":3,'
            b'"image_coverage":0.0,"annotations":0,"oversize":false,"text":"\\ud800","words":[]}',
            good_page(2),
        ],
        # "Oversize" means it was sent without its text.
        [good_page(1, oversize=True), good_page(2)],
        # More text than a page may have (the model's own cap is in characters).
        [good_page(1, text="x" * (256 * 1024 + 1), words=[]), good_page(2)],
        [good_page(1, text="é" * (128 * 1024 + 1), words=[]), good_page(2)],
    ],
)
def test_anything_else_is_refused_and_nothing_after_it_is_handled(records: list[Any]) -> None:
    handled: list[int] = []
    with pytest.raises(runner.SandboxBadOutput):
        runner.read_text(SOURCE, 1, 2, lambda r: handled.append(r.page), spawn=sending(records))
    # Whatever was handled came before the refusal, in order: never the bad record itself.
    assert handled in ([], [1], [1, 2])


def test_fewer_pages_than_asked_for_is_refused_even_if_the_child_ended_well() -> None:
    with pytest.raises(runner.SandboxBadOutput):
        read(sending([good_page(1)]))
    # And a count that does not match what arrived.
    with pytest.raises(runner.SandboxBadOutput):
        read(sending([good_page(1), good_page(2)], result={"pages": 5}))
    with pytest.raises(runner.SandboxBadOutput):
        read(sending([good_page(1), good_page(2)], result={"pages": 2, "note": "hello"}))


def test_a_record_larger_than_a_megabyte_is_the_output_limit() -> None:
    huge = good_page(1, text="", words=[])
    line = (
        json.dumps(huge)[:-1].encode() + b',"pad":"' + b"x" * runner.MAX_PAGE_RECORD_BYTES + b'"}'
    )
    with pytest.raises(runner.SandboxLimit) as stopped:
        read(sending([line, good_page(2)]))
    assert stopped.value.reason == "output_limit"


def test_a_child_that_ended_badly_is_named_before_its_records_are_judged() -> None:
    with pytest.raises(runner.SandboxKilled) as killed:
        read(sending([good_page(1), good_page(2)], returncode=-11))
    assert killed.value.reason == "signal_SIGSEGV"


def png_record(number: int, data: bytes, **changes: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": "image",
        "page": number,
        "width": 1700,
        "height": 2200,
        "dpi": 200,
        "png": base64.b64encode(data).decode(),
    }
    return record | changes


PNG = runner.PNG_SIGNATURE + b"the rest is never looked at"


def test_a_picture_is_checked_for_its_first_bytes_and_its_length_and_never_opened() -> None:
    got: list[runner.PageImage] = []
    runner.render(
        SOURCE, [4, 9], got.append, spawn=sending([png_record(4, PNG), png_record(9, PNG)])
    )
    assert [(image.page, image.data) for image in got] == [(4, PNG), (9, PNG)]
    assert "the rest" not in repr(got[0])  # the bytes are not in its repr


@pytest.mark.parametrize(
    "record",
    [
        png_record(4, b"GIF89a not a png"),
        png_record(4, b"%PDF-1.7"),
        png_record(4, PNG + b"x" * runner.MAX_IMAGE_BYTES),
        png_record(4, PNG, png="not base64 !!"),
        png_record(4, PNG, png=""),
        png_record(4, PNG, width=0),
        png_record(4, PNG, height=10_001),
        png_record(4, PNG, dpi=0),
        png_record(4, PNG, path="/work/x.png"),
        png_record(5, PNG),
        good_page(4),
    ],
)
def test_a_picture_that_is_not_a_png_within_its_cap_is_refused(record: dict[str, Any]) -> None:
    with pytest.raises(runner.SandboxBadOutput):
        runner.render(SOURCE, [4], lambda _image: None, spawn=sending([record]))


def test_the_page_jobs_are_started_with_their_pages_and_the_channels_descriptor() -> None:
    seen: list[list[str]] = []

    def spawn(
        command: Callable[[int], Sequence[str]], limits: Limits, channel: Channel
    ) -> Completed:
        seen.append(list(command(7)))
        return sending([good_page(26), good_page(27)])(command, limits, channel)

    runner.read_text(SOURCE, 26, 27, lambda _r: None, spawn=spawn)
    assert seen[0][1:5] == ["-I", "-B", "-m", "assay_workers.sandbox.child"]
    assert seen[0][5:] == ["text", str(SOURCE), "7", "26", "27"]

    def draw(
        command: Callable[[int], Sequence[str]], limits: Limits, channel: Channel
    ) -> Completed:
        seen.append(list(command(9)))
        assert limits == runner.RENDER_LIMITS
        return sending([png_record(3, PNG), png_record(8, PNG)])(command, limits, channel)

    runner.render(SOURCE, [3, 8], lambda _i: None, spawn=draw)
    assert seen[1][5:] == ["render", str(SOURCE), "9", "3,8"]


def test_the_page_jobs_print_numbers_only_and_their_records_are_not_results() -> None:
    """Standard output keeps its rule: `RESULTS` holds no free text. Text travels only in a
    page record, which is not a result."""
    for job in ("text", "render", "pageprobe"):
        assert runner.RESULTS[job] is runner.PagesResult
    assert set(runner.PagesResult.model_fields) == {"pages"}
    assert runner.PageRecord not in runner.RESULTS.values()
    assert runner.ImageRecord not in runner.RESULTS.values()
    for model in (runner.PageRecord, runner.ImageRecord):
        assert model.model_config.get("extra") == "forbid"
        assert model.model_config.get("strict") is True
    assert runner.MAX_OUTPUT_BYTES == 64 * 1024  # unchanged


def test_the_child_refuses_a_page_job_without_a_channel() -> None:
    for argv in (
        ["child", "text", "/x.pdf"],
        ["child", "text", "/x.pdf", "abc", "1", "2"],
        # Standard input, output and error are never the channel.
        ["child", "render", "/x.pdf", "0", "3"],
        ["child", "render", "/x.pdf", "1", "3"],
        ["child", "text", "/x.pdf", "2", "1", "2"],
    ):
        assert child._page_job(argv) is None, argv
    assert set(jobs.PAGE_JOBS) == {"text", "render", "pageprobe"}
    assert not set(jobs.PAGE_JOBS) & set(jobs.JOBS)


# --- The real jobs, on the synthetic files -------------------------------------------------------


@pytest.fixture(scope="module")
def files(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    folder = tmp_path_factory.mktemp("files")
    written = {}
    for name, body in make.files(None).items():
        if name.endswith(".pdf"):
            (folder / name).write_bytes(body)
            written[name] = folder / name
    return written


def facts(path: Path, last: int) -> list[runner.PageRecord]:
    got: list[runner.PageRecord] = []
    runner.read_text(path, 1, last, got.append, spawn=in_process)
    return got


def test_the_text_job_reports_each_kind_of_page_as_it_is(files: dict[str, Path]) -> None:
    typed = facts(files["01-normal.pdf"], 3)
    assert all(
        r.image_coverage == 0 and r.annotations == 0 and "SYNTHETIC" in r.text for r in typed
    )
    scanned = facts(files["08-scanned.pdf"], 3)
    assert all(r.image_coverage == 1.0 and r.text == "" and r.words == [] for r in scanned)
    first, second, third = facts(files["09-mixed.pdf"], 3)
    assert first.image_coverage == 0 and 0.40 <= second.image_coverage <= 0.45
    assert third.image_coverage == 1.0 and third.text.startswith("Envelope ID:")
    [form] = facts(files["10-filled-form.pdf"], 1)
    assert form.annotations == 3 and "Joy Testperson" not in form.text


def test_word_positions_are_fractions_of_the_page_from_its_top_left(files: dict[str, Path]) -> None:
    [first, *_] = facts(files["01-normal.pdf"], 3)
    left, top, width, height, word = first.words[0]
    assert word == "SYNTHETIC"
    # The first line starts 72 points in and 72 points down an 8.5 by 11 inch page.
    assert 1100 < left < 1250 and 700 < top < 900
    assert 900 < width < 1300 and 80 < height < 160
    # Reading order: each line's words go left to right.
    assert first.words[1][0] > left


def unpack(png: bytes) -> tuple[int, int, int, int, bytes]:
    """Read a PNG back with the standard library: its size, depth, colour type and rows."""
    assert png.startswith(runner.PNG_SIGNATURE)
    at, chunks = 8, []
    while at < len(png):
        length = int.from_bytes(png[at : at + 4], "big")
        kind, data = png[at + 4 : at + 8], png[at + 8 : at + 8 + length]
        assert int.from_bytes(png[at + 8 + length : at + 12 + length], "big") == zlib.crc32(
            kind + data
        )
        chunks.append((kind, data))
        at += 12 + length
    assert [kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    header = chunks[0][1]
    width, height = int.from_bytes(header[:4], "big"), int.from_bytes(header[4:8], "big")
    return width, height, header[8], header[9], zlib.decompress(chunks[1][1])


def test_a_page_is_drawn_as_a_real_greyscale_png_with_what_a_person_sees(
    files: dict[str, Path],
) -> None:
    got: list[runner.PageImage] = []
    runner.render(files["10-filled-form.pdf"], [1], got.append, spawn=in_process)
    [image] = got
    assert (image.width, image.height, image.dpi) == (1700, 2200, 200)
    width, height, depth, colour, rows = unpack(image.data)
    assert (width, height, depth, colour) == (1700, 2200, 8, 0)  # 8-bit grey
    assert len(rows) == height * (width + 1)
    # White paper and black ink, and ink where the form's answers are drawn (the fields are
    # to the right of their questions: from 200 points across, about 555 pixels).
    assert rows.count(0xFF) > len(rows) * 0.9 and min(rows) < 40
    answer_row = next(
        y for y in range(380, 440) if min(rows[y * (width + 1) + 556 : y * (width + 1) + 800]) < 80
    )
    assert answer_row


def test_a_picture_too_large_is_drawn_again_smaller(
    monkeypatch: pytest.MonkeyPatch, files: dict[str, Path]
) -> None:
    monkeypatch.setattr(jobs, "MAX_IMAGE_BYTES", 12_000)
    monkeypatch.setattr(jobs, "MAX_RAW_PIXELS", 60_000)
    sent = io.BytesIO()
    jobs.render(files["01-normal.pdf"], sent, "1")
    record = json.loads(sent.getvalue())
    assert record["width"] * record["height"] <= 61_000 and record["dpi"] < 200
    assert len(base64.b64decode(record["png"])) <= 12_000


def test_a_page_with_more_text_than_a_record_holds_is_sent_without_it(
    monkeypatch: pytest.MonkeyPatch, files: dict[str, Path]
) -> None:
    monkeypatch.setattr(jobs, "MAX_PAGE_CHARS", 50)
    sent = io.BytesIO()
    jobs.text(files["01-normal.pdf"], sent, "1", "1")
    record = runner._checked_text(runner.PageRecord.model_validate_json(sent.getvalue()))
    assert record.oversize and record.text == "" and record.words == []
    # The parent then treats it as a page with content and no usable text: Textract.
    result = quality.score("", image_coverage=0.0, annotations=0, objects=max(record.objects, 1))
    assert result.fails(THRESHOLD)


def test_the_page_jobs_refuse_pages_the_document_does_not_have(files: dict[str, Path]) -> None:
    source = files["01-normal.pdf"]
    for first, last in (("3", "4"), ("0", "1"), ("2", "1")):
        with pytest.raises(ValueError, match="not pages of this document"):
            jobs.text(source, io.BytesIO(), first, last)
    with pytest.raises(ValueError, match="not pages of this document"):
        jobs.render(source, io.BytesIO(), "1,9")


def test_the_1000_page_file_is_behind_its_flag_and_has_1000_scanned_pages() -> None:
    assert make.LARGE_NAME not in make.files(None)
    assert (make.LARGE_NAME, make.LARGE_PAGES) == ("11-scanned-1000-pages.pdf", 1000)
    body = make.scanned("Package 011", "Max Testperson", "TEST-011", pages=12)
    assert body.count(b"/Type /Page /Parent") == 12 and body.count(b"/Subtype /Image") == 12


# --- Textract -------------------------------------------------------------------------------------


def error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "a message that may quote things"}},
        "DetectDocumentText",
    )


RESPONSE = {
    "DetectDocumentTextModelVersion": "1.0",
    "ResponseMetadata": {"RequestId": "abc-123"},
    "Blocks": [
        {"BlockType": "PAGE"},
        {"BlockType": "LINE", "Text": "Borrower: Jane", "Confidence": 99.1},
        {"BlockType": "LINE", "Text": "Amount: $1", "Confidence": 97.0},
        {
            "BlockType": "WORD",
            "Text": "Borrower:",
            "Confidence": 99.5,
            "Geometry": {"BoundingBox": {"Left": 0.1, "Top": 0.2, "Width": 0.15, "Height": 0.02}},
        },
        {
            "BlockType": "WORD",
            "Text": "Jane",
            "Confidence": 90.5,
            "Geometry": {"BoundingBox": {"Left": 0.3, "Top": 0.2, "Width": 0.05, "Height": 0.02}},
        },
    ],
}


class Client:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def detect_document_text(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def reader(client: Client, clock: Clock | None = None) -> ocr.TextractOcr:
    clock = clock or Clock()
    return ocr.TextractOcr(client, sleep=clock.sleep, clock=clock, jitter=lambda: 1.0)


def test_one_operation_with_the_pictures_bytes_and_never_a_bucket() -> None:
    client = Client(RESPONSE)
    result = reader(client).read(PNG)
    assert client.calls == [{"Document": {"Bytes": PNG}}]
    assert result.text == "Borrower: Jane\nAmount: $1"
    assert result.words == (
        (1000, 2000, 1500, 200, "Borrower:", 0.995),
        (3000, 2000, 500, 200, "Jane", 0.905),
    )
    assert result.confidence == 0.95
    assert (result.model_version, result.request_id) == ("1.0", "abc-123")
    assert (ocr.OPERATION, ocr.SERVICE) == ("DetectDocumentText", "textract")
    source = Path(ocr.__file__).read_text(encoding="utf-8")
    assert "S3Object" not in source and "start_document" not in source and "analyze_" not in source


def test_a_response_is_untrusted_too() -> None:
    odd = {
        "Blocks": [
            {"BlockType": "LINE", "Text": None},
            {"BlockType": "LINE", "Text": "a\x00b"},
            {"BlockType": "WORD", "Text": "w" * 500, "Confidence": "high", "Geometry": None},
            {
                "BlockType": "WORD",
                "Text": "x",
                "Confidence": 250,
                "Geometry": {
                    "BoundingBox": {"Left": -4, "Top": 7, "Width": float("nan"), "Height": "tall"}
                },
            },
            {"BlockType": "LINE", "Text": "kept"},
        ],
        "DetectDocumentTextModelVersion": 12,
        "ResponseMetadata": {"RequestId": "r" * 500},
    }
    result = ocr.parse(odd)
    assert result.text == "kept"
    assert result.words == ((0, 0, 0, 0, "w" * 200, 0.0), (0, 10000, 0, 0, "x", 0.0))
    assert result.confidence is None and result.model_version is None
    assert result.request_id is not None and len(result.request_id) == 128
    assert ocr.parse({}).text == "" and ocr.parse({"Blocks": None}).words == ()


@pytest.mark.parametrize(
    "code", ["ThrottlingException", "ProvisionedThroughputExceededException", "InternalServerError"]
)
def test_a_busy_textract_is_tried_again_with_growing_waits(code: str) -> None:
    clock = Clock()
    client = Client(error(code), error(code), error(code), RESPONSE)
    assert reader(client, clock).read(PNG).text.startswith("Borrower")
    assert len(client.calls) == 4
    assert clock.slept == [0.5, 1.0, 2.0]


def test_a_call_with_no_answer_is_tried_again_too() -> None:
    clock = Clock()
    client = Client(ConnectTimeoutError(endpoint_url="https://textract"), RESPONSE)
    assert reader(client, clock).read(PNG).text.startswith("Borrower")
    assert clock.slept == [0.5]


def test_after_five_minutes_the_step_fails_with_the_errors_own_name() -> None:
    clock = Clock()
    client = Client(*[error("ThrottlingException")] * 100)
    with pytest.raises(ocr.OcrFailed) as failed:
        reader(client, clock).read(PNG)
    assert failed.value.error_type == "ThrottlingException"
    assert str(failed.value) == "ThrottlingException"  # a name, never Textract's message
    assert 280 <= sum(clock.slept) <= ocr.RETRY_SECONDS == 300.0
    assert max(clock.slept) == ocr.MAX_DELAY_SECONDS == 20.0

    clock = Clock()
    silent = Client(*[ConnectTimeoutError(endpoint_url="https://textract")] * 100)
    with pytest.raises(ocr.OcrFailed) as unanswered:
        reader(silent, clock).read(PNG)
    assert unanswered.value.error_type == "ConnectTimeoutError"


@pytest.mark.parametrize(
    "code",
    [
        "UnsupportedDocumentException",
        "BadDocumentException",
        "DocumentTooLargeException",
        "InvalidParameterException",
    ],
)
def test_a_picture_textract_will_not_take_is_that_pages_problem_and_is_not_tried_again(
    code: str,
) -> None:
    clock = Clock()
    client = Client(error(code), RESPONSE)
    with pytest.raises(ocr.PageRefused) as refused:
        reader(client, clock).read(PNG)
    assert refused.value.code == code and len(client.calls) == 1 and clock.slept == []


@pytest.mark.parametrize(
    "code", ["AccessDeniedException", "InvalidS3ObjectException", "SomethingNew"]
)
def test_any_other_refusal_fails_the_step_at_once_by_name(code: str) -> None:
    clock = Clock()
    client = Client(error(code), RESPONSE)
    with pytest.raises(ocr.OcrFailed) as failed:
        reader(client, clock).read(PNG)
    assert failed.value.error_type == code and len(client.calls) == 1 and clock.slept == []


def test_botocore_retries_nothing_itself_so_the_five_minutes_are_the_limit() -> None:
    config = ocr.CLIENT_CONFIG
    assert getattr(config, "retries") == {"max_attempts": 1, "mode": "standard"}  # noqa: B009
    assert (getattr(config, "connect_timeout"), getattr(config, "read_timeout")) == (5, 30)  # noqa: B009


# --- Settings -------------------------------------------------------------------------------------


def test_the_text_steps_settings_come_from_the_task_and_have_no_default() -> None:
    env = {
        "TEXT_QUALITY_THRESHOLD": "0.90",
        "TEXT_MAX_PAGES": "1000",
        "TEXTRACT_PRICE_PER_PAGE": "0.0015",
    }
    assert text_settings(env) == TextSettings(
        threshold=0.9, max_pages=1000, price_per_page=Decimal("0.0015")
    )
    for missing in env:
        with pytest.raises(KeyError):
            text_settings({k: v for k, v in env.items() if k != missing})
    for key, bad in (
        ("TEXT_QUALITY_THRESHOLD", "0"),
        ("TEXT_QUALITY_THRESHOLD", "1.5"),
        ("TEXT_QUALITY_THRESHOLD", "high"),
        ("TEXT_MAX_PAGES", "0"),
        ("TEXT_MAX_PAGES", "many"),
        ("TEXTRACT_PRICE_PER_PAGE", "-1"),
        ("TEXTRACT_PRICE_PER_PAGE", ""),
        ("TEXTRACT_PRICE_PER_PAGE", "NaN"),
    ):
        with pytest.raises((ValueError, ArithmeticError)):
            text_settings(env | {key: bad})


def test_no_price_threshold_or_page_limit_is_written_in_the_workers_code() -> None:
    """They come from stack config. The price in particular appears in no worker module."""
    workers = Path(runner.__file__).resolve().parents[1]
    for path in workers.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "0.0015" not in source, path.name
        assert "TEXTRACT_PRICE_PER_PAGE" not in source or path.name == "__main__.py", path.name
