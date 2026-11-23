"""Managed login branding assets against Cognito's limits (assay_infra.branding).

Cognito checks branding only when it creates it, during `pulumi up`. These tests, and the
same checks run by the Pulumi program, catch a bad asset before a deploy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from assay_infra.branding import (
    ASSET_EXTENSIONS,
    LOGIN_LOGO,
    LOGO_CATEGORIES,
    MARK,
    MAX_ASSET_BASE64_CHARS,
    SVG_ATTRIBUTES,
    SVG_ELEMENTS,
    Asset,
    BrandingAssetError,
    asset_problems,
    brandable_svg,
    branding_assets,
    branding_settings,
    request_problems,
    svg_asset,
    svg_size,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PORTAL_LOGO = "assets/assay-insights-logo.svg"


def svg(body: str = "<rect width='1' height='1'/>", **root: str) -> bytes:
    attributes = {"xmlns": "http://www.w3.org/2000/svg", "viewBox": "0 0 100 100", **root}
    head = " ".join(f'{k}="{v}"' for k, v in attributes.items() if v)
    return f"<svg {head}>{body}</svg>".encode()


def asset(category: str = "FORM_LOGO", content: bytes | None = None, **fields: str) -> Asset:
    defaults = {"extension": "SVG", "color_mode": "LIGHT"}
    defaults.update(fields)
    return Asset(path="test", category=category, content=content or svg(), **defaults)


# --- The assets we upload ---------------------------------------------------------------


def test_every_uploaded_asset_passes() -> None:
    assets = branding_assets(REPO_ROOT)
    assert [(a.path, a.category) for a in assets] == [
        (LOGIN_LOGO, "FORM_LOGO"),
        (MARK, "FAVICON_SVG"),
    ]
    for a in assets:
        assert asset_problems(a) == [], a.path
    assert request_problems(assets, branding_settings()) == []


def test_login_logo_is_the_portal_logo_with_a_taller_canvas() -> None:
    portal = (REPO_ROOT / PORTAL_LOGO).read_text(encoding="utf-8").splitlines()
    login = (REPO_ROOT / LOGIN_LOGO).read_text(encoding="utf-8").splitlines()
    assert portal[1:] == login[1:]  # only the <svg> line differs
    assert svg_size(login[0] + "</svg>") == (520, 140)
    # The artwork (y 8 to 112 in the portal logo) stays vertically centred.
    assert 'viewBox="0 -10 520 140"' in login[0]


def test_logo_ratio_is_within_cognitos_range() -> None:
    width, height = svg_size((REPO_ROOT / LOGIN_LOGO).read_text(encoding="utf-8"))
    assert 1 <= width / height <= 4
    assert round(width / height, 2) == 3.71


def test_the_portal_logo_would_be_rejected_as_a_login_logo() -> None:
    """The failed deploy: 520x120 is 4.33:1, outside 1:1 to 4:1."""
    with pytest.raises(BrandingAssetError, match=r"4\.33:1"):
        svg_asset(REPO_ROOT, PORTAL_LOGO, "FORM_LOGO")


def test_favicon_is_square() -> None:
    assert svg_size((REPO_ROOT / MARK).read_text(encoding="utf-8")) == (120, 120)


@pytest.mark.parametrize("path", [LOGIN_LOGO, MARK, PORTAL_LOGO])
def test_only_disallowed_attributes_are_dropped(path: str) -> None:
    original = (REPO_ROOT / path).read_text(encoding="utf-8")
    cleaned = brandable_svg(original)
    assert "role=" not in cleaned and "aria-label=" not in cleaned
    assert len(original) - len(cleaned) == len(' role="img" aria-label="Assay Insights"')


def test_uploaded_asset_sizes_leave_plenty_of_room() -> None:
    for a in branding_assets(REPO_ROOT):
        assert len(a.base64) < MAX_ASSET_BASE64_CHARS // 100


# --- Each rule ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("category", "extension"),
    [("FAVICON_SVG", "PNG"), ("FAVICON_ICO", "SVG"), ("FORM_LOGO", "ICO"), ("FORM_LOGO", "WEBP")],
)
def test_category_must_accept_the_extension(category: str, extension: str) -> None:
    problems = asset_problems(asset(category, extension=extension, content=b"x"))
    assert f"{category} does not accept {extension}" in problems


def test_documented_category_table() -> None:
    assert ASSET_EXTENSIONS["FAVICON_SVG"] == {"SVG"}
    assert ASSET_EXTENSIONS["FAVICON_ICO"] == {"ICO"}
    assert ASSET_EXTENSIONS["IDP_BUTTON_ICON"] == {"ICO", "SVG"}
    assert ASSET_EXTENSIONS["FORM_LOGO"] == {"PNG", "SVG", "JPEG"}
    assert len(ASSET_EXTENSIONS) == 15
    assert {"FORM_LOGO", "PAGE_HEADER_LOGO", "PAGE_FOOTER_LOGO"} == LOGO_CATEGORIES


def test_unknown_category_and_colour_mode() -> None:
    assert asset_problems(asset("LOGO")) == ["unknown category 'LOGO'"]
    assert "unknown colour mode 'AUTO'" in asset_problems(asset(color_mode="AUTO"))


@pytest.mark.parametrize(
    ("box", "ok"),
    [("0 0 100 100", True), ("0 0 400 100", True), ("0 0 401 100", False), ("0 0 99 100", False)],
)
def test_logo_ratio_bounds(box: str, ok: bool) -> None:
    for category in sorted(LOGO_CATEGORIES):
        problems = asset_problems(asset(category, svg(viewBox=box)))
        assert (problems == []) is ok, (category, box, problems)


def test_ratio_does_not_apply_to_other_categories() -> None:
    assert asset_problems(asset("PAGE_BACKGROUND", svg(viewBox="0 0 1000 10"))) == []


def test_favicon_must_be_square() -> None:
    problems = asset_problems(asset("FAVICON_SVG", svg(viewBox="0 0 120 100")))
    assert problems == ["favicon is 120x100, not square"]


@pytest.mark.parametrize(
    ("root", "message"),
    [
        ({"viewBox": ""}, "no viewBox"),
        ({"viewBox": "0 0 100"}, "not four numbers"),
        ({"viewBox": "0 0 0 100"}, "no area"),
        ({"width": "520", "height": "120", "viewBox": "0 -10 520 140"}, "does not match"),
        ({"width": "100%", "height": "100%"}, "plain numbers"),
        ({"width": "100"}, "plain numbers"),
    ],
)
def test_svg_size_problems(root: dict[str, str], message: str) -> None:
    problems = asset_problems(asset(content=svg(**root)))
    assert any(message in p for p in problems), problems


def test_width_and_height_matching_the_view_box_are_fine() -> None:
    content = svg(viewBox="0 0 300 100", width="300px", height="100")
    assert asset_problems(asset(content=content)) == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("<script>alert(1)</script>", "element <script> is not allowed"),
        ("<foreignObject/>", "element <foreignobject> is not allowed"),
        ("<use/>", "element <use> is not allowed"),
        ("<image width='1' height='1'/>", "element <image> is not used here"),
        ("<style>rect{}</style>", "element <style> is not used here"),
        ("<a><rect/></a>", "element <a> is not used here"),
        ("<rect onload='x()'/>", "attribute 'onload' is not allowed"),
        # Either quote style is caught.
        ("<rect role='img'/>", "attribute 'role' is not allowed"),
        ('<rect onclick="x()"/>', "attribute 'onclick' is not allowed"),
        ('<rect href="https://example.test/x"/>', "attribute 'href' is not used here"),
        ('<rect style="fill:url(x)"/>', "attribute 'style' is not used here"),
    ],
)
def test_svg_content_rules(body: str, message: str) -> None:
    assert message in asset_problems(asset("PAGE_BACKGROUND", svg(body)))


def test_allow_lists_cover_what_our_svgs_use() -> None:
    for name in ("svg", "rect", "path", "circle", "text", "tspan"):
        assert name in SVG_ELEMENTS
    for name in ("viewbox", "width", "height", "fill", "opacity", "font-family", "dx"):
        assert name in SVG_ATTRIBUTES
    assert "role" not in SVG_ATTRIBUTES and "aria-label" not in SVG_ATTRIBUTES


def test_oversized_asset() -> None:
    big = Asset(
        path="big",
        category="PAGE_BACKGROUND",
        extension="PNG",
        color_mode="LIGHT",
        content=b"\0" * 800_000,
    )
    assert any("base64 characters, over" in p for p in asset_problems(big))


def test_request_limits() -> None:
    logo, _mark = branding_assets(REPO_ROOT)
    settings = branding_settings()
    assert "two assets share a category and colour mode" in request_problems([logo, logo], settings)
    big = [
        Asset(
            path=category,
            category=category,
            extension="PNG",
            color_mode="LIGHT",
            content=b"\0" * 740_000,
        )
        for category in ("PAGE_BACKGROUND", "FORM_BACKGROUND", "PAGE_HEADER_BACKGROUND")
    ]
    # Each is under the per-asset limit; together they are over 2 MB.
    assert all(asset_problems(a) == [] for a in big)
    assert any("request of about" in p for p in request_problems([*big, logo], settings))


def test_settings_need_their_assets() -> None:
    logo, mark = branding_assets(REPO_ROOT)
    settings = branding_settings()
    assert request_problems([logo], settings) == [
        "favicon type SVG is enabled but has no FAVICON_SVG asset"
    ]
    assert request_problems([mark], settings) == [
        "the form logo is enabled but there is no FORM_LOGO asset"
    ]
    document = json.loads(settings)
    assert document["components"]["favicon"]["enabledTypes"] == ["SVG"]
