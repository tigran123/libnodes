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
    for path in ("/devices", "/library", "/jobs", "/devices.yaml", "/keys"):
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


def test_the_theme_icon_names_the_mode_you_get():
    """The glyph is the affordance: in dark you are offered the sun, in light the moon.
    Rendered server-side and swapped again in app.js, so the two have to agree."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "libnodes"
    base = (root / "templates" / "base.html").read_text()
    js = (root / "static" / "app.js").read_text()

    assert "'☾' if theme == 'light' else '☼'" in base
    # Same pair in the client-side swap, and the opposite of the theme just applied.
    assert 'light ? "☾" : "☼"' in js
    # Neither glyph has an emoji presentation; U+2600 does, and would have gone colour
    # on Android without a variation selector.
    assert "☀" not in base and "☀" not in js


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
