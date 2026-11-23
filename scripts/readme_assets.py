#!/usr/bin/env python3
"""Generate the README artwork in ``assets/readme/``.

Every figure that shows a number reads it from the same place the docs do: token capacity comes from
``scripts/free_tier_capacity.py`` and the test count from the test suite. Run after changing either:

    python scripts/readme_assets.py            # writes assets/readme/*.svg
    python scripts/readme_assets.py --png      # also renders assets/social-preview.png (needs cairosvg)

The SVGs are self-contained, use no external fonts or scripts, and only animate when the viewer has not
asked for reduced motion. Their static frame is the finished state, so renderers without CSS animation
(and people who prefer reduced motion) see the complete picture.
"""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "readme"
sys.path.insert(0, str(ROOT / "scripts"))

import free_tier_capacity as cap  # noqa: E402

FONT = "Inter, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "'JetBrains Mono', 'SFMono-Regular', Menlo, Consolas, monospace"
TEAL, BLUE, VIOLET, AMBER = "#35DFC1", "#62A9FF", "#A78BFA", "#F5B84A"
REDUCED = "@media (prefers-reduced-motion: no-preference) {"


@dataclass(frozen=True)
class Theme:
    name: str
    bg: str
    card: str
    border: str
    text: str
    muted: str
    faint: str
    teal: str
    blue: str
    violet: str
    amber: str


DARK = Theme(
    "dark", "#0B1220", "#111B2B", "#22344A", "#E8F1FC", "#93A8C0", "#2A3B52", TEAL, BLUE, VIOLET, AMBER
)
LIGHT = Theme(
    "light",
    "#FFFFFF",
    "#F5F8FC",
    "#D5E1EE",
    "#0F1B2A",
    "#55697F",
    "#E3EBF4",
    "#0E9E85",
    "#2F6FD6",
    "#6D4FD8",
    "#B7791F",
)


def text_width(s: str, size: float, bold: bool = True) -> float:
    """Approximate rendered width of ``s`` in a sans-serif face. Good enough to size pills and cells."""
    wide = sum(1 for c in s if c in "MWmw@%")
    narrow = sum(1 for c in s if c in "il.,:;|!'· ")
    upper = sum(1 for c in s if c.isupper() or c.isdigit())
    rest = len(s) - wide - narrow - upper
    per = 0.6 if bold else 0.55
    return size * (wide * 0.9 + narrow * 0.3 + upper * (per + 0.06) + rest * per)


def fmt_tokens(n: float) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.1f}B"
    if n >= 1e6:
        return f"{n / 1e6:.0f}M"
    return f"{n / 1e3:.0f}K"


def count_tests() -> int:
    pattern = re.compile(r"^\s*(?:async\s+)?def test_", re.M)
    return sum(len(pattern.findall(p.read_text())) for p in (ROOT / "tests").glob("test_*.py"))


