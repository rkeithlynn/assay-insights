"""Managed login branding: the settings document and the image assets.

Cognito validates branding only when it creates it, at `pulumi up`; `pulumi preview` does
not. So every asset is checked here when the Pulumi program runs, and a bad one fails the
preview and the unit tests instead of a deploy.

Documented limits (Cognito developer guide, "Managed login branding editor", section
"Image assets"; API reference, AssetType and CreateManagedLoginBranding):

- the file types each category accepts (`ASSET_EXTENSIONS`);
- `AssetType.Bytes` is base64, at most 1,000,000 characters;
- a create or update request is at most 2 MB;
- SVG files may use only the listed elements and attributes (`SVG_ELEMENTS`,
  `SVG_ATTRIBUTES`, copied from that page on 2026-11-23).

The guide gives no file-size, dimension, aspect-ratio or favicon rule beyond these. One
rule is not documented and was learned from a failed deploy (2026-11-23):

- assets of subtype LOGO need a width:height ratio between 1:1 and 4:1. Cognito read an
  SVG's size from its viewBox ("Detected width: 520.0; height: 120.0"). We apply this to
  every `*_LOGO` category.

Our own, stricter rules:

- favicons are square;
- if an SVG gives width and height, they have the viewBox's ratio, so the size Cognito
  reads is the same whichever it uses;
- no references outside the file and no embedded content (`a`, `image`, `audio`, `video`,
  `canvas`, `style`, `script`, `foreignobject` elements; `href` and `style` attributes),
  although Cognito allows some of them.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pulumi_aws as aws

LOGIN_LOGO = "assets/assay-insights-logo-login.svg"  # the portal keeps assay-insights-logo.svg
MARK = "assets/assay-insights-mark.svg"

# Brand colours as managed login wants them: RRGGBBAA, lower case. Gold on white fails WCAG
# AA contrast for text and focus rings, so links are navy and focus rings the darker gold.
NAVY = "14213dff"
NAVY_PRESSED = "0b1426ff"
GOLD_TEXT = "b07d12ff"
WHITE = "ffffffff"

# --- Documented limits ----------------------------------------------------------------------

_IMAGE = frozenset({"PNG", "SVG", "JPEG"})
ASSET_EXTENSIONS: Mapping[str, frozenset[str]] = {
    "FAVICON_ICO": frozenset({"ICO"}),
    "FAVICON_SVG": frozenset({"SVG"}),
    "EMAIL_GRAPHIC": _IMAGE,
    "SMS_GRAPHIC": _IMAGE,
    "AUTH_APP_GRAPHIC": _IMAGE,
    "PASSWORD_GRAPHIC": _IMAGE,
    "PASSKEY_GRAPHIC": _IMAGE,
    "PAGE_HEADER_LOGO": _IMAGE,
    "PAGE_HEADER_BACKGROUND": _IMAGE,
    "PAGE_FOOTER_LOGO": _IMAGE,
    "PAGE_FOOTER_BACKGROUND": _IMAGE,
    "PAGE_BACKGROUND": _IMAGE,
    "FORM_BACKGROUND": _IMAGE,
    "FORM_LOGO": _IMAGE,
    "IDP_BUTTON_ICON": frozenset({"ICO", "SVG"}),
}
COLOR_MODES = frozenset({"LIGHT", "DARK", "DYNAMIC"})
MAX_ASSET_BASE64_CHARS = 1_000_000
MAX_REQUEST_BYTES = 2_000_000
MAX_ASSETS = 40

SVG_ELEMENTS = frozenset(
    e.lower()
    for e in [
        "svg",
        "a",
        "altglyph",
        "altglyphdef",
        "altglyphitem",
        "animatecolor",
        "animatemotion",
        "animatetransform",
        "audio",
        "canvas",
        "circle",
        "clippath",
        "defs",
        "desc",
        "ellipse",
        "filter",
        "font",
        "g",
        "glyph",
        "glyphref",
        "hkern",
        "image",
        "line",
        "lineargradient",
        "marker",
        "mask",
        "metadata",
        "mpath",
        "path",
        "pattern",
        "polygon",
        "polyline",
        "radialgradient",
        "rect",
        "stop",
        "style",
        "switch",
        "symbol",
        "text",
        "textpath",
        "title",
        "tref",
        "tspan",
        "video",
        "view",
        "vkern",
        "feBlend",
        "feColorMatrix",
        "feComponentTransfer",
        "feComposite",
        "feConvolveMatrix",
        "feDiffuseLighting",
        "feDisplacementMap",
        "feDistantLight",
        "feFlood",
        "feFuncA",
        "feFuncB",
        "feFuncG",
        "feFuncR",
        "feGaussianBlur",
        "feMerge",
        "feMergeNode",
        "feMorphology",
        "feOffset",
        "fePointLight",
        "feSpecularLighting",
        "feSpotLight",
        "feTile",
        "feTurbulence",
    ]
)
SVG_ATTRIBUTES = frozenset(
    [
        "accent-height",
        "accumulate",
        "additivive",
        "alignment-baseline",
        "ascent",
        "attributename",
        "attributetype",
        "azimuth",
        "basefrequency",
        "baseline-shift",
        "begin",
        "bias",
        "by",
        "class",
        "clip",
        "clip-path",
        "clip-rule",
        "color",
        "color-interpolation",
        "color-interpolation-filters",
        "color-profile",
        "color-rendering",
        "cx",
        "cy",
        "d",
        "dx",
        "dy",
        "diffuseconstant",
        "direction",
        "display",
        "divisor",
        "dur",
        "edgemode",
        "elevation",
        "end",
        "fill",
        "fill-opacity",
        "fill-rule",
        "filter",
        "filterunits",
        "flood-color",
        "flood-opacity",
        "font-family",
        "font-size",
        "font-size-adjust",
        "font-stretch",
        "font-style",
        "font-variant",
        "font-weight",
        "fx",
        "fy",
        "g1",
        "g2",
        "glyph-name",
        "glyphref",
        "gradientunits",
        "gradienttransform",
        "height",
        "href",
        "id",
        "image-rendering",
        "in",
        "in2",
        "k",
        "k1",
        "k2",
        "k3",
        "k4",
        "kerning",
        "keypoints",
        "keysplines",
        "keytimes",
        "lang",
        "lengthadjust",
        "letter-spacing",
        "kernelmatrix",
        "kernelunitlength",
        "lighting-color",
        "local",
        "marker-end",
        "marker-mid",
        "marker-start",
        "markerheight",
        "markerunits",
        "markerwidth",
        "maskcontentunits",
        "maskunits",
        "max",
        "mask",
        "media",
        "method",
        "mode",
        "min",
        "name",
        "numoctaves",
        "offset",
        "operator",
        "opacity",
        "order",
        "orient",
        "orientation",
        "origin",
        "overflow",
        "paint-order",
        "path",
        "pathlength",
        "patterncontentunits",
        "patterntransform",
        "patternunits",
        "points",
        "preservealpha",
        "preserveaspectratio",
        "r",
        "rx",
        "ry",
        "radius",
        "refx",
        "refy",
        "repeatcount",
        "repeatdur",
        "restart",
        "result",
        "rotate",
        "scale",
        "seed",
        "shape-rendering",
        "specularconstant",
        "specularexponent",
        "spreadmethod",
        "stddeviation",
        "stitchtiles",
        "stop-color",
        "stop-opacity",
        "stroke-dasharray",
        "stroke-dashoffset",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-miterlimit",
        "stroke-opacity",
        "stroke",
        "stroke-width",
        "style",
        "surfacescale",
        "tabindex",
        "targetx",
        "targety",
        "transform",
        "text-anchor",
        "text-decoration",
        "text-rendering",
        "textlength",
        "type",
        "u1",
        "u2",
        "unicode",
        "values",
        "viewbox",
        "visibility",
        "vert-adv-y",
        "vert-origin-x",
        "vert-origin-y",
        "width",
        "word-spacing",
        "wrap",
        "writing-mode",
        "xchannelselector",
        "ychannelselector",
        "x",
        "x1",
        "x2",
        "xmlns",
        "y",
        "y1",
        "y2",
        "z",
        "zoomandpan",
    ]
)

# --- Learned from Cognito's errors, and our own rules ------------------------------------------

LOGO_CATEGORIES = frozenset(c for c in ASSET_EXTENSIONS if c.endswith("_LOGO"))
LOGO_RATIO_RANGE = (1.0, 4.0)
SQUARE_CATEGORIES = frozenset({"FAVICON_ICO", "FAVICON_SVG"})
FORBIDDEN_ELEMENTS = frozenset(
    {"a", "image", "audio", "video", "canvas", "style", "script", "foreignobject"}
)
FORBIDDEN_ATTRIBUTES = frozenset({"href", "style"})

# Attribute values in either quote style (an unquoted value is not valid XML).
_ATTRIBUTE = re.compile(r"""\s([A-Za-z][A-Za-z0-9:._-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_ELEMENT = re.compile(r"<([A-Za-z][A-Za-z0-9:._-]*)")
_ROOT = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)
_NUMBER = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(?:px)?\s*$")


class BrandingAssetError(ValueError):
    """An asset Cognito would reject (or that breaks our own rules). Fails the preview."""


@dataclass(frozen=True)
class Asset:
    path: str
    category: str
    extension: str
    color_mode: str
    content: bytes

    @property
    def base64(self) -> str:
        return base64.b64encode(self.content).decode("ascii")

    def args(self) -> aws.cognito.ManagedLoginBrandingAssetArgs:
        return aws.cognito.ManagedLoginBrandingAssetArgs(
            category=self.category,
            color_mode=self.color_mode,
            extension=self.extension,
            bytes=self.base64,
        )


def brandable_svg(svg: str) -> str:
    """The SVG with every attribute that managed login does not allow removed."""

    def keep(match: re.Match[str]) -> str:
        return match.group(0) if match.group(1).lower() in SVG_ATTRIBUTES else ""

    return _ATTRIBUTE.sub(keep, svg)


def _root_attributes(svg: str) -> dict[str, str]:
    root = _ROOT.search(svg)
    if root is None:
        raise BrandingAssetError("no <svg> element")
    return {
        m.group(1).lower(): m.group(2) if m.group(2) is not None else m.group(3)
        for m in _ATTRIBUTE.finditer(root.group(0))
    }


def svg_size(svg: str) -> tuple[float, float]:
    """The width and height Cognito reads: the viewBox's, checked against width/height."""
    attributes = _root_attributes(svg)
    box = attributes.get("viewbox")
    if box is None:
        raise BrandingAssetError("SVG has no viewBox")
    parts = box.replace(",", " ").split()
    if len(parts) != 4:
        raise BrandingAssetError(f"SVG viewBox {box!r} is not four numbers")
    width, height = float(parts[2]), float(parts[3])
    if width <= 0 or height <= 0:
        raise BrandingAssetError(f"SVG viewBox {box!r} has no area")
    if "width" in attributes or "height" in attributes:
        given = [_NUMBER.match(attributes.get(k, "")) for k in ("width", "height")]
        if not all(given):
            raise BrandingAssetError("SVG width and height must both be plain numbers")
        w, h = (float(m.group(1)) for m in given if m is not None)
        if abs(w / h - width / height) > 1e-6:
            raise BrandingAssetError(
                f"SVG width:height {w:g}:{h:g} does not match its viewBox {width:g}:{height:g}"
            )
    return width, height


def _svg_problems(category: str, svg: str) -> list[str]:
    problems: list[str] = []
    elements = {e.lower() for e in _ELEMENT.findall(svg)}
    attributes = {m.group(1).lower() for m in _ATTRIBUTE.finditer(svg)}
    problems += [f"element <{e}> is not allowed" for e in sorted(elements - SVG_ELEMENTS)]
    problems += [f"element <{e}> is not used here" for e in sorted(elements & FORBIDDEN_ELEMENTS)]
    problems += [f"attribute {a!r} is not allowed" for a in sorted(attributes - SVG_ATTRIBUTES)]
    problems += [
        f"attribute {a!r} is not used here" for a in sorted(attributes & FORBIDDEN_ATTRIBUTES)
    ]
    try:
        width, height = svg_size(svg)
    except BrandingAssetError as exc:
        return [*problems, str(exc)]
    ratio = width / height
    low, high = LOGO_RATIO_RANGE
    if category in LOGO_CATEGORIES and not low <= ratio <= high:
        problems.append(
            f"logo width:height is {ratio:.2f}:1 ({width:g}x{height:g}); "
            f"Cognito needs {low:g}:1 to {high:g}:1"
        )
    if category in SQUARE_CATEGORIES and abs(ratio - 1) > 1e-6:
        problems.append(f"favicon is {width:g}x{height:g}, not square")
    return problems


def asset_problems(asset: Asset) -> list[str]:
    problems: list[str] = []
    allowed = ASSET_EXTENSIONS.get(asset.category)
    if allowed is None:
        return [f"unknown category {asset.category!r}"]
    if asset.extension not in allowed:
        problems.append(f"{asset.category} does not accept {asset.extension}")
    if asset.color_mode not in COLOR_MODES:
        problems.append(f"unknown colour mode {asset.color_mode!r}")
    if len(asset.base64) > MAX_ASSET_BASE64_CHARS:
        problems.append(f"{len(asset.base64)} base64 characters, over {MAX_ASSET_BASE64_CHARS}")
    if asset.extension == "SVG":
        problems += _svg_problems(asset.category, asset.content.decode("utf-8"))
    return problems


def request_problems(assets: Sequence[Asset], settings: str) -> list[str]:
    problems: list[str] = []
    if len(assets) > MAX_ASSETS:
        problems.append(f"{len(assets)} assets, over {MAX_ASSETS}")
    categories = [(a.category, a.color_mode) for a in assets]
    if len(set(categories)) != len(categories):
        problems.append("two assets share a category and colour mode")
    # Everything the settings switch on must have its asset.
    document = json.loads(settings)
    components = document.get("components", {})
    present = {a.category for a in assets}
    for kind in components.get("favicon", {}).get("enabledTypes", []):
        if f"FAVICON_{kind}" not in present:
            problems.append(f"favicon type {kind} is enabled but has no FAVICON_{kind} asset")
    if components.get("form", {}).get("logo", {}).get("enabled") and "FORM_LOGO" not in present:
        problems.append("the form logo is enabled but there is no FORM_LOGO asset")
    # Generous: the request also carries IDs and JSON framing, well under 1 KB.
    size = sum(len(a.base64) for a in assets) + len(settings.encode("utf-8")) + 1024
    if size > MAX_REQUEST_BYTES:
        problems.append(f"request of about {size} bytes, over {MAX_REQUEST_BYTES}")
    return problems


def svg_asset(repo_root: Path, path: str, category: str) -> Asset:
    svg = brandable_svg((repo_root / path).read_text(encoding="utf-8"))
    asset = Asset(
        path=path,
        category=category,
        extension="SVG",
        color_mode="LIGHT",
        content=svg.encode("utf-8"),
    )
    problems = asset_problems(asset)
    if problems:
        raise BrandingAssetError(f"{path} as {category}: " + "; ".join(problems))
    return asset


def branding_assets(repo_root: Path) -> list[Asset]:
    """The assets both pools' managed login uses, each checked; raises if any would fail."""
    assets = [
        svg_asset(repo_root, LOGIN_LOGO, "FORM_LOGO"),
        svg_asset(repo_root, MARK, "FAVICON_SVG"),
    ]
    problems = request_problems(assets, branding_settings())
    if problems:
        raise BrandingAssetError("; ".join(problems))
    return assets


def branding_settings() -> str:
    """A partial settings document; Cognito keeps its defaults for everything else."""
    pressed = {"backgroundColor": NAVY_PRESSED, "textColor": WHITE}
    return json.dumps(
        {
            "categories": {
                "global": {
                    "colorSchemeMode": "LIGHT",
                    "pageHeader": {"enabled": False},
                    "pageFooter": {"enabled": False},
                }
            },
            "components": {
                "favicon": {"enabledTypes": ["SVG"]},
                "form": {
                    "logo": {
                        "enabled": True,
                        "formInclusion": "IN",
                        "location": "CENTER",
                        "position": "TOP",
                    }
                },
                "pageText": {"lightMode": {"headingColor": NAVY}},
                "primaryButton": {
                    "lightMode": {
                        "defaults": {"backgroundColor": NAVY, "textColor": WHITE},
                        "hover": pressed,
                        "active": pressed,
                    }
                },
            },
            "componentClasses": {
                "focusState": {"lightMode": {"borderColor": GOLD_TEXT}},
                "link": {
                    "lightMode": {
                        "defaults": {"textColor": NAVY},
                        "hover": {"textColor": NAVY_PRESSED},
                    }
                },
            },
        },
        sort_keys=True,
    )
