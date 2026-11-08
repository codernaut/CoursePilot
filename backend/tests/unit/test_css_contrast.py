"""Guard for the design system: every theme x accent combination keeps WCAG AA contrast.

The test parses the token blocks of ``static/css/app.css`` (``:root``, ``:root[data-theme="dark"]``
and the ``:root[data-accent=...]`` overrides) and applies the WCAG 2.x relative-luminance formula
to the colour pairs the stylesheet actually uses. If you change a colour and this fails, pick a
different shade instead of weakening the test.
"""

import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parents[2] / "app" / "web" / "static" / "css" / "app.css"
THEMES = ("light", "dark")
ACCENTS = ("crimson", "coral", "plum", "teal", "ocean")
TEXT = 4.5  # normal text
UI = 3.0  # component boundaries, focus indicators, large text


def _rules() -> dict[str, dict[str, str]]:
    text = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    rules: dict[str, dict[str, str]] = {}
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        selector = " ".join(selector.split())
        decls = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body))
        if decls:
            rules.setdefault(selector, {}).update({k: v.strip() for k, v in decls.items()})
    return rules


def tokens(theme: str, accent: str) -> dict[str, str]:
    """The resolved custom properties for one theme and accent, as the cascade would give them."""
    rules = _rules()
    result = dict(rules[":root"])
    if theme == "dark":
        result.update(rules[':root[data-theme="dark"]'])
    if accent != "crimson":
        result.update(rules.get(f':root[data-accent="{accent}"]', {}))
        if theme == "dark":
            result.update(rules.get(f':root[data-theme="dark"][data-accent="{accent}"]', {}))
    return result


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(colour: str) -> float:
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", colour), colour
    r, g, b = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(first: str, second: str) -> float:
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_luminance_formula_matches_known_values() -> None:
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#777777", "#ffffff") == pytest.approx(4.48, abs=0.01)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0)


def test_every_theme_and_accent_defines_all_tokens() -> None:
    needed = {
        "--bg", "--surface", "--surface2", "--ink", "--muted", "--edge", "--a", "--as",
        "--on-a", "--b", "--on-b", "--bs", "--bt", "--ok", "--oks", "--bad", "--bads",
        "--focus",
    }  # fmt: skip
    for theme in THEMES:
        for accent in ACCENTS:
            assert needed <= set(tokens(theme, accent)), (theme, accent)


# (foreground, background, minimum) pairs the stylesheet uses.
THEME_PAIRS = [
    ("--ink", "--bg", TEXT),
    ("--ink", "--surface", TEXT),
    ("--ink", "--surface2", TEXT),
    ("--muted", "--bg", TEXT),
    ("--muted", "--surface", TEXT),
    ("--muted", "--surface2", TEXT),
    ("--on-b", "--b", TEXT),
    ("--bt", "--bs", TEXT),
    ("--bt", "--bg", TEXT),
    ("--ok", "--oks", TEXT),
    ("--bad", "--bads", TEXT),
    ("--bad", "--surface", TEXT),
    ("--edge", "--bg", UI),
    ("--edge", "--surface", UI),
    ("--edge", "--surface2", UI),
    ("--focus", "--bg", UI),
    ("--focus", "--surface", UI),
    ("--focus", "--surface2", UI),
]
ACCENT_PAIRS = [
    ("--on-a", "--a", TEXT),  # top bar, primary buttons, own chat bubbles
    ("--a", "--bg", TEXT),  # links on the page
    ("--a", "--surface", TEXT),
    ("--a", "--surface2", TEXT),
    ("--a", "--as", TEXT),  # chips, info alerts, badges
    ("--ink", "--as", TEXT),
    ("--muted", "--as", TEXT),
]


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("accent", ACCENTS)
def test_accent_pairs_meet_aa(theme: str, accent: str) -> None:
    t = tokens(theme, accent)
    for fg, bg, minimum in ACCENT_PAIRS:
        ratio = contrast(t[fg], t[bg])
        assert ratio >= minimum, f"{theme}/{accent}: {fg} on {bg} is {ratio:.2f}, need {minimum}"