def write(name: str, svg: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(svg.strip() + "\n")


# ---------------------------------------------------------------------------------------------- badges

ICONS = {
    "python": "M8 3h5a3 3 0 0 1 3 3v4H8a3 3 0 0 0-3 3v2H4a2 2 0 0 1-2-2V9a3 3 0 0 1 3-3h3zM16 21h-5a3 3 0 0 1-3-3v-4h8a3 3 0 0 0 3-3V9h1a2 2 0 0 1 2 2v4a3 3 0 0 1-3 3h-3z",
    "api": "M8 7l-5 5 5 5M16 7l5 5-5 5M14 4l-4 16",
    "token": "M12 3l8 4.5v9L12 21l-8-4.5v-9zM12 12l8-4.5M12 12v9M12 12L4 7.5",
    "route": "M5 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM19 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM7 17h4a4 4 0 0 0 4-4V9a2 2 0 0 1 2-2",
    "tests": "M9 3h6M10 3v6l-5.4 9.4A2 2 0 0 0 6.3 21h11.4a2 2 0 0 0 1.7-2.6L14 9V3M7.5 15h9",
    "license": "M12 3v18M5 7h14M5 7l-3 7a4 4 0 0 0 6 0zM19 7l-3 7a4 4 0 0 0 6 0zM8 21h8",
    "docker": "M3 12h17a3 3 0 0 0 2-2 4 4 0 0 1-3-1c0 6-5 10-11 10-3 0-5-2-5-7zM6 9h3v3H6zM9 9h3v3H9zM12 9h3v3h-3zM9 6h3v3H9z",
    "home": "M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z",
    "shield": "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6zM9 12l2 2 4-4",
    "coin": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM15 9.5c-.5-1-1.6-1.5-3-1.5-1.7 0-3 .8-3 2s1.3 1.7 3 2 3 .8 3 2-1.3 2-3 2c-1.4 0-2.5-.5-3-1.5M12 6v2M12 16v2",
    "pr": "M6 3v12M6 15a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM18 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM18 9v3a3 3 0 0 1-3 3H9",
    "book": "M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2zM4 21V5M19 19v2H6",
    "agent": "M9 3h6v4H9zM5 7h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2zM9 12h.01M15 12h.01M9 16h6",
}


def badge(slug: str, label: str, value: str, icon: str, c1: str, c2: str) -> None:
    lw = text_width(label, 10.5)
    vw = text_width(value, 12)
    val_w = vw + 24
    w = math.ceil(38 + lw + 10 + val_w + 3)
    vx = w - 3 - val_w
    uid = slug
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="30" viewBox="0 0 {w} 30" role="img" aria-label="{escape(label.lower())}: {escape(value)}">
<title>{escape(label.lower())}: {escape(value)}</title>
<defs>
<linearGradient id="{uid}-bg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#131D2E"/><stop offset="1" stop-color="#0B1220"/></linearGradient>
<linearGradient id="{uid}-val" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient>
<linearGradient id="{uid}-edge" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{c1}" stop-opacity=".6"/><stop offset=".5" stop-color="{c1}" stop-opacity=".15"/><stop offset="1" stop-color="{c2}" stop-opacity=".55"/></linearGradient>
<linearGradient id="{uid}-shine" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#fff" stop-opacity=".22"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
<clipPath id="{uid}-clip"><rect width="{w}" height="30" rx="15"/></clipPath>
</defs>
<style>{REDUCED}
.sw{{animation:sw 9s 2s cubic-bezier(.4,0,.2,1) infinite}}
@keyframes sw{{0%{{transform:translateX(-80px)}}20%,100%{{transform:translateX({w + 80}px)}}}}
}}</style>
<g clip-path="url(#{uid}-clip)">
<rect width="{w}" height="30" fill="url(#{uid}-bg)"/>
<rect x="{vx:.1f}" y="3" width="{val_w:.1f}" height="24" rx="12" fill="url(#{uid}-val)"/>
<rect class="sw" x="-60" y="0" width="46" height="30" fill="url(#{uid}-shine)" transform="translate(-80 0)"/>
</g>
<rect x=".5" y=".5" width="{w - 1}" height="29" rx="14.5" fill="none" stroke="url(#{uid}-edge)"/>
<g transform="translate(12 6) scale(.75)" fill="none" stroke="{c1}" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><path d="{ICONS[icon]}"/></g>
<text x="38" y="18.8" font-family="{FONT}" font-size="10.5" font-weight="700" fill="#A6B8CC" letter-spacing=".04em">{escape(label)}</text>
<text x="{vx + val_w / 2:.1f}" y="19.4" text-anchor="middle" font-family="{FONT}" font-size="12" font-weight="700" fill="#0B1220">{escape(value)}</text>
</svg>"""
    write(f"badge-{slug}.svg", svg)


# --------------------------------------------------------------------------------------------- tagline


def tagline(t: Theme) -> None:
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="132" viewBox="0 0 1280 132" role="img" aria-labelledby="t d">
<title id="t">Every free token you are entitled to, behind one key.</title>
<desc id="d">OASIS Route finds verified free inference tiers, connects the accounts you own and routes each request to the strongest model with quota left.</desc>
<defs><linearGradient id="g" x1="0" x2="1"><stop offset="0" stop-color="{t.teal}"/><stop offset=".55" stop-color="{t.blue}"/><stop offset="1" stop-color="{t.violet}"/></linearGradient></defs>
<style>{REDUCED}
.l1{{animation:up 1s .1s cubic-bezier(.2,.7,.1,1) both}}.l2{{animation:up 1s .45s cubic-bezier(.2,.7,.1,1) both}}
@keyframes up{{from{{opacity:0;transform:translateY(10px)}}to{{opacity:1;transform:none}}}}
}}</style>
<text class="l1" x="640" y="58" text-anchor="middle" font-family="{FONT}" font-size="40" font-weight="800" fill="url(#g)" letter-spacing="-.01em">Every free token you are entitled to. One key.</text>
<text class="l2" x="640" y="102" text-anchor="middle" font-family="{FONT}" font-size="19" font-weight="500" fill="{t.muted}">Verified free tiers, your own accounts, the strongest model with quota left, and honest failover.</text>
</svg>"""
    write(f"tagline-{t.name}.svg", svg)


