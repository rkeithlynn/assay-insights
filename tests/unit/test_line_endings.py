"""Files that `.gitattributes` marks as LF have LF line endings, on disk and in Git.

Owner, 2026-11-27, after it happened twice: files saved with CRLF on the Windows development
machine. Git shows no difference for them, but a container image is built from the bytes on
disk and tagged with their hash. Three things now stand in the way:

- this test, so `uv run pytest` fails where the problem arises (CI runs the same script, but
  a Linux checkout has no CRLF, so there it can only catch CRLF that was committed);
- `.editorconfig`, for editors, kept in step with `.gitattributes` below;
- `assay_infra.images.source_tag`, which refuses to tag an image whose sources have CRLF, so
  `pulumi preview` and `pulumi up` stop before anything is built.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from assay_infra import images

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_line_endings.py"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_line_endings", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_line_endings"] = module
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> None:
    exe = shutil.which("git")
    assert exe is not None
    subprocess.run(
        [exe, "-c", "user.name=t", "-c", "user.email=t@example.test", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


# --- The repository itself ------------------------------------------------------------------


def test_every_file_marked_lf_has_lf_line_endings(script: ModuleType) -> None:
    """Fix a failure with: uv run scripts/check_line_endings.py --fix"""
    found = script.entries()
    marked = [e for e in found if e.marked_lf]
    assert len(marked) > 200  # the attributes were read: most of the repository is marked
    on_disk = sorted(e.path for e in found if e.wrong_on_disk)
    assert not on_disk, (
        "CRLF line endings on disk in files .gitattributes marks as LF; fix them with "
        f"`uv run scripts/check_line_endings.py --fix`: {on_disk}"
    )
    assert not sorted(e.path for e in found if e.wrong_in_git)


def test_everything_that_goes_into_an_image_is_marked_lf(script: ModuleType) -> None:
    """An image's tag is the hash of its sources' bytes on disk. A source that Git may check
    out with CRLF (`text=auto` under `core.autocrlf=true`) would make the tag depend on the
    machine: tests/isolation/README.md did, until `*.md` was marked."""
    trees = (
        "db/assay_db/",
        "api/assay_api/",
        "schemas/assay_schemas/",
        "tss/assay_tss/",
        "workers/assay_workers/",
        "tests/isolation/",
    )
    files = {*images.WORKSPACE_MANIFESTS, "tests/__init__.py", ".dockerignore"}
    sources = [e for e in script.entries() if e.path.startswith(trees) or e.path in files]
    assert len(sources) > 100
    assert [e.path for e in sources if not e.marked_lf] == []


def test_the_editor_settings_name_every_pattern_git_marks_as_lf() -> None:
    marked = {
        pattern
        for pattern, attributes in (
            line.split(None, 1)
            for line in (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        )
        if "eol=lf" in attributes.split()
    }
    text = (REPO_ROOT / ".editorconfig").read_text(encoding="utf-8")
    section = re.search(r"^\[\{(.+)\}\]\nend_of_line = lf$", text, re.MULTILINE)
    assert section is not None
    assert set(section.group(1).split(",")) == marked
    assert text.count("end_of_line") == 1  # no other section says anything else


# --- The script -----------------------------------------------------------------------------


def test_the_listing_is_read_as_git_prints_it(script: ModuleType) -> None:
    listing = (
        "i/lf    w/crlf  attr/text eol=lf      \tapi/assay_api/storage.py\n"
        "i/lf    w/lf    attr/text eol=lf      \tapi/assay_api/audit.py\n"
        "i/lf    w/crlf  attr/text=auto        \tportal/src/main.tsx\n"
        "i/-text w/-text attr/-text            \tassets/logo.png\n"
        "i/crlf  w/crlf  attr/text eol=lf      \told/committed.py\n"
        "i/      w/mixed attr/text eol=lf      \tnew file.py\n"
    )
    found = {e.path: e for e in script.parse(listing)}
    assert len(found) == 6
    assert [path for path, e in found.items() if e.wrong_on_disk] == [
        "api/assay_api/storage.py",
        "old/committed.py",
        "new file.py",
    ]
    assert [path for path, e in found.items() if e.wrong_in_git] == ["old/committed.py"]
    # A file Git does not mark as LF is left as the machine has it.
    assert not found["portal/src/main.tsx"].wrong_on_disk


def test_crlf_is_found_in_tracked_and_new_files_and_fixed_without_a_git_change(
    script: ModuleType, tmp_path: Path
) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitattributes").write_bytes(b"*.py text eol=lf\n*.txt text=auto\n")
    (tmp_path / "tracked.py").write_bytes(b"a = 1\nb = 2\n")
    (tmp_path / "notes.txt").write_bytes(b"one\r\ntwo\r\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "first")

    # Saved with CRLF, as an editor on Windows would: Git sees no change.
    (tmp_path / "tracked.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    (tmp_path / "new.py").write_bytes(b"c = 3\r\n")
    wrong = sorted(e.path for e in script.entries(tmp_path) if e.wrong_on_disk)
    assert wrong == ["new.py", "tracked.py"]

    assert script.to_lf(tmp_path / "tracked.py") is True
    assert script.to_lf(tmp_path / "new.py") is True
    assert script.to_lf(tmp_path / "new.py") is False
    assert (tmp_path / "tracked.py").read_bytes() == b"a = 1\nb = 2\n"
    assert [e.path for e in script.entries(tmp_path) if e.wrong_on_disk] == []
    # The file Git does not mark as LF was never touched.
    assert (tmp_path / "notes.txt").read_bytes() == b"one\r\ntwo\r\n"


# --- The image tag refuses CRLF sources -----------------------------------------------------


def test_an_image_is_never_tagged_from_sources_with_crlf(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "Dockerfile").write_bytes(b"FROM scratch\n")
    (tmp_path / "pkg" / "a.py").write_bytes(b"a = 1\n")
    (tmp_path / "pkg" / "b.py").write_bytes(b"b = 2\n")
    clean = images.source_tag(tmp_path, ["Dockerfile"], ["pkg"])

    (tmp_path / "pkg" / "b.py").write_bytes(b"b = 2\r\n")
    with pytest.raises(images.CrlfSources, match=r"pkg/b\.py.*check_line_endings\.py --fix"):
        images.source_tag(tmp_path, ["Dockerfile"], ["pkg"])
    (tmp_path / "Dockerfile").write_bytes(b"FROM scratch\r\n")
    with pytest.raises(images.CrlfSources, match="2 image source file"):
        images.source_tag(tmp_path, ["Dockerfile"], ["pkg"])

    # Rewritten with LF, the tag is the one a clean checkout produces.
    (tmp_path / "Dockerfile").write_bytes(b"FROM scratch\n")
    (tmp_path / "pkg" / "b.py").write_bytes(b"b = 2\n")
    assert images.source_tag(tmp_path, ["Dockerfile"], ["pkg"]) == clean