@pytest.mark.parametrize("theme", THEMES)
def test_theme_pairs_meet_aa(theme: str) -> None:
    t = tokens(theme, "crimson")
    for fg, bg, minimum in THEME_PAIRS:
        ratio = contrast(t[fg], t[bg])
        assert ratio >= minimum, f"{theme}: {fg} on {bg} is {ratio:.2f}, need {minimum}"


# Extra pairs found by the adversarial review: text on tinted surfaces the pairs above skip.
EXTRA_THEME_PAIRS = [
    ("--ink", "--bs", TEXT),  # active drop zone
    ("--bad", "--surface2", TEXT),  # failed upload row, conversation delete on hover
    ("--ok", "--surface2", TEXT),  # finished upload row
    ("--muted", "--bads", TEXT),  # author line inside an error message
    ("--muted", "--bs", TEXT),
    ("--a", "--bads", TEXT),  # a link inside an error alert
    ("--a", "--bs", TEXT),  # a link inside a warning alert
    ("--a", "--oks", TEXT),
    ("--bt", "--surface", TEXT),
    ("--bt", "--bl", UI),  # warning alert border on its own background is decoration only
]


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("accent", ACCENTS)
def test_extra_pairs_meet_aa(theme: str, accent: str) -> None:
    t = tokens(theme, accent)
    for fg, bg, minimum in EXTRA_THEME_PAIRS:
        ratio = contrast(t[fg], t[bg])
        assert ratio >= minimum, f"{theme}/{accent}: {fg} on {bg} is {ratio:.2f}, need {minimum}"


def _body(selector: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        if " ".join(sel.split()) == selector:
            return str(body)
    raise AssertionError(f"no rule for {selector}")


def _mix(foreground: str, background: str, alpha: float) -> str:
    f = [int(foreground[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(background[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(alpha * f[i] + (1 - alpha) * b[i]):02x}" for i in range(3))


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("accent", ACCENTS)
@pytest.mark.parametrize("selector", [".nav a:hover", ".usermenu summary"])
def test_translucent_white_overlays_on_the_top_bar_keep_text_readable(
    theme: str, accent: str, selector: str
) -> None:
    """Top bar text sits on --a; hover and menu backgrounds lighten it with translucent white."""
    match = re.search(r"background:\s*rgba\(255,\s*255,\s*255,\s*([0-9.]+)\)", _body(selector))
    assert match, selector
    t = tokens(theme, accent)
    ratio = contrast(t["--on-a"], _mix("#ffffff", t["--a"], float(match.group(1))))
    assert ratio >= TEXT, f"{theme}/{accent}: {selector} text is {ratio:.2f}"


def test_accent_swatch_check_mark_is_readable_on_every_swatch() -> None:
    css = CSS.read_text(encoding="utf-8")
    colours = re.findall(r'button\[data-accent="\w+"\]\s*\{\s*--sw:\s*(#[0-9a-fA-F]{6})', css)
    assert len(colours) == len(ACCENTS)
    for colour in colours:
        assert contrast("#ffffff", colour) >= TEXT, colour


def test_without_javascript_the_system_dark_preference_is_followed() -> None:
    """Without data-theme (JavaScript off) prefers-color-scheme: dark must switch the tokens."""
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    match = re.search(
        r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root:not\(\[data-theme\]\)\s*\{([^{}]*)\}",
        css,
    )
    assert match, "missing the prefers-color-scheme fallback"
    fallback = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", match.group(1)))
    dark = _rules()[':root[data-theme="dark"]']
    assert {k: v.strip() for k, v in fallback.items()} == dark


def test_file_type_icons_have_enough_contrast_with_white_text() -> None:
    t = tokens("light", "crimson")
    icons = {k: v for k, v in t.items() if k.startswith("--ft-")}
    assert len(icons) >= 6
    for name, colour in icons.items():
        assert contrast("#ffffff", colour) >= TEXT, name