# ----------------------------------------------------------------------------------------------- stats


def stats(t: Theme, cells: list[tuple[str, str, str, str]]) -> None:
    n = len(cells)
    gap, pad = 20, 0
    cw = (1280 - pad * 2 - gap * (n - 1)) / n
    parts = []
    for i, (value, label, sub, color) in enumerate(cells):
        x = pad + i * (cw + gap)
        parts.append(
            f"""<g class="c{i}">
<rect x="{x:.1f}" y="1" width="{cw:.1f}" height="150" rx="18" fill="{t.card}" stroke="{t.border}"/>
<rect class="b{i}" x="{x + 24:.1f}" y="124" width="{cw - 48:.1f}" height="4" rx="2" fill="{color}" opacity=".85"/>
<rect x="{x + 24:.1f}" y="124" width="{cw - 48:.1f}" height="4" rx="2" fill="{color}" opacity=".15"/>
<text x="{x + 24:.1f}" y="66" font-family="{FONT}" font-size="44" font-weight="800" fill="{color}" letter-spacing="-.02em">{escape(value)}</text>
<text x="{x + 24:.1f}" y="92" font-family="{FONT}" font-size="15" font-weight="700" fill="{t.text}">{escape(label)}</text>
<text x="{x + 24:.1f}" y="112" font-family="{FONT}" font-size="12.5" font-weight="500" fill="{t.muted}">{escape(sub)}</text>
</g>"""
        )
    anim = "".join(
        f".c{i}{{animation:fade .9s {0.15 + i * 0.18:.2f}s cubic-bezier(.2,.7,.1,1) both}}"
        f".b{i}{{transform-box:fill-box;transform-origin:left;animation:grow 1.6s {0.4 + i * 0.18:.2f}s cubic-bezier(.2,.7,.1,1) both}}"
        for i in range(n)
    )
    desc = "; ".join(f"{v} {lab} ({s})" for v, lab, s, _ in cells)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="152" viewBox="0 0 1280 152" role="img" aria-labelledby="t d">
