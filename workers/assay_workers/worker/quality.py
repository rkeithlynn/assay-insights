"""The quality score: is a page's text layer what a person sees on the page?

Step 5 of BUILD_SPEC.md section 8 reads each page's text layer, scores it, and sends the
pages that fail to Textract. The score is computed here, in the task's main process, from the
text the sandbox child returned and three numbers it reported about the page. Nothing here
opens a PDF, and nothing here is in the sandbox: the threshold comes from stack config
(`textQualityThreshold`).

    1.0   a blank page: no text, no objects, no annotations that draw anything
    0.0   the page has annotations that draw content (form fields, stamps, free text, ink):
          what they show is not in the text layer
    0.0   the page has content but no text
    else  legible x (1 - image coverage)

`legible` is the share of the text's characters that are ordinary (not the replacement
character, private-use, unassigned or control characters: what a font with no usable
character map produces), times the share of its longer words that contain a vowel. The second
factor catches text that is made of real letters and still is not language (a font whose map
is shifted). It is English only, it looks only at words of four letters or more that are not
all capitals, and it needs thirty of them before it says anything.

A page fails when its score is below the threshold. With the threshold at 0.90, an image
over about a tenth of a page sends it to Textract even when its text layer is good: a scan
with a stamped header, a scan with a hidden text layer, and a typed page with a pasted
picture all have text a person cannot check against what they see (owner decision B,
2026-11-27).

`layer_usable` says whether the text layer itself read well (it failed, if it failed, for
what is beside it on the page). Such a page's text-layer text is kept in the text object next
to Textract's, marked as not the page's source (owner, addition 1).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Not letters, marks, digits, punctuation or symbols: control (Cc), private use (Co),
# surrogate (Cs) and unassigned (Cn) characters.
_NOT_ORDINARY = frozenset({"Cc", "Co", "Cs", "Cn"})
_REPLACEMENT = "�"
_LONGER_WORD = re.compile(r"[A-Za-z]{4,}")
_VOWEL = re.compile(r"[AEIOUYaeiouy]")
# The vowel factor says nothing until the page has this many longer words.
MIN_WORDS_FOR_VOWELS = 30


@dataclass(frozen=True)
class Score:
    quality: float
    # Characters that are not white space.
    chars: int
    legible: float
    # The page has nothing on it at all.
    blank: bool

    def fails(self, threshold: float) -> bool:
        return self.quality < threshold

    def layer_usable(self, threshold: float) -> bool:
        """The text layer has text and reads well by itself."""
        return self.chars > 0 and self.legible >= threshold


def legibility(text: str) -> tuple[int, float]:
    """(characters that are not white space, the share of them that reads as language)."""
    characters = [c for c in text if not c.isspace()]
    if not characters:
        return 0, 0.0
    ordinary = sum(
        1 for c in characters if c != _REPLACEMENT and unicodedata.category(c) not in _NOT_ORDINARY
    )
    words = [w for w in _LONGER_WORD.findall(text) if not w.isupper()]
    vowels = 1.0
    if len(words) >= MIN_WORDS_FOR_VOWELS:
        vowels = sum(1 for w in words if _VOWEL.search(w)) / len(words)
    return len(characters), (ordinary / len(characters)) * vowels


def score(text: str, *, image_coverage: float, annotations: int, objects: int) -> Score:
    chars, legible = legibility(text)
    if chars == 0 and objects == 0 and annotations == 0:
        return Score(quality=1.0, chars=0, legible=1.0, blank=True)
    if annotations > 0 or chars == 0:
        quality = 0.0
    else:
        quality = legible * (1.0 - min(max(image_coverage, 0.0), 1.0))
    return Score(quality=round(quality, 4), chars=chars, legible=round(legible, 4), blank=False)
