"""Theme selection, and the held-job semantics of the offline dialog."""

from __future__ import annotations


# --------------------------------------------------------------------- theme --


def _css() -> str:
    from pathlib import Path

    return (
        Path(__file__).resolve().parent.parent / "libnodes" / "static" / "app.css"
    ).read_text()


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hue_and_lightness(hex_colour: str) -> tuple[float, float]:
    import colorsys

    r, g, b = (c / 255 for c in _rgb(hex_colour))
    hue, lightness, _ = colorsys.rgb_to_hls(r, g, b)
    return hue * 360, lightness * 100


def _contrast(a: str, b: str) -> float:
    """WCAG contrast ratio, so the thresholds below are the published ones: 4.5:1 for
    small text, 3:1 for a graphic."""
    def relative(hex_colour: str) -> float:
        def channel(c: float) -> float:
            c /= 255
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        r, g, b = _rgb(hex_colour)
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)

    hi, lo = sorted((relative(a), relative(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


_THEMES = {"dark": ":root {", "light": ':root[data-theme="light"] {'}


def _token(css: str, theme: str, name: str) -> str:
    """One palette token, resolving a `var(--other)` alias one level -- which is how the
    dark theme says "the fill is the ink" without repeating the hex."""
    import re

    block = css.split(_THEMES[theme], 1)[1].split("}", 1)[0]
    values = dict(re.findall(r"--([\w-]+):\s*([^;]+);", block))
    value = values[name].strip()
    alias = re.fullmatch(r"var\(--([\w-]+)\)", value)
    if alias:
        value = values[alias.group(1)].strip()
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), f"{theme} --{name} is {value!r}"
    return value


def _rule(css: str, selector: str) -> str:
    import re

    match = re.search(r"^" + re.escape(selector) + r"\s*\{([^}]*)\}", css, re.MULTILINE)
    assert match, f"no rule for {selector}"
    return match.group(1)


def test_amber_does_not_read_as_red():
    """`sleeping` and `offline` are different states and a 6px dot is what says which.

    Two pairs have failed this. The design bundle's dark one was #d3a05a on #d1685c: 29deg
    of hue and *identical* lightness, 1.53:1 against each other. The light one was worse in
    practice — #92661f on #b23d31, both mid-dark and saturated, reported as "almost the
    same as red". Hue *and* lightness, because each pair passed on one of them alone.
    """
    css = _css()
    for theme in _THEMES:
        # The dot's own colour, which in the light theme is not the ink -- see
        # test_warning_text_stays_readable for why they had to part company.
        amber = _token(css, theme, "warn-fill")
        red = _token(css, theme, "err")
        amber_h, amber_l = _hue_and_lightness(amber)
        red_h, red_l = _hue_and_lightness(red)

        assert abs(amber_h - red_h) >= 30, (
            f"{theme}: amber {amber} is only {abs(amber_h - red_h):.0f}deg of hue from "
            f"red {red}"
        )
        assert amber_l - red_l >= 5, (
            f"{theme}: amber {amber} (L{amber_l:.0f}) is not clearly lighter than red "
            f"{red} (L{red_l:.0f}) — hue alone does not separate them at 6px"
        )


def test_warning_text_stays_readable():
    """Why the light theme has two ambers instead of one.

    `--warn` is small mono text — a battery percentage, `connection refused` on a row — so
    it owes 4.5:1 to the panel behind it. On white that caps it at a dark mustard, which is
    exactly the colour that read as red. Brightening this token is the tempting fix and it
    trades a legible warning for a distinguishable one; `--warn-fill` exists so neither has
    to give. A fill owes only 3:1, and only to its background.
    """
    css = _css()
    for theme in _THEMES:
        ink = _token(css, theme, "warn")
        panel = _token(css, theme, "panel")
        assert _contrast(ink, panel) >= 4.5, (
            f"{theme}: --warn {ink} on --panel {panel} is "
            f"{_contrast(ink, panel):.2f}:1, under 4.5:1 for small text"
        )


def test_a_state_tint_is_the_state_colour():
    """The badge fills and dot halos are rgba() literals of the palette, not `var()`s --
    alpha needs the channels spelled out. So a token can be changed while the literals go
    on wearing the old colour, in the two places the state is drawn rather than written."""
    import re

    for theme, prefix in (("dark", ""), ("light", ':root[data-theme="light"] ')):
        css = _css()
        for state in ("ok", "warn", "err"):
            # A halo rings the dot, so it follows the dot's fill; a badge is ink and a 1px
            # border, so it follows the ink.
            for selector, token in (
                (f"{prefix}.badge-{state}", state),
                (f"{prefix}.dot-{state}", "warn-fill" if state == "warn" else state),
            ):
                expected = _rgb(_token(css, theme, token))
                found = [
                    tuple(int(c) for c in triple)
                    for triple in re.findall(
                        r"rgba\((\d+),\s*(\d+),\s*(\d+)", _rule(css, selector)
                    )
                ]
                assert found, f"{selector}: no rgba tint to check"
                for triple in found:
                    assert triple == expected, (
                        f"{theme} {selector} is tinted rgb{triple} while --{token} is "
                        f"rgb{expected}"
                    )


def test_btn_default_does_not_wear_the_hover_background():
    """An emphasised button must not arrive already looking pressed.

    `.btn-default` used to set `background: var(--hover)` -- the exact background
    `.btn:hover` paints -- so it rendered permanently hovered and had no hover response
    left. Invisible while a whole group wore it (the Actions dialog), obvious the moment
    a plain `.btn` stood beside one (Test next to Actions on a device row).

    Asserted against the stylesheet because there is no browser here to read a computed
    style from; the rule is short enough that its text is the behaviour.
    """
    from pathlib import Path

    css = Path(__file__).resolve().parent.parent / "libnodes" / "static" / "app.css"
    block = css.read_text().split(".btn-default {")[1].split("}")[0]
    assert "background" not in block
    assert "border-color" in block          # still distinguishable from a plain .btn


async def test_defaults_to_dark(client):
    r = await client.get("/devices")
    assert 'data-theme="light"' not in r.text


async def test_light_cookie_is_rendered_server_side(client):
    """Server-stamped rather than JS-applied, so there is no flash of dark first."""
    r = await client.get("/devices", cookies={"libnodes_theme": "light"})
    assert 'data-theme="light"' in r.text


async def test_toggle_is_present_on_every_page(client):
    for path in ("/devices", "/library", "/jobs", "/settings"):
        r = await client.get(path)
        assert "data-theme-toggle" in r.text, path


def test_the_theme_toggle_outranks_btn_on_the_cascade():
    """`.theme-toggle` and `.btn` have identical specificity, so whichever is written
    later wins. The toggle's block used to sit ~140 lines *above* `.btn`, which meant its
    font-size never applied and the button rendered at `.btn`'s 11px -- verified in a real
    browser before this was fixed. Order is the whole mechanism, so order is what is
    pinned here."""
    from pathlib import Path

    css = (
        Path(__file__).resolve().parent.parent / "libnodes" / "static" / "app.css"
    ).read_text()
    btn = css.index("\n.btn {")
    hover = css.index("\n.btn:hover {")
    toggle = css.index("\n.theme-toggle {")
    assert toggle > btn, ".theme-toggle is back above .btn and is inert again"
    assert css.index("\n.theme-toggle:hover {") > hover


#: Moons the Nexus 10 has no font for, suns that have one, and the emoji-presentation
#: pair that would go colour on Android -- none of them may reach the page.
NO_FONT_FOR_THESE = ("☾", "☽", "☼", "☀", "🌙", "🌜", "◐", "◑")


def test_the_theme_icon_is_drawn_and_not_typed():
    """The icon is the affordance: in dark you are offered the sun, in light the moon.
    Both are SVG, and that is not decoration. U+263E is in none of the Nexus 10's 91
    /system/fonts (Android 5.1, cmaps checked), so a glyph pair drew a tofu box on the
    fleet's own tablet beside a ☼ that rendered fine -- NotoSansSymbols-Subsetted and
    NotoSerif carry U+263C and nothing there carries a moon. The only moon on that device
    is U+1F319 in NotoColorEmoji, which is the colour-emoji trap, not the fix."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "libnodes"
    base = (root / "templates" / "base.html").read_text()
    js = (root / "static" / "app.js").read_text()
    css = (root / "static" / "app.css").read_text()

    start = base.index('<button class="btn btn-icon theme-toggle"')
    toggle = base[start : base.index("</button>", start)]
    assert '<svg class="theme-moon"' in toggle
    assert '<svg class="theme-sun"' in toggle
    # Nothing stands in for either one in the markup or the script. The Jinja comment
    # above the button names the glyphs it is explaining, which is why this reads the
    # button and not the file -- and why the rendered page is checked separately below.
    for text in (toggle, js):
        for glyph in NO_FONT_FOR_THESE:
            assert glyph not in text, glyph

    # Which icon shows is CSS off data-theme, so app.js no longer re-renders a pair the
    # server also renders -- the two used to have to hold the same glyphs by hand.
    assert "theme-moon" not in js and "theme-sun" not in js
    assert ".theme-toggle .theme-moon,\n[data-theme=\"light\"] .theme-toggle .theme-sun {" in css
    assert '[data-theme="light"] .theme-toggle .theme-moon {' in css


def test_log_out_is_an_icon_sized_like_the_toggle():
    """The words were 80 of the 363 px a 412px phone has for the top bar, and left the
    index chip ~165 px, so it read "index fresh …". An SVG and not a glyph for the
    toggle's reason above, and a rule after .btn for the cascade's."""
    import re
    from pathlib import Path

    base = (Path(__file__).resolve().parent.parent / "libnodes" / "templates" / "base.html").read_text()
    start = base.index('<button class="btn btn-icon logout"')
    button = base[start : base.index("</button>", start)]
    assert "<svg" in button
    assert 'aria-label="Log out"' in button
    # The only text left is inside attributes and the Jinja comment.
    visible = re.sub(r"\{#.*?#\}|<[^>]*>", "", button[button.index(">") + 1 :], flags=re.S)
    assert visible.strip() == ""

    css = _css()
    assert css.index("\n.logout,\n.rescan {") > css.index("\n.btn {")


def test_the_pinned_crumb_owns_the_padding_above_it():
    """Chrome pins a sticky child at its scroller's content box, so a `padding-top` on
    .lib-body is a band the rows scroll through above the crumb -- 18.9 real px on a
    phone, measured. The 14px has to be the opaque crumb's own."""
    import re

    css = _css()
    body = css[css.index("\n.lib-body {") : css.index("}", css.index("\n.lib-body {"))]
    pad = re.search(r"padding: (\S+)", body).group(1)
    assert pad in ("0", "0px"), f".lib-body has a top padding again: {pad}"
    crumb = css[css.index("\n.pathline {") : css.index("}", css.index("\n.pathline {"))]
    assert "position: sticky" in crumb
    assert re.search(r"padding-top: [1-9]", crumb)


async def test_both_theme_icons_are_always_in_the_dom(client):
    """CSS picks between them, so both ship on every page and in either theme. If only
    the current one were rendered the client toggle would have nothing to switch to."""
    for cookie in ({}, {"libnodes_theme": "light"}):
        r = await client.get("/devices", cookies=cookie)
        assert 'class="theme-moon"' in r.text and 'class="theme-sun"' in r.text
        # And no character the tablet would have to find a font for reaches it.
        for glyph in NO_FONT_FOR_THESE:
            assert glyph not in r.text, glyph


async def test_unknown_cookie_value_falls_back_to_dark(client):
    r = await client.get("/devices", cookies={"libnodes_theme": "banana"})
    assert 'data-theme="light"' not in r.text


# ---------------------------------------------------------------- held jobs --


async def test_unticking_auto_start_holds_the_job(client, app):
    """The bug: the checkbox was disabled, so this could not be expressed at all."""
    r = await client.post(
        "/jobs",
        data={
            "device": "kobo",
            "path": "Science/Physics",
            "confirmed": "yes",
            # "auto" absent = the user cleared the box
        },
    )
    assert r.status_code == 200
    job = app.state.lib.jobs.recent()[0]
    assert job.state == "deferred"
    assert job.hold is True


async def test_leaving_auto_start_ticked_does_not_hold(client, app):
    await client.post(
        "/jobs",
        data={
            "device": "kobo",
            "path": "Science/Physics",
            "confirmed": "yes",
            "auto": "on",
        },
    )
    job = app.state.lib.jobs.recent()[0]
    assert job.state == "deferred"
    assert job.hold is False


async def test_watcher_ignores_held_jobs(client, app, monkeypatch):
    """A held job must not start just because the device turned up."""
    import time

    from libnodes.probe import Reachability

    lib = app.state.lib
    await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    job = lib.jobs.recent()[0]
    assert job.hold is True

    # The node comes online; the watcher's promotion rule must still skip it.
    lib.probe._slot("kobo").reach = Reachability(
        state="online", last_ok=time.time(), checked_at=time.time()
    )
    promotable = [
        j
        for j in lib.jobs.active()
        if j.state == "deferred"
        and not j.hold
        and lib.probe.status(j.device_id).online
    ]
    assert promotable == []


async def test_held_job_can_be_started_manually(client, app):
    lib = app.state.lib
    await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    job = lib.jobs.recent()[0]

    r = await client.post(f"/jobs/{job.id}/start")
    assert r.status_code == 200
    after = lib.jobs.get(job.id)
    assert after.hold is False
    assert after.state in ("queued", "running", "failed", "done")


async def test_held_job_row_offers_start(client, app):
    await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    job_id = app.state.lib.jobs.recent()[0].id
    r = await client.get("/jobs/rows")
    assert "HELD" in r.text
    assert f'hx-post="/jobs/{job_id}/start"' in r.text


async def test_hold_survives_a_restart(settings, app, client):
    """hold is persisted, so a restart does not silently make a held job auto-run."""
    from libnodes.jobs import JobStore

    await client.post(
        "/jobs",
        data={"device": "kobo", "path": "Science/Physics", "confirmed": "yes"},
    )
    job_id = app.state.lib.jobs.recent()[0].id

    fresh = JobStore(settings.jobs_db)
    assert fresh.get(job_id).hold is True


def test_schema_migration_adds_hold_to_an_old_database(tmp_path):
    """Existing deployments have a jobs.db without the column."""
    import sqlite3

    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,"
        " sources TEXT NOT NULL, label TEXT NOT NULL, dest TEXT, state TEXT NOT NULL,"
        " created_at REAL, started_at REAL, finished_at REAL, files_done INTEGER,"
        " files_total INTEGER, bytes_done INTEGER, bytes_total INTEGER, pct REAL,"
        " exit_code INTEGER, error TEXT, argv TEXT, attempt INTEGER, dry_run INTEGER);"
    )
    conn.execute(
        "INSERT INTO jobs (device_id, sources, label, state) VALUES ('d','[]','x','done')"
    )
    conn.commit()
    conn.close()

    from libnodes.jobs import JobStore

    store = JobStore(db)          # must migrate, not explode
    job = store.recent()[0]
    assert job.hold is False
    # files_sent/entries_* arrived the same way. The old files_done column is left where
    # it is: its rows hold entry counts, and relabelling those as transfers would invent
    # history rather than migrate it.
    assert job.files_sent == 0
    assert job.entries_total == 0


# ---------------------------------------------------------------------- mark --


def test_the_mark_is_literally_the_same_file_as_the_favicon():
    """One identity, one file. The tab, the rail and the login card all point at
    static/icon.svg, so there is no second copy of the drawing to fall out of step.

    This replaced an inline SVG that tinted itself from var(--accent), which is exactly
    how the tab and the rail came to show different-coloured books at the same time.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "libnodes"
    mark = (root / "templates" / "brand_mark.html").read_text()
    assert "asset('icon.svg')" in mark
    assert "<svg" not in mark, "the drawing is back in the template; it belongs in the file"

    for page in ("base.html", "login.html"):
        text = (root / "templates" / page).read_text()
        assert '{% include "brand_mark.html" %}' in text, page
        assert "asset('icon.svg')" in text, page      # the favicon link


def test_the_mark_does_not_follow_the_theme():
    """It is a logo, and it is the same everywhere on purpose.

    Tracking var(--accent) cannot work: white books on the dark theme's accent (#9a8ce6)
    measure 2.89:1, under the 3:1 floor for a graphical object, so an accent-following
    mark has to restyle its books per theme -- which is what put dark books in the tab
    beside white ones in the rail. #7159dd carries white books at 5.03:1 and still holds
    an edge against both the dark rail (3.89:1) and the light one (4.41:1).
    """
    from pathlib import Path

    icon = (Path(__file__).resolve().parent.parent
            / "libnodes" / "static" / "icon.svg").read_text()
    svg = icon[icon.index("<svg"):]
    assert 'fill="#7159dd"' in svg          # the tile
    assert 'fill="#ffffff"' in svg          # the books, painted the same everywhere
    assert "var(--" not in svg              # a file cannot read the page's tokens
    assert "<mask" not in svg               # painted, not knocked out: see below


def test_the_books_are_painted_not_knocked_out():
    """As holes they would take the colour of whatever is behind them, which for a
    favicon is a tab strip of unknown colour -- and on a dark one the icon degrades to a
    featureless purple blob."""
    from pathlib import Path

    icon = (Path(__file__).resolve().parent.parent
            / "libnodes" / "static" / "icon.svg").read_text()
    svg = icon[icon.index("<svg"):]
    assert "fill-opacity" not in svg and 'fill="none"' not in svg
    assert svg.count("<rect") == 5          # tile, three books, shelf


def test_the_favicon_is_well_formed_xml():
    """An .svg file is parsed as XML, not HTML, and XML forbids a double hyphen inside a
    comment.

    The first version of this file explained itself using the accent token's real name,
    which begins with two hyphens. That made the document ill formed, so the browser
    dropped it and went on showing the previous favicon -- with a 200 in the network tab
    and nothing anywhere to say why. The inline mark in brand_mark.html cannot hit this,
    because the HTML parser tolerates what XML rejects, so only the file needs the guard.
    """
    import xml.dom.minidom
    from pathlib import Path

    icon = Path(__file__).resolve().parent.parent / "libnodes" / "static" / "icon.svg"
    doc = xml.dom.minidom.parse(str(icon))          # raises if not well formed
    assert doc.documentElement.tagName == "svg"
    assert len(doc.getElementsByTagName("rect")) == 5


# ------------------------------------------------------------------- dialogs --


def test_a_dialog_taller_than_the_screen_scrolls_and_keeps_its_close():
    """sigmaai.au's Actions on a phone (shots/mobile-actions.jpg): .backdrop centres its
    dialog in a fixed box that does not scroll, so a dialog taller than the screen spilled
    past both edges and took its head and its Close with it. The dialog is capped at the
    viewport -- divided by --scale, because zoom does not scale viewport units -- and the
    body scrolls, so the foot is always on screen."""
    css = _css()
    dialog = _rule(css, ".dialog")
    assert "max-height: calc(100vh / var(--scale) - 40px)" in dialog
    assert "flex-direction: column" in dialog
    body = _rule(css, ".dialog-body")
    assert "overflow-y: auto" in body and "min-height: 0" in body
    assert "flex-shrink: 0" in _rule(css, ".dialog-head,\n.dialog-foot")
    # A child with its own scroller would otherwise be squeezed before the body scrolls.
    assert "flex-shrink: 0" in _rule(css, ".dialog-body > *")


def test_an_action_note_drops_under_its_button_on_a_narrow_dialog():
    """Unwrapped, the 150px button left the note ~120px of a phone's dialog and the Pull
    note stood one word to a line."""
    css = _css()
    assert "flex-wrap: wrap" in _rule(css, ".action-head")
    assert "flex: 1 1 220px" in _rule(css, ".action-note")


def test_a_dialog_can_be_left_without_its_close_button():
    """Close in the foot was the only way out; a tap on the backdrop or Escape now removes
    the dialog, which is all Close does."""
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent
          / "libnodes" / "static" / "app.js").read_text()
    assert 'contains("backdrop")' in js
    assert 'e.key !== "Escape"' in js