<title id="t">OASIS Route in numbers</title>
<desc id="d">{escape(desc)}</desc>
<style>{REDUCED}
{anim}
@keyframes fade{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:none}}}}
@keyframes grow{{from{{transform:scaleX(0)}}to{{transform:scaleX(1)}}}}
}}</style>
{"".join(parts)}
</svg>"""
    write(f"stats-{t.name}.svg", svg)


# -------------------------------------------------------------------------------------- capacity chart


def capacity_chart(t: Theme) -> None:
    rows = []
    for label, pools in cap.scenarios():
        letter, _, rest = label.partition(". ")
        rows.append((letter, rest, [cap.scenario(pools, p) for p in cap.PROFILES]))
    short = {
        "A": ("No purchase", "Groq, SambaNova, OpenRouter, Cohere"),
        "B": ("+ Cloudflare Workers AI", "10,000 neurons a day via a custom endpoint"),
        "C": ("+ OpenRouter unlock", "one-time 10 credits lift 50 to 1,000 free requests a day"),
        "D": ("+ NVIDIA ceiling", "40 RPM at 100% use; prototyping terms only"),
    }
    lo, hi = 6.0, 11.0  # log10 axis: 1M .. 100B
    x0, x1 = 400, 1230
    top, row_h = 104, 86

    def xpos(v: float) -> float:
        return x0 + (math.log10(max(v, 10**lo)) - lo) / (hi - lo) * (x1 - x0)

    h = top + row_h * len(rows) + 64
    grid = []
    for e in range(int(lo), int(hi) + 1):
        x = xpos(10**e)
        lab = {6: "1M", 7: "10M", 8: "100M", 9: "1B", 10: "10B", 11: "100B"}[e]
        grid.append(
            f'<line x1="{x:.1f}" y1="{top - 14}" x2="{x:.1f}" y2="{h - 52}" stroke="{t.faint}" stroke-width="1"/>'
            f'<text x="{x:.1f}" y="{h - 30}" text-anchor="middle" font-family="{FONT}" font-size="12" fill="{t.muted}">{lab}</text>'
        )
    omni = 1.62e9
    ox = xpos(omni)
    bars = []
    for i, (letter, _, (chat, agent)) in enumerate(rows):
        y = top + i * row_h
        title, sub = short[letter]
        bars.append(
            f"""<g class="r{i}">
<text x="40" y="{y + 18}" font-family="{FONT}" font-size="16" font-weight="800" fill="{t.text}"><tspan fill="{t.teal}">{letter}</tspan>  {escape(title)}</text>
<text x="40" y="{y + 40}" font-family="{FONT}" font-size="12.5" fill="{t.muted}">{escape(sub)}</text>
<rect class="g{i}a" x="{x0}" y="{y + 2}" width="{xpos(chat) - x0:.1f}" height="20" rx="6" fill="url(#chat)"/>
<text x="{xpos(chat) + 10:.1f}" y="{y + 17}" font-family="{FONT}" font-size="13" font-weight="700" fill="{t.text}">{fmt_tokens(chat)}</text>
<rect class="g{i}b" x="{x0}" y="{y + 28}" width="{xpos(agent) - x0:.1f}" height="20" rx="6" fill="url(#agent)"/>
<text x="{xpos(agent) + 10:.1f}" y="{y + 43}" font-family="{FONT}" font-size="13" font-weight="700" fill="{t.text}">{fmt_tokens(agent)}</text>
</g>"""
        )
    anim = "".join(
        f".g{i}a,.g{i}b{{transform-box:fill-box;transform-origin:left;animation:grow 1.4s {0.2 + i * 0.25:.2f}s cubic-bezier(.2,.7,.1,1) both}}"
        for i in range(len(rows))
    )
    desc = "; ".join(
        f"{short[letter][0]}: {fmt_tokens(c)} chat, {fmt_tokens(a)} coding agent"
        for letter, _, (c, a) in rows
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="{h}" viewBox="0 0 1280 {h}" role="img" aria-labelledby="t d">
<title id="t">Free tokens per month through OASIS Route, one account per provider</title>
<desc id="d">Log-scale bars, limits checked {cap.CHECKED}. {escape(desc)}. OmniRoute's claimed total of 1.62B is marked for reference.</desc>
<defs>
<linearGradient id="chat" x1="0" x2="1"><stop offset="0" stop-color="{t.teal}" stop-opacity=".55"/><stop offset="1" stop-color="{t.teal}"/></linearGradient>
<linearGradient id="agent" x1="0" x2="1"><stop offset="0" stop-color="{t.violet}" stop-opacity=".55"/><stop offset="1" stop-color="{t.violet}"/></linearGradient>
</defs>
<style>{REDUCED}
{anim}
@keyframes grow{{from{{transform:scaleX(0)}}to{{transform:scaleX(1)}}}}
}}</style>
<rect x=".5" y=".5" width="1279" height="{h - 1}" rx="22" fill="{t.card}" stroke="{t.border}"/>
<text x="40" y="46" font-family="{FONT}" font-size="20" font-weight="800" fill="{t.text}">Free tokens per month, one account per provider</text>
<text x="40" y="70" font-family="{FONT}" font-size="13" fill="{t.muted}">Published limits checked {cap.CHECKED} · log scale · binding limit applied per request size</text>
<g font-family="{FONT}" font-size="13" font-weight="600" fill="{t.text}">
<rect x="880" y="36" width="14" height="14" rx="4" fill="{t.teal}"/><text x="902" y="48">Chat, 1,500 tok/request</text>
<rect x="1060" y="36" width="14" height="14" rx="4" fill="{t.violet}"/><text x="1082" y="48">Agent, 20,000 tok/request</text>
</g>
{"".join(grid)}
<line x1="{ox:.1f}" y1="{top - 14}" x2="{ox:.1f}" y2="{h - 52}" stroke="{t.amber}" stroke-width="1.5" stroke-dasharray="5 5"/>
<text x="{ox - 8:.1f}" y="{top - 22}" text-anchor="end" font-family="{FONT}" font-size="12" font-weight="700" fill="{t.amber}">OmniRoute claim, 1.62B</text>
{"".join(bars)}
</svg>"""
    write(f"capacity-{t.name}.svg", svg)


# ------------------------------------------------------------------------------------------ quickstart


def quickstart(t: Theme) -> None:
    steps = [
        (
            "01",
            "Run it",
            "uvx --from git+…/OASIS-ROUTE oasis-route dev",
            "Prints the console URL and an Oasis Key.",
        ),
        (
            "02",
            "Paste your keys",
            "oasis-route connect --from-env",
            "Each key is recognised, checked, synced.",
        ),
        (
            "03",
            "Point any SDK at it",
            'base_url=…/v1   model="auto"',
            "Strongest free model first, honest failover.",
        ),
    ]
    gap = 24
    cw = (1280 - gap * 2) / 3
    parts = []
    colors = [t.teal, t.blue, t.violet]
    for i, (num, title, cmd, sub) in enumerate(steps):
        x = i * (cw + gap)
        c = colors[i]
        arrow = ""
        if i < 2:
            ax = x + cw + 2
            arrow = f'<path d="M{ax:.1f} 84h{gap - 4}" stroke="{t.border}" stroke-width="2"/><path d="M{ax + gap - 10:.1f} 79l6 5-6 5" fill="none" stroke="{t.border}" stroke-width="2"/>'
        parts.append(
            f"""<g class="s{i}">
<rect x="{x + 1:.1f}" y="1" width="{cw - 2:.1f}" height="166" rx="18" fill="{t.card}" stroke="{t.border}"/>
<text x="{x + 26:.1f}" y="50" font-family="{FONT}" font-size="30" font-weight="800" fill="{c}">{num}</text>
<text x="{x + 76:.1f}" y="47" font-family="{FONT}" font-size="18" font-weight="800" fill="{t.text}">{escape(title)}</text>
<rect x="{x + 22:.1f}" y="70" width="{cw - 44:.1f}" height="40" rx="10" fill="{t.bg}" stroke="{t.border}"/>
<text x="{x + 36:.1f}" y="95" font-family="{MONO}" font-size="13" fill="{t.text}"><tspan fill="{c}">$ </tspan>{escape(cmd)}</text>
<text x="{x + 26:.1f}" y="142" font-family="{FONT}" font-size="13.5" fill="{t.muted}">{escape(sub)}</text>
</g>{arrow}"""
        )
    anim = "".join(
        f".s{i}{{animation:fade .9s {0.15 + i * 0.3:.2f}s cubic-bezier(.2,.7,.1,1) both}}" for i in range(3)
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="168" viewBox="0 0 1280 168" role="img" aria-labelledby="t d">
<title id="t">Quick start in three steps</title>
<desc id="d">01 run oasis-route dev with uvx; 02 connect your provider keys with oasis-route connect --from-env; 03 point any OpenAI or Anthropic SDK at the gateway with model auto.</desc>
<style>{REDUCED}
{anim}
@keyframes fade{{from{{opacity:0;transform:translateY(8px)}}to{{opacity:1;transform:none}}}}
}}</style>
{"".join(parts)}
</svg>"""
    write(f"quickstart-{t.name}.svg", svg)


# ------------------------------------------------------------------------------------- icons, dividers

FEATURE_ICONS = {
    "discover": ("M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.3-4.3M8 11h6M11 8v6", TEAL),
    "route": (
        "M5 19a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM19 9a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM19 21a2 2 0 1 0 0-4 2 2 0 0 0 0 4zM7 17h4a4 4 0 0 0 4-4V9a2 2 0 0 1 2-2M11 17h6",
        BLUE,
    ),
    "quota": ("M4 20V10M10 20V4M16 20v-7M22 20H2", VIOLET),
    "failover": ("M3 12a9 9 0 0 1 15.5-6.2L21 8M21 3v5h-5M21 12a9 9 0 0 1-15.5 6.2L3 16M3 21v-5h5", AMBER),
    "vault": ("M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4M12 15v2", TEAL),
    "free": ("M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM5.6 5.6l12.8 12.8", BLUE),
}


def feature_icon(name: str, t: Theme) -> None:
    path, accent = FEATURE_ICONS[name]
    color = {TEAL: t.teal, BLUE: t.blue, VIOLET: t.violet, AMBER: t.amber}[accent]
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48" aria-hidden="true">
<rect x=".5" y=".5" width="47" height="47" rx="14" fill="{color}" fill-opacity=".12" stroke="{color}" stroke-opacity=".45"/>
<g transform="translate(12 12)" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="{path}"/></g>
</svg>"""
    write(f"icon-{name}-{t.name}.svg", svg)


def divider(t: Theme) -> None:
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="24" viewBox="0 0 1280 24" aria-hidden="true">
<defs><linearGradient id="s" x1="0" x2="1"><stop offset="0" stop-color="{t.teal}" stop-opacity="0"/><stop offset=".5" stop-color="{t.teal}"/><stop offset="1" stop-color="{t.violet}" stop-opacity="0"/></linearGradient></defs>
<style>{REDUCED}
.sweep{{animation:sweep 6s ease-in-out infinite}}@keyframes sweep{{from{{transform:translateX(-320px)}}to{{transform:translateX(1280px)}}}}
}}</style>
<path d="M0 12H1280" stroke="{t.faint}" stroke-width="1.5"/>
<rect class="sweep" x="0" y="11" width="320" height="2" fill="url(#s)" transform="translate(-320 0)"/>
<g transform="translate(628 0)"><rect width="24" height="24" rx="8" fill="{t.bg}" stroke="{t.teal}" stroke-opacity=".7"/><circle cx="12" cy="12" r="6.5" fill="none" stroke="{t.teal}" stroke-width="1.8"/><circle cx="12" cy="12" r="2.2" fill="{t.teal}"/></g>
</svg>"""
    write(f"divider-{t.name}.svg", svg)


def footer() -> None:
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="120" viewBox="0 0 1280 120" role="img" aria-label="OASIS Route. One gateway. One Oasis Key. The strongest eligible route.">
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0B1220"/><stop offset="1" stop-color="#111B2B"/></linearGradient>
<linearGradient id="g" x1="0" x2="1"><stop offset="0" stop-color="{TEAL}"/><stop offset=".55" stop-color="{BLUE}"/><stop offset="1" stop-color="{VIOLET}"/></linearGradient>
</defs>
<style>{REDUCED}
.p{{stroke-dasharray:8 10;animation:flow 2.4s linear infinite}}@keyframes flow{{to{{stroke-dashoffset:-36}}}}
}}</style>
<rect width="1280" height="120" rx="22" fill="url(#bg)"/>
<path class="p" d="M60 96C220 96 330 40 580 40" fill="none" stroke="url(#g)" stroke-opacity=".5" stroke-width="2"/>
<path class="p" d="M1220 96C1060 96 950 40 830 40" fill="none" stroke="url(#g)" stroke-opacity=".5" stroke-width="2"/>
<g transform="translate(604 22)"><rect width="36" height="36" rx="11" fill="#102831" stroke="{TEAL}" stroke-opacity=".75"/><circle cx="18" cy="18" r="9.5" fill="none" stroke="{TEAL}" stroke-width="2"/><circle cx="18" cy="18" r="3.2" fill="{TEAL}"/></g>
<text x="650" y="47" font-family="{FONT}" font-size="20" font-weight="800" fill="#E8F1FC">OASIS Route</text>
<text x="640" y="88" text-anchor="middle" font-family="{FONT}" font-size="14" font-weight="600" fill="#93A8C0">One gateway · One Oasis Key · The strongest eligible route</text>
</svg>"""
    write("footer.svg", svg)


def social_svg(floor_chat: float, agent: float, tests: int) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="640" viewBox="0 0 1280 640">
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#0B1220"/><stop offset=".6" stop-color="#0A101A"/><stop offset="1" stop-color="#151B33"/></linearGradient>
<linearGradient id="g" x1="0" x2="1"><stop offset="0" stop-color="{TEAL}"/><stop offset=".55" stop-color="{BLUE}"/><stop offset="1" stop-color="{VIOLET}"/></linearGradient>
<radialGradient id="halo"><stop offset="0" stop-color="{TEAL}" stop-opacity=".22"/><stop offset="1" stop-color="{TEAL}" stop-opacity="0"/></radialGradient>
<pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0H0V40" fill="none" stroke="#91A8C5" stroke-opacity=".07"/></pattern>
</defs>
<rect width="1280" height="640" fill="url(#bg)"/><rect width="1280" height="640" fill="url(#grid)"/>
<circle cx="1010" cy="320" r="330" fill="url(#halo)"/>
<g transform="translate(80 86)"><rect width="64" height="64" rx="20" fill="#102831" stroke="{TEAL}" stroke-opacity=".75" stroke-width="1.5"/><circle cx="32" cy="32" r="17" fill="none" stroke="{TEAL}" stroke-width="3"/><circle cx="32" cy="32" r="5.6" fill="{TEAL}"/></g>
<text x="164" y="132" font-family="{FONT}" font-size="44" font-weight="800" fill="#F5F8FF">OASIS Route</text>
<text x="80" y="250" font-family="{FONT}" font-size="58" font-weight="800" fill="url(#g)" letter-spacing="-.02em">Every free token you are</text>
<text x="80" y="320" font-family="{FONT}" font-size="58" font-weight="800" fill="url(#g)" letter-spacing="-.02em">entitled to. One key.</text>
<text x="80" y="380" font-family="{FONT}" font-size="24" font-weight="500" fill="#A9BBD0">Open-source LLM gateway: verified free tiers, quota-aware routing,</text>
<text x="80" y="414" font-family="{FONT}" font-size="24" font-weight="500" fill="#A9BBD0">honest failover. OpenAI and Anthropic compatible.</text>
<g font-family="{FONT}" font-weight="800">
<rect x="80" y="468" width="240" height="96" rx="18" fill="#111B2B" stroke="#22344A"/>
<text x="104" y="516" font-size="38" fill="{TEAL}">~{fmt_tokens(floor_chat)}</text><text x="104" y="546" font-size="15" font-weight="600" fill="#93A8C0">free tokens / month</text>
<rect x="340" y="468" width="240" height="96" rx="18" fill="#111B2B" stroke="#22344A"/>
<text x="364" y="516" font-size="38" fill="{VIOLET}">~{fmt_tokens(agent)}</text><text x="364" y="546" font-size="15" font-weight="600" fill="#93A8C0">agent tokens / month*</text>
<rect x="600" y="468" width="240" height="96" rx="18" fill="#111B2B" stroke="#22344A"/>
<text x="624" y="516" font-size="38" fill="{BLUE}">{tests}</text><text x="624" y="546" font-size="15" font-weight="600" fill="#93A8C0">tests · SQLite + Postgres</text>
</g>
<text x="80" y="600" font-family="{FONT}" font-size="15" fill="#6F86A0">* after OpenRouter's one-time 10-credit unlock · github.com/FAHTHIZ/OASIS-ROUTE</text>
<g transform="translate(930 170)" fill="none" stroke-linecap="round">
<path d="M0 150C80 150 90 60 170 60" stroke="{TEAL}" stroke-width="3" stroke-opacity=".8"/>
<path d="M0 150H170" stroke="{BLUE}" stroke-width="3" stroke-opacity=".8"/>
<path d="M0 150C80 150 90 240 170 240" stroke="{VIOLET}" stroke-width="3" stroke-opacity=".8"/>
<circle cx="0" cy="150" r="16" fill="#102831" stroke="{TEAL}" stroke-width="3"/>
<circle cx="190" cy="60" r="18" fill="#111B2B" stroke="{TEAL}" stroke-width="2.5"/>
<circle cx="190" cy="150" r="18" fill="#111B2B" stroke="{BLUE}" stroke-width="2.5"/>
<circle cx="190" cy="240" r="18" fill="#111B2B" stroke="{VIOLET}" stroke-width="2.5"/>
</g>
</svg>"""


def main() -> None:
    profiles = cap.PROFILES
    sc = {label.split(".")[0]: pools for label, pools in cap.scenarios()}
    floor_chat = cap.scenario(sc["B"], profiles[0])
    floor_agent = cap.scenario(sc["B"], profiles[1])
    unlocked_agent = cap.scenario(sc["C"], profiles[1])
    tests = count_tests()

    for slug, label, value, icon, c1, c2 in (
        ("python", "PYTHON", "3.12+", "python", BLUE, "#3D7FE0"),
        ("api", "API", "OpenAI · Anthropic", "api", TEAL, "#19B89B"),
        ("free-tokens", "FREE TIER", f"~{fmt_tokens(floor_chat)} tok/mo", "token", TEAL, BLUE),
        ("agent-tokens", "AGENT", f"~{fmt_tokens(unlocked_agent)} tok/mo", "agent", VIOLET, "#8A6CF0"),
        ("providers", "PROVIDERS", "12 + any OpenAI-compatible", "route", BLUE, VIOLET),
        ("tests", "TESTS", f"{tests}", "tests", TEAL, "#19B89B"),
        ("spend", "SPEND", "free-only by default", "coin", AMBER, "#E09A2B"),
        ("license", "LICENSE", "Apache 2.0", "license", VIOLET, "#8A6CF0"),
        ("docker", "DOCKER", "ghcr.io", "docker", BLUE, "#3D7FE0"),
        ("hosted", "SELF-HOSTED", "your machine", "home", TEAL, BLUE),
        ("security", "SECURITY", "policy", "shield", TEAL, "#19B89B"),
        ("docs", "DOCS", "25 guides", "book", BLUE, VIOLET),
        ("prs", "PULL REQUESTS", "welcome", "pr", VIOLET, "#8A6CF0"),
    ):
        badge(slug, label, value, icon, c1, c2)

    for t in (DARK, LIGHT):
        tagline(t)
        stats(
            t,
            [
                (f"~{fmt_tokens(floor_chat)}", "free tokens a month", "chat use, no purchase", t.teal),
                (
                    f"~{fmt_tokens(floor_agent)}",
                    "for coding agents",
                    "20K-token requests, no purchase",
                    t.blue,
                ),
                (
                    f"~{fmt_tokens(unlocked_agent)}",
                    "agent tokens a month",
                    "after a one-time 10-credit unlock",
                    t.violet,
                ),
                (str(tests), "tests", "SQLite and PostgreSQL in CI", t.amber),
            ],
        )
        capacity_chart(t)
        quickstart(t)
        divider(t)
        for name in FEATURE_ICONS:
            feature_icon(name, t)
    footer()
    social = social_svg(floor_chat, unlocked_agent, tests)
    write("social-preview.svg", social)
    if "--png" in sys.argv:
        import cairosvg  # type: ignore[import-not-found]

        cairosvg.svg2png(bytestring=social.encode(), write_to=str(ROOT / "assets" / "social-preview.png"))
    print(f"wrote {len(list(OUT.glob('*.svg')))} files to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
