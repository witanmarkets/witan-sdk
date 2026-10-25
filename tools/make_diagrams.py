"""Generate the SDK documentation diagrams as SVG (sdk/python/docs/diagrams/*.svg).

Styled with the site's tokens (web/src/index.css), one dark card per diagram so they read on white
(PyPI, npm) and dark (GitHub) pages alike. PNG copies for READMEs: scripts/diagrams-render.sh.

    python3 sdk/python/tools/make_diagrams.py
"""
from __future__ import annotations

import math
import random
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent.parent / "docs" / "diagrams"
# the same files, where the api reads them for /about and /developers/docs (api/src/figures.ts)
OUT_API = Path(__file__).resolve().parents[3] / "api" / "src" / "assets" / "diagrams"


# The site's tokens (web/src/index.css): near-black with a violet-to-cyan tint, glass panels with
# hairline borders, violet glow on what matters, amber for money, green for "live / verified".
BG, PANEL, LINE = "#07080C", "#0E0F16", "#272830"
INK, SOFT, DIM = "#ECEEF2", "#9BA1AD", "#5D6370"
VIOLET, ROSE, AMBER, GREEN = "#7C5CFF", "#FF5C87", "#FFB35C", "#58C48A"
CYAN = "#5EE0FF"   # the brand bars' second stop (--bar): violet-to-cyan for whatever measures or leads the eye
FONT = "'Wanted Sans Variable', 'Wanted Sans', 'Segoe UI', 'Helvetica Neue', Arial, sans-serif"
MONO = "'DM Mono', 'SFMono-Regular', Consolas, monospace"
HEAD = 40  # the brand row pushes every body down by this much


def _marker(mid: str, color: str) -> str:
    return (f'<marker id="{mid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="{color}"/></marker>')


def _defs() -> str:
    glow = ('<filter id="{id}" x="-50%" y="-50%" width="200%" height="200%">'
            '<feDropShadow dx="0" dy="0" stdDeviation="{sd}" flood-color="{c}" flood-opacity="{o}"/></filter>')
    return ('<defs>' + _marker("arrow", SOFT) + _marker("arrow-v", VIOLET) + _marker("arrow-a", AMBER)
            + '<linearGradient id="aurora" x1="12" y1="2" x2="21" y2="10" gradientUnits="userSpaceOnUse">'
            '<stop offset="0" stop-color="#7C5CFF"/><stop offset=".6" stop-color="#FF5C87"/>'
            '<stop offset="1" stop-color="#FFB35C"/></linearGradient>'
            '<linearGradient id="aurora-h" x1="0" y1="0" x2="1" y2="0">'
            '<stop offset="0" stop-color="#7C5CFF"/><stop offset="1" stop-color="#5EE0FF"/></linearGradient>'
            '<linearGradient id="aurora-v" x1="0" y1="1" x2="0" y2="0">'
            '<stop offset="0" stop-color="#7C5CFF"/><stop offset="1" stop-color="#5EE0FF"/></linearGradient>'
            '<linearGradient id="tint" x1=".3" y1="0" x2=".7" y2="1">'
            '<stop offset="0" stop-color="#7C5CFF" stop-opacity=".2"/><stop offset=".46" stop-color="#07080C" stop-opacity="0"/>'
            '<stop offset="1" stop-color="#5EE0FF" stop-opacity=".07"/></linearGradient>'
            + glow.format(id="glow-v", sd=7, c=VIOLET, o=.55)
            + glow.format(id="glow-w", sd=2.5, c=INK, o=.45)
            + glow.format(id="glow-g", sd=4, c=GREEN, o=.8)
            + glow.format(id="glow-a", sd=4, c=AMBER, o=.45)
            + '<filter id="blur-mark" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="1.2"/></filter>'
            + '</defs>')


def svg(w: int, h: int, title: str, subtitle: str, body: list[str], label: str) -> str:
    h += HEAD
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'role="img" aria-label="{escape(label)}" font-family="{FONT}" text-rendering="geometricPrecision">\n'
            + _defs() + "\n"
            f'<rect width="{w}" height="{h}" rx="18" fill="{BG}"/>\n'
            f'<rect width="{w}" height="{h}" rx="18" fill="url(#tint)"/>\n'
            f'<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="17.5" fill="none" stroke="{INK}" stroke-opacity=".12"/>\n'
            # brand row, as on the site's nav
            + mark(44, 36, 0.95, glow=True)
            + f'<text x="62" y="41.5" font-size="15.5" font-weight="700" letter-spacing="-0.3" fill="{INK}">WITAN</text>\n'
            f'<text x="32" y="{46 + HEAD}" font-size="25" font-weight="800" letter-spacing="-0.8" fill="{INK}">{escape(title)}</text>\n'
            f'<text x="32" y="{71 + HEAD}" font-size="14" fill="{SOFT}">{escape(subtitle)}</text>\n'
            f'<g transform="translate(0 {HEAD})">\n' + "\n".join(body) + "\n</g>\n</svg>\n")


def mark(cx: float, cy: float, scale: float, *, glow: bool = False) -> str:
    """The WITAN crystal; with glow, a blurred copy sits behind it as on the site."""
    shape = ('<polygon points="3,7 12,2 12,10" fill="#F2F3F7"/><polygon points="12,2 21,7 12,10" fill="url(#aurora)"/>'
             '<polygon points="21,7 21,17 12,22 12,10" fill="#B9BEC9"/><polygon points="3,7 12,10 12,22 3,17" fill="#5E6470"/>')
    t = f'translate({cx - 12 * scale} {cy - 12 * scale}) scale({scale})'
    halo = (f'<g transform="{t}" filter="url(#blur-mark)" opacity=".6">{shape}</g>' if glow else "")
    return halo + f'<g transform="{t}">{shape}</g>'


def text(x: float, y: float, s: str, size: float = 12.5, fill: str = SOFT, weight: int = 400,
         anchor: str = "start", mono: bool = False) -> str:
    fam = f' font-family="{MONO}"' if mono else ""
    return (f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
            f'text-anchor="{anchor}" xml:space="preserve"{fam}>{escape(s)}</text>')


def panel(x: float, y: float, w: float, h: float, *, accent: bool = False, rx: float = 14,
          dashed: bool = False, fill: str = PANEL) -> str:
    """A glass panel: hairline border; the one that matters glows violet."""
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    if accent:
        return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{VIOLET}" '
                f'stroke-width="1.5" filter="url(#glow-v)"{dash}/>')
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" fill-opacity=".86" '
            f'stroke="{INK}" stroke-opacity=".16"{dash}/>')


def box(x: float, y: float, w: float, h: float, lines: list[str], *, accent: str | None = None,
        dashed: bool = False, fill: str = PANEL, mono_rest: bool = False) -> str:
    if accent and accent != VIOLET:  # red/amber outcomes keep their colour, without the violet glow
        out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" stroke="{accent}" stroke-width="1.5"/>']
    else:
        out = [panel(x, y, w, h, accent=bool(accent), rx=12, dashed=dashed, fill=fill)]
    if lines:
        out.append(text(x + 16, y + 25, lines[0], 14.5, INK, 600))
        for i, line in enumerate(lines[1:]):
            out.append(text(x + 16, y + 45 + 18 * i, line, 12.5, SOFT, mono=mono_rest))
    return "\n".join(out)


def arrow(x1: float, y1: float, x2: float, y2: float, label: str = "", *, violet: bool = False,
          dashed: bool = False, lx: float | None = None, ly: float | None = None) -> str:
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    if violet:
        style = f'stroke="{VIOLET}" stroke-width="1.8" marker-end="url(#arrow-v)"'  # no glow: a filter on a flat line has an empty box
    else:
        style = f'stroke="{SOFT}" stroke-width="1.4" marker-end="url(#arrow)"'
    out = [f'<path d="M{x1} {y1}L{x2} {y2}" fill="none"{dash} {style}/>']
    if label:
        out.append(text(lx if lx is not None else (x1 + x2) / 2, ly if ly is not None else min(y1, y2) - 8,
                        label, 11.5, DIM, anchor="middle"))
    return "\n".join(out)


def header(x: float, y: float, s: str) -> str:
    """The site's eyebrow: small capitals, tracked out, dim."""
    return (f'<text x="{x}" y="{y}" font-size="11" font-weight="600" letter-spacing="0.7" fill="{DIM}" '
            f'xml:space="preserve">{escape(s.upper())}</text>')


def overview() -> str:
    b = [header(32, 106, "Agents"), header(324, 106, "WITAN origin"), header(708, 106, "Copies")]
    b += [box(32, 118, 220, 64, ["Claude Code · Cursor", "MCP plugin, dataset tools"]),
          box(32, 196, 220, 64, ["Python SDK · wtn", "pull, query, contribute, pay"]),
          box(32, 274, 220, 64, ["JS SDK", "state for serverless and edge"])]
    b.append(panel(324, 118, 312, 220, accent=True))
    rows = [("Validation", "schema · PII · duplicates · model review"),
            ("Market", "knowledge units · versioned datasets"),
            ("Payments", "x402 USDC · credits · seller payouts"),
            ("Signing", "Ed25519 over every version manifest")]
    for i, (t, s) in enumerate(rows):
        y = 132 + 50 * i
        b.append(f'<rect x="338" y="{y}" width="284" height="42" rx="8" fill="{BG}" stroke="{LINE}"/>')
        b.append(text(352, y + 17, t, 13, INK, 600))
        b.append(text(352, y + 33, s, 11.5, SOFT))
    b += [box(708, 118, 220, 64, ["Object store", "content-addressed Parquet parts"]),
          box(708, 196, 220, 64, ["Local node", "wtn serve · witan-node image"], accent=VIOLET),
          box(708, 274, 220, 64, ["Mirror of a node", "--upstream, signatures intact"])]
    b += [arrow(252, 150, 322, 150, "API · MCP", lx=288, ly=142),
          arrow(252, 228, 322, 228), arrow(252, 306, 322, 306),
          arrow(636, 150, 706, 150, "parts", lx=672, ly=142),
          arrow(636, 228, 706, 228, "follow", violet=True, lx=672, ly=220),
          arrow(818, 260, 818, 272)]
    b += [text(32, 378, "Agents read from the origin or from any node: the same API, SQL and MCP. A copy is trusted"),
          text(32, 398, "because its manifest carries the origin's signature, not because of where it came from.")]
    return svg(960, 424, "How WITAN works",
               "Agents exchange what they measured; every dataset version is signed where it is made.",
               b, "How WITAN works: agents, the origin, and signed copies")


def dataset_model() -> str:
    colors = {"a": "#4E5566", "b": "#5B6275", "c": "#6F5CD6", "d": "#8B6BE8", "e": CYAN}
    b = [header(32, 106, "Versions (immutable manifests)")]
    versions = [("v1", "ab"), ("v2", "abcd"), ("v3", "abcde")]
    for i, (v, parts) in enumerate(versions):
        x = 32 + 300 * i
        b.append(box(x, 118, 272, 92, [f"{v} manifest", "signed by the origin"], accent=VIOLET if v == "v3" else None))
        for j, p in enumerate(parts):
            b.append(f'<rect x="{x + 16 + 44 * j}" y="{118 + 58}" width="36" height="22" rx="5" fill="{colors[p]}"/>')
            b.append(text(x + 34 + 44 * j, 118 + 72, p.upper(), 12, INK, 600, "middle", mono=True))
    b.append(header(32, 262, "Object store (content-addressed Parquet parts, shared across versions)"))
    for j, p in enumerate("abcde"):
        x = 32 + 180 * j
        b.append(f'<rect x="{x}" y="274" width="164" height="56" rx="12" fill="{PANEL}" stroke="{colors[p]}" stroke-width="1.5"/>')
        b.append(text(x + 16, 298, f"part {p.upper()}", 14, INK, 600))
        b.append(text(x + 16, 317, f"<sha256-{p.upper()}>.parquet", 11.5, SOFT, mono=True))
    b.append(arrow(760, 210, 800, 272, "", violet=True))
    b.append(text(812, 246, "pull v3 with v2 on disk:", 11.5, SOFT))
    b.append(text(812, 262, "only part E transfers", 11.5, INK, 600))
    b += [text(32, 368, "A contribution becomes new parts plus a new manifest. Old versions never change, so a pinned"),
          text(32, 388, "version answers the same query forever, and every part is checked against its SHA-256 on the way in.")]
    return svg(960, 414, "A dataset version is a signed list of parts",
               "Like image layers: parts are content-addressed Parquet files that versions share.",
               b, "Dataset versions are signed manifests of shared Parquet parts")


def trust_chain() -> str:
    b = [box(32, 118, 196, 96, ["Origin", "signs each manifest", "key k2, endorsed by k1"], accent=VIOLET),
         box(312, 118, 196, 96, ["Node or mirror", "stores, serves copies", "signature unchanged"]),
         box(592, 118, 196, 96, ["Your client", "pinned k1 (trust add)", "follows k1 → k2"])]
    b.append(panel(848, 118, 80, 42, accent=True, rx=10))
    b.append(text(888, 144, "verified", 13, INK, 600, "middle"))
    b.append(f'<rect x="848" y="172" width="80" height="42" rx="10" fill="{PANEL}" stroke="{ROSE}" stroke-width="1.5"/>')
    b.append(text(888, 190, "Signature", 12, INK, 600, "middle"))
    b.append(text(888, 205, "Error", 12, INK, 600, "middle"))
    b += [arrow(228, 166, 310, 166, "signed", lx=269, ly=156),
          arrow(508, 166, 590, 166, "unchanged", lx=549, ly=156),
          arrow(788, 150, 846, 139, violet=True), arrow(788, 182, 846, 193)]
    b += [text(32, 262, "Key rotation: the old key endorses the new one, and the endorsement travels inside every signature,"),
          text(32, 282, "so clients pinned to k1 keep verifying after the origin moves to k2. A revoked key stops counting at once."),
          text(32, 310, "Python: verify=True or WITAN_VERIFY=1 · JS: manifest(slug, { verify: keys }) · node: wtn serve --verify", 12, DIM, mono=True)]
    return svg(960, 336, "Signatures travel with the data",
               "Pin the origin's keys once; then check a copy from anywhere, however many hops it took.",
               b, "Origin signatures pass through nodes and mirrors and are verified by clients")


def node_topology() -> str:
    b = [box(32, 138, 200, 84, ["Your agent or app", "SDK or MCP client", "Bearer <token>"])]
    b.append(f'<rect x="306" y="106" width="324" height="206" rx="16" fill="none" stroke="{INK}" stroke-opacity=".3" stroke-dasharray="6 5"/>')
    b.append(text(322, 128, "witan-node container · uid 10001 · read-only root", 11.5, DIM, 600))
    b.append(box(322, 142, 292, 64, ["wtn serve :8686", "API · SQL (DuckDB) · MCP /mcp"], accent=VIOLET))
    b.append(box(322, 224, 292, 72, ["/data volume", "witan-data/  trust.json"], mono_rest=True))
    b += [box(712, 118, 216, 84, ["WITAN origin", "pull the latest version", "--follow SLUG --verify"]),
          box(712, 226, 216, 84, ["Another node", "--upstream mirror", "signatures still checked"], dashed=True)]
    b += [arrow(232, 176, 320, 176, "HTTP · MCP", lx=269, ly=168),
          arrow(614, 164, 710, 158, "follow", violet=True, lx=668, ly=150),
          arrow(614, 190, 710, 256, "or", dashed=True, lx=676, ly=214),
          arrow(468, 206, 468, 222)]
    b += [text(32, 350, "docker run -d -p 127.0.0.1:8686:8686 -e WITAN_NODE_TOKEN=... -v witan-data:/data \\", 12, SOFT, mono=True),
          text(32, 370, "  ghcr.io/witanmarkets/witan-node --follow agent-api-observatory --verify", 12, SOFT, mono=True)]
    return svg(960, 396, "witan-node in a container",
               "The origin's dataset API, SQL and MCP, served from a volume; kept current and verified.",
               b, "witan-node container topology: agent, node, volume, origin and mirrors")


# ---- story diagrams: few words, big shapes ----
def robot(cx: float, cy: float, color: str, size: float = 1.0) -> str:
    """An agent: a rounded head, two eyes, an antenna."""
    w, h = 40 * size, 32 * size
    x, y = cx - w / 2, cy - h / 2 + 4 * size
    return (f'<line x1="{cx}" y1="{y}" x2="{cx}" y2="{y - 9 * size}" stroke="{color}" stroke-width="{1.8 * size}"/>'
            f'<circle cx="{cx}" cy="{y - 11 * size}" r="{2.8 * size}" fill="{color}"/>'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{9 * size}" fill="{color}" fill-opacity="0.1" '
            f'stroke="{color}" stroke-width="{1.8 * size}"/>'
            f'<circle cx="{cx - 8 * size}" cy="{y + h * 0.48}" r="{3.4 * size}" fill="{INK}"/>'
            f'<circle cx="{cx + 8 * size}" cy="{y + h * 0.48}" r="{3.4 * size}" fill="{INK}"/>')


def unit_card(cx: float, cy: float) -> str:
    """What is traded: a small glass card with a chart on it."""
    x, y = cx - 22, cy - 16
    bars = "".join(f'<rect x="{x + 9 + 9 * i}" y="{y + 24 - h}" width="5" height="{h}" rx="1.5" fill="url(#aurora-v)"/>'
                   for i, h in enumerate((8, 13, 18)))
    return (f'<rect x="{x}" y="{y}" width="44" height="32" rx="7" fill="{PANEL}" stroke="{INK}" stroke-opacity=".4" '
            f'stroke-width="1.2"/>' + bars)


def coin(cx: float, cy: float, r: float = 17) -> str:
    return (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{AMBER}" fill-opacity="0.16" stroke="{AMBER}" stroke-width="1.8" '
            f'filter="url(#glow-a)"/>' + text(cx, cy + r * 0.36, "$", r * 1.05, AMBER, 800, "middle"))


def badge_check(cx: float, cy: float) -> str:
    return (f'<circle cx="{cx}" cy="{cy}" r="14" fill="{BG}" stroke="{GREEN}" stroke-width="1.8" filter="url(#glow-g)"/>'
            f'<path d="M{cx - 6} {cy}l4 4.5l8 -9" stroke="{GREEN}" stroke-width="2.4" fill="none" '
            'stroke-linecap="round" stroke-linejoin="round"/>')


def node(cx: float, cy: float, r: float, *, accent: bool = False) -> str:
    if accent:
        return (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{PANEL}" stroke="{VIOLET}" stroke-width="1.8" '
                f'filter="url(#glow-v)"/>')
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{PANEL}" stroke="{INK}" stroke-opacity=".2" stroke-width="1.2"/>'


def how_it_works() -> str:
    y = 206
    b = []
    # the three actors
    b.append(node(150, y, 62))
    b.append(robot(150, y, INK, 1.35))
    b.append(node(480, y, 74, accent=True))
    b.append(mark(480, y + 2, 3.9, glow=True))
    b.append(badge_check(534, y - 52))
    b.append(node(810, y, 62))
    b.append(robot(810, y, INK, 1.35))
    # the result travels right, on the site's white arcs
    for x1, x2, cx, label in ((214, 404, 309, "submits"), (556, 746, 651, "delivers")):
        b.append(f'<path d="M{x1} {y}Q{cx} {y - 30} {x2} {y}" stroke="{INK}" stroke-opacity=".8" stroke-width="1.8" '
                 f'fill="none" filter="url(#glow-w)" marker-end="url(#arrow)"/>')
        b.append(unit_card(cx, y - 15))
        b.append(text(cx, y - 44, label, 12.5, SOFT, 600, "middle"))
    # who does what — three words each
    for cx, name, what in ((150, "Agent A", "measures once"), (480, "WITAN", "screens & scores"), (810, "Agent B", "reads it for $0.01")):
        b.append(text(cx, y + 104, name, 17, INK, 700, "middle"))
        b.append(text(cx, y + 126, what, 14, SOFT, 500, "middle"))
    # the money travels back
    b.append(f'<path d="M810 {y + 146}V{y + 172}H150V{y + 150}" stroke="{AMBER}" stroke-width="2" fill="none" '
             f'stroke-dasharray="7 5" marker-end="url(#arrow-a)"/>')
    b.append(f'<rect x="338" y="{y + 154}" width="284" height="36" rx="18" fill="{BG}"/>')
    b.append(coin(362, y + 172))
    b.append(text(388, y + 178, "every sale pays Agent A", 15, AMBER, 700))
    return svg(960, 420, "Agents trade what they measured",
               "Measured once, screened and scored by WITAN, bought by every agent that needs it.",
               b, "How WITAN works: agent A measures, WITAN screens and scores it, agent B buys it; the sale pays A")


def why_witan() -> str:
    b = []
    base = 348  # bars stand on this line; height = the work
    b.append(panel(24, 96, 440, 296, rx=16))
    b.append(panel(496, 96, 440, 296, rx=16, accent=True))
    b.append(header(48, 126, "Without WITAN"))
    b.append(f'<text x="520" y="126" font-size="11" font-weight="700" letter-spacing="0.7" fill="#B7A6FF">WITH WITAN</text>')
    b.append(text(48, 186, "4×", 52, DIM, 800))
    b.append(text(140, 164, "the same work,", 15, SOFT, 600))
    b.append(text(140, 184, "done four times", 15, SOFT, 600))
    b.append(text(520, 186, "1×", 52, "url(#aurora-h)", 800))
    b.append(text(612, 164, "measured once,", 15, INK, 600))
    b.append(text(612, 184, "bought for $0.01", 15, INK, 600))
    for i in range(4):
        cx = 92 + 96 * i
        b.append(robot(cx, 222, DIM, 0.8))
        b.append(f'<rect x="{cx - 22}" y="{base - 96}" width="44" height="96" rx="8" fill="{INK}" fill-opacity=".07" '
                 f'stroke="{INK}" stroke-opacity=".12"/>')
        cx2 = 564 + 96 * i
        if i == 0:
            b.append(robot(cx2, 222, INK, 0.8))
            b.append(f'<rect x="{cx2 - 22}" y="{base - 96}" width="44" height="96" rx="8" fill="url(#aurora-v)" '
                     f'filter="url(#glow-v)"/>')
        else:
            b.append(robot(cx2, 222, AMBER, 0.8))
            b.append(f'<rect x="{cx2 - 22}" y="{base - 12}" width="44" height="12" rx="4" fill="{AMBER}" fill-opacity=".5"/>')
            b.append(coin(cx2, base - 34, 14))
    b.append(f'<line x1="48" y1="{base}" x2="440" y2="{base}" stroke="{INK}" stroke-opacity=".14"/>')
    b.append(f'<line x1="520" y1="{base}" x2="912" y2="{base}" stroke="{INK}" stroke-opacity=".14"/>')
    b.append(text(244, base + 28, "every agent pays the full cost", 13, DIM, 600, "middle"))
    b.append(text(716, base + 28, "and Agent A is paid on every sale", 13, AMBER, 700, "middle"))
    return svg(960, 416, "Measured once, reused by every agent",
               "The bar is the work: without a market everyone repeats it; with WITAN it is done once and paid for.",
               b, "Why WITAN: without it four agents repeat the same work; with it one measures and three buy for $0.01")


def x402_flow() -> str:
    """The x402 purchase, as a sequence: one request, one 402, one signed transfer, the goods."""
    ax, wx, bx = 132, 480, 828   # the lanes: buyer agent, WITAN, the chain
    b = [box(32, 96, 200, 54, ["Buyer agent", "a wallet with USDC, no account"]),
         box(380, 96, 200, 54, ["WITAN", "the paid endpoint, x402"], accent=VIOLET),
         box(728, 96, 200, 54, ["Base", "USDC settles on-chain"])]
    for x in (ax, wx, bx):
        b.append(f'<line x1="{x}" y1="150" x2="{x}" y2="392" stroke="{INK}" stroke-opacity=".12" stroke-dasharray="3 5"/>')
    steps = [
        (ax, wx, "1  GET /paid/knowledge?id=…", False),
        (wx, ax, "2  402 Payment Required · $0.01 USDC · pay-to · base-sepolia", False),
        (ax, wx, "3  the same request, with X-PAYMENT: a signed USDC transfer", True),
        (wx, bx, "4  verify and settle through the facilitator", True),
        (wx, ax, "5  200 OK · the unit body", False),
    ]
    y = 186
    for x1, x2, label, violet in steps:
        b.append(arrow(x1 + (8 if x1 < x2 else -8), y, x2 - (8 if x1 < x2 else -8), y, label, violet=violet,
                       lx=(x1 + x2) / 2, ly=y - 9))
        y += 42
    b.append(coin(bx, 352, 15))   # under the settle arrow's end, on the chain's lane
    b.append(text(bx, 386, "USDC moves once", 11.5, AMBER, 600, "middle"))
    b += [text(32, 422, "No sign-up, no key, no card: the payment is the auth. The seller's share of every sale accrues at settlement"),
          text(32, 442, "and is paid out on-chain; a buyer with an agent key can pay from prepaid credits instead.", 12.5, SOFT)]
    return svg(960, 458, "Payment is the auth",
               "One request, one 402, one signed transfer — and the second request comes back with the goods.",
               b, "The x402 purchase: a GET answered 402 with a price, retried with a signed USDC transfer, answered 200")


# ---- blog figures: narrow (480 wide) so they stay readable in a phone's column ----
def step(n: int, y: float, title: str, sub: str, *, accent: bool = False) -> str:
    """One numbered step of a vertical sequence: a number on the left, a glass row with two lines."""
    ring = (f'<circle cx="38" cy="{y + 25}" r="13" fill="{BG}" stroke="{VIOLET if accent else INK}" '
            f'stroke-opacity="{1 if accent else .3}" stroke-width="1.5"/>')
    return "\n".join([ring, text(38, y + 30, str(n), 13, INK, 700, "middle"),
                      panel(64, y, 384, 50, accent=accent, rx=12),
                      text(80, y + 21, title, 14.5, INK, 600), text(80, y + 39, sub, 13, SOFT)])


def oauth_signin() -> str:
    """The MCP sign-in, as the post tells it (api/src/oauth-web.ts): a 401, metadata, the client, consent, code, token."""
    rows = [("Claude or ChatGPT gets a 401", "WWW-Authenticate points at the metadata", False),
            ("It reads WITAN's metadata", "protected resource, then authorization server", False),
            ("It says who it is", "a client_id URL to its document, or registers", False),
            ("The operator signs in and allows", "a code by mail; consent picks the agent", True),
            ("WITAN redirects back with a code", "to a redirect URI the client declared", False),
            ("The app trades the code for tokens", "PKCE verifier; ChatGPT adds a signed JWT", False),
            ("MCP calls run as that agent", "Bearer wta_… for an hour, then refresh", True)]
    b = []
    for i, (title, sub, accent) in enumerate(rows):
        y = 100 + 62 * i
        b.append(step(i + 1, y, title, sub, accent=accent))
        if i:
            b.append(arrow(38, y - 23, 38, y + 10))
    return svg(480, 540, "Signing in to the MCP server",
               "From the first 401 to a token that acts as one agent.",
               b, "The MCP sign-in: a 401, the metadata, the client's identity, the operator's consent, a code, "
                  "a PKCE token exchange, and MCP calls as the chosen agent")


def chip(x: float, y: float, w: float, label: str, color: str, *, refused: bool = False) -> str:
    """A scope as a pill; a scope that cannot be granted is a dashed outline."""
    if refused:
        rect = (f'<rect x="{x}" y="{y}" width="{w}" height="26" rx="13" fill="none" stroke="{DIM}" '
                f'stroke-width="1.2" stroke-dasharray="4 3"/>')
        return rect + text(x + w / 2, y + 17.5, label, 12.5, DIM, 600, "middle")
    rect = (f'<rect x="{x}" y="{y}" width="{w}" height="26" rx="13" fill="{color}" fill-opacity=".16" '
            f'stroke="{color}" stroke-width="1.2"/>')
    return rect + text(x + w / 2, y + 17.5, label, 12.5, INK, 600, "middle")


def oauth_grant() -> str:
    """What one consent decides (api/src/oauth.ts): the app, the agent, the scopes per endpoint, the record, the revoke."""
    b = [box(32, 100, 416, 52, ["Claude or ChatGPT", "the app the operator allowed"]),
         arrow(240, 152, 240, 178), text(252, 170, "acts as", 12.5, DIM),
         panel(32, 180, 416, 70, accent=True, rx=12),
         text(48, 205, "One of the operator's agents", 14.5, INK, 600),
         text(48, 224, "an existing one, or a new one named after the app", 13, SOFT),
         text(48, 242, "one connection is one agent", 13, SOFT),
         arrow(240, 250, 240, 276), text(252, 268, "with scopes, per endpoint", 12.5, DIM)]
    for x, path, chips, note in ((32, "/mcp", [("read", VIOLET, False), ("write", VIOLET, False), ("spend", AMBER, False)],
                                  "every tool"),
                                 (248, "/mcp/directory", [("read", VIOLET, False), ("write", VIOLET, False), ("no spend", DIM, True)],
                                  "nothing that moves money")):
        b.append(panel(x, 278, 200, 100, rx=12))
        b.append(text(x + 16, 302, path, 14, INK, 500, mono=True))
        cx = x + 16
        for label, color, refused in chips:
            w = {"read": 46, "write": 50, "spend": 56}.get(label, 66)
            b.append(chip(cx, 314, w, label, color, refused=refused))
            cx += w + 5
        b.append(text(x + 16, 362, note, 13, SOFT))
    b += [arrow(240, 378, 240, 404),
          box(32, 406, 416, 52, ["Recorded under the agent's name", "what it reads, submits and earns; its quotas"]),
          arrow(240, 458, 240, 484),
          box(32, 486, 416, 52, ["Revoked in the console", "Connected apps; it stops within 30 seconds"])]
    return svg(480, 556, "What the consent decides",
               "One app, one agent, the scopes its endpoint allows.",
               b, "The OAuth grant: the app acts as one of the operator's agents with read, write and, on /mcp only, "
                  "spend; everything is recorded under the agent's name and can be revoked in the console")


# ---- blog figures in the site's own language: a starfield, hairline glass cards, constellations ----
SKY_INK, SKY_SOFT, SKY_DIM = "#F1F3F8", "#A9B0C0", "#7C85A0"   # the sky pages' tokens (api/src/theme.ts SKY_CSS)
SKY_GOOD, SKY_AMB = "#3ED6A0", "#FFB35C"
_TONES = ["200,215,255", "236,240,255"]


def svg_plain(w: int, h: int, body: list[str], label: str, seed: int = 3) -> str:
    """A blog figure: no header (the caption carries the words), a sparse starfield, the sky's near-black ground."""
    defs = ('<defs><radialGradient id="halo"><stop offset="0" stop-color="#7C5CFF" stop-opacity=".42"/>'
            '<stop offset="1" stop-color="#7C5CFF" stop-opacity="0"/></radialGradient>'
            '<radialGradient id="cardv" cx="0" cy="0" r="1"><stop offset="0" stop-color="#7C5CFF" stop-opacity=".12"/>'
            '<stop offset=".55" stop-color="#7C5CFF" stop-opacity="0"/></radialGradient>'
            '<radialGradient id="cardr" cx="1" cy="1" r="1"><stop offset="0" stop-color="#FF5C87" stop-opacity=".07"/>'
            '<stop offset=".6" stop-color="#FF5C87" stop-opacity="0"/></radialGradient></defs>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'role="img" aria-label="{escape(label)}" font-family="{FONT}" text-rendering="geometricPrecision">\n'
            + defs + f'<rect width="{w}" height="{h}" rx="18" fill="#03040A"/>\n'
            + starfield(w, h, seed) + "\n" + "\n".join(body) + "\n"
            f'<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="17.5" fill="none" stroke="{INK}" stroke-opacity=".12"/>\n</svg>\n')


def _dots(items: list[tuple[float, float, float, float]], tone: str) -> str:
    return f'<g fill="rgb({tone})">' + "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill-opacity="{a:.2f}"/>' for x, y, r, a in items) + "</g>"


def starfield(w: int, h: int, seed: int, n: int = 150) -> str:
    rng = random.Random(seed)
    groups: dict[str, list] = {t: [] for t in _TONES + ["124,92,255", "255,92,135"]}
    for _ in range(n):
        far = rng.random() < .85
        r = .5 + .5 * rng.random() if far else .9 + .5 * rng.random()
        a = .15 + .25 * rng.random() if far else .35 + .2 * rng.random()
        t = rng.random()
        tone = "124,92,255" if t < .04 else "255,92,135" if t < .06 else _TONES[0] if t < .45 else _TONES[1]
        groups[tone].append((rng.random() * w, rng.random() * h, r, a))
    return "\n".join(_dots(v, k) for k, v in groups.items() if v)


def _bez(p: tuple, t: float) -> tuple[float, float, float, float]:
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = p
    u = 1 - t
    x = u**3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t**3 * x3
    y = u**3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t**3 * y3
    dx = 3 * u * u * (x1 - x0) + 6 * u * t * (x2 - x1) + 3 * t * t * (x3 - x2)
    dy = 3 * u * u * (y1 - y0) + 6 * u * t * (y2 - y1) + 3 * t * t * (y3 - y2)
    n = math.hypot(dx, dy) or 1
    return x, y, -dy / n, dx / n   # the point and the unit normal


def stream(p: tuple, n: int, w0: float, w1: float, seed: int, warm: float = 0.0) -> str:
    """A stream of tiny stars along a curve, w0..w1 its half-width: density and width carry the amount."""
    rng = random.Random(seed)
    groups: dict[str, list] = {t: [] for t in _TONES + ["124,92,255", "94,224,255", "255,179,92"]}
    for _ in range(n):
        t = rng.random()
        x, y, nx, ny = _bez(p, t)
        off = rng.uniform(-1, 1) * (w0 + (w1 - w0) * t)
        k = rng.random()
        tone = ("255,179,92" if k < warm else "124,92,255" if k < warm + .14 else "94,224,255" if k < warm + .24
                else _TONES[0] if k < .62 else _TONES[1])
        groups[tone].append((x + nx * off, y + ny * off, .8 + rng.random() * .9, .35 + rng.random() * .5))
    return "\n".join(_dots(v, k) for k, v in groups.items() if v)


def dotted(p: tuple, count: int, tone: str = "185,169,255", r: float = 2.4, a: float = .85) -> str:
    return _dots([(x, y, r, a) for x, y, _, _ in (_bez(p, i / (count - 1)) for i in range(count))], tone)


def star(cx: float, cy: float, r: float, *, seed: int = 1, halo: float = 2.8, spikes: bool = True) -> str:
    """A constellation node: a soft violet halo, a handful of stars, one bright core."""
    rng = random.Random(seed)
    pts = [(cx + math.cos(a) * r * rng.random() ** .7, cy + math.sin(a) * r * rng.random() ** .7, .8 + rng.random() * 1.1,
            .4 + rng.random() * .5) for a in (rng.random() * 6.283 for _ in range(max(4, int(r * 1.6))))]
    out = [f'<circle cx="{cx}" cy="{cy}" r="{r * halo:.1f}" fill="url(#halo)"/>', _dots(pts, _TONES[1])]
    if spikes:
        L = r * 1.9
        out.append(f'<path d="M{cx - L} {cy}H{cx + L}M{cx} {cy - L}V{cy + L}" stroke="rgb(236,240,255)" stroke-opacity=".5" '
                   f'stroke-width="1.4" stroke-linecap="round"/>')
    out.append(f'<circle cx="{cx}" cy="{cy}" r="{max(2.4, r * .22):.1f}" fill="#fff"/>')
    return "\n".join(out)


def glass(x: float, y: float, w: float, h: float, rx: float = 16) -> str:
    """A sky card: near-black, the faint violet and pink corner glows, a hairline rim."""
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="#03030A" fill-opacity=".74"/>'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="url(#cardv)"/>'
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="url(#cardr)"/>'
            f'<rect x="{x + .5}" y="{y + .5}" width="{w - 1}" height="{h - 1}" rx="{rx}" fill="none" stroke="rgb(190,170,255)" stroke-opacity=".2"/>')


def mlabel(x: float, y: float, s: str, fill: str = SKY_DIM, size: float = 22, anchor: str = "start") -> str:
    """The site's small uppercase mono label."""
    return (f'<text x="{x}" y="{y}" font-size="{size}" fill="{fill}" text-anchor="{anchor}" letter-spacing="1.6" '
            f'font-family="{MONO}">{escape(s.upper())}</text>')


def split_flow() -> str:
    """One payment, one split, shares as streams of stars (api/src/blog/*-payment-splits.md)."""
    b = [mlabel(40, 232, "Payment · USDC"),
         stream(((40, 280), (160, 280), (280, 280), (410, 280)), 340, 30, 20, 11, warm=.12),
         text(40, 352, "the buyer pays", 28, SKY_SOFT), text(40, 386, "the item's own address", 28, SKY_SOFT),
         star(470, 280, 44, seed=4), text(470, 372, "The item's split", 30, SKY_INK, 500, "middle"),
         mlabel(470, 408, "no owner", SKY_DIM, 21, "middle"),
         stream(((520, 272), (700, 272), (790, 150), (980, 150)), 640, 36, 36, 21),
         stream(((520, 292), (700, 292), (790, 334), (980, 334)), 380, 22, 22, 22),
         f'<path d="M520 300C700 320 790 470 990 470" fill="none" stroke="{SKY_SOFT}" stroke-opacity=".38" stroke-width="1.6"/>',
         f'<line x1="720" y1="112" x2="720" y2="520" stroke="{SKY_SOFT}" stroke-opacity=".28" stroke-dasharray="2 7"/>',
         text(720, 62, "distribute()", 28, SKY_INK, 500, "middle", mono=True),
         mlabel(720, 92, "hourly · or by anyone", SKY_DIM, 19, "middle"),
         star(1020, 150, 30, seed=5), text(1076, 148, "Seller", 30, SKY_INK, 500), text(1076, 182, "the rest", 24, SKY_SOFT),
         star(1020, 314, 15, seed=6), star(1020, 346, 12, seed=7), star(1020, 374, 8, seed=8, spikes=False),
         text(1076, 336, "Contributors", 30, SKY_INK, 500), text(1076, 370, "by record weight", 24, SKY_SOFT),
         star(1020, 470, 6, seed=9, spikes=False, halo=2), text(1076, 466, "WITAN fee", 26, SKY_SOFT),
         text(1076, 498, "0% on testnet", 22, SKY_AMB, mono=True),
         f'<rect x="40.5" y="474.5" width="470" height="52" rx="14" fill="none" stroke="{SKY_SOFT}" stroke-opacity=".3" stroke-dasharray="3 5"/>',
         text(66, 508, "No WITAN wallet in the path", 26, SKY_SOFT)]
    return svg_plain(1280, 560, b, "A sale with payment splits, drawn as stars: one USDC payment enters the item's ownerless split "
                     "address, distribute() sends it on as streams whose width shows each share, to the seller and to the "
                     "contributors by record weight; WITAN's fee is a faint line at 0% on testnet; no WITAN wallet is in the path", seed=3)


def split_vs_custody() -> str:
    """One bright point held by a key, against stars that follow a printed rule to several points."""
    b = [glass(24, 24, 606, 512), glass(650, 24, 606, 512), mlabel(56, 74, "Platform wallet"), mlabel(682, 74, "The item's split")]
    for i, (y0, c) in enumerate(((140, 60), (250, 0), (360, -60), (190, 40), (310, -40))):
        b.append(stream(((70, y0), (170, y0 + c * .3), (230, 250 + c * .4), (306, 250)), 120, 5, 4, 30 + i))
    b += [star(330, 250, 26, seed=12, halo=3.2)]
    ring = [(500 + 26 * math.cos(a), 250 + 26 * math.sin(a), 2.3, .9) for a in (i * 6.2832 / 30 for i in range(30))]
    shaft = [(474 - i * 4.2, 250, 2.3, .9) for i in range(0, 28)]
    teeth = [(386 + dx, 250 + k * 5, 2.3, .9) for dx in (0, 22) for k in range(1, 5)]
    b += [_dots(ring + shaft + teeth, "236,240,255"), mlabel(430, 322, "one owner", SKY_DIM, 21, "middle"),
          mlabel(56, 420, "who decides"), text(56, 462, "us", 40, SKY_INK, 500), text(56, 502, "one key, one operator", 26, SKY_SOFT)]
    b += [text(682, 176, "seller        the rest", 22, SKY_SOFT, mono=True), text(682, 206, "contributors  by record weight", 22, SKY_SOFT, mono=True),
          f'<line x1="682" y1="226" x2="1010" y2="226" stroke="{SKY_SOFT}" stroke-opacity=".4"/>',
          stream(((676, 270), (760, 270), (840, 270), (920, 270)), 240, 8, 8, 40)]
    for i, (y, r) in enumerate(((190, 14), (250, 22), (312, 12), (366, 9))):
        b += [stream(((920, 270), (1010, 270), (1060, y), (1170, y)), 70, 5, 3, 50 + i), star(1190, y, r, seed=60 + i, spikes=r > 10)]
    b += [mlabel(1190, 410, "no owner", SKY_DIM, 21, "middle"), mlabel(682, 420, "who decides"),
          text(682, 462, "the rule", 40, SKY_INK, 500), text(682, 502, "readable before paying", 26, SKY_SOFT)]
    return svg_plain(1280, 560, b, "Two ways to pay a seller, drawn as stars: through a platform wallet, where every payment converges on one point "
                     "held by one operator's key, or through the item's ownerless split, where the stars follow a printed rule "
                     "that anyone can read before paying to several points", seed=5)


def review_flow() -> str:
    """The score axis, 0 to 100: three tinted zones, units as stars on it, the way back from the question band."""
    x = lambda v: 80 + v * 11.2
    y0 = 300
    b = []
    for lo, hi, rgb, name, tone, sub in ((0, 45, "255,92,135", "Reject", ROSE, "with the reason"),
                                         (45, 55, "255,179,92", "Ask first", SKY_AMB, "questions · 72 h"),
                                         (55, 100, "62,214,160", "Publish", SKY_GOOD, "with a verdict card")):
        b += [f'<rect x="{x(lo)}" y="190" width="{x(hi) - x(lo)}" height="{y0 - 190}" fill="rgb({rgb})" fill-opacity=".07"/>',
              mlabel((x(lo) + x(hi)) / 2, 128, name, tone, 25, "middle"),
              text((x(lo) + x(hi)) / 2, 166, sub, 26, SKY_SOFT, anchor="middle")]
    b.append(f'<line x1="{x(0)}" y1="{y0}" x2="{x(100)}" y2="{y0}" stroke="{SKY_SOFT}" stroke-opacity=".5" stroke-width="1.5"/>')
    for v in (0, 45, 55, 100):
        b += [f'<line x1="{x(v)}" y1="{y0}" x2="{x(v)}" y2="{y0 + 12}" stroke="{SKY_SOFT}" stroke-opacity=".6"/>',
              text(x(v), y0 + 46, str(v), 26, SKY_INK if v in (45, 55) else SKY_DIM, 500, "middle", mono=True)]
    for i, v in enumerate((14, 36, 49, 63, 88)):
        b.append(star(x(v), y0, 9, seed=70 + i))
    sx, ex = x(49), x(72)
    b += [star(ex, y0, 9, seed=80), dotted(((sx, y0 + 26), (sx, 470), (ex, 470), (ex, y0 + 28)), 46),
          f'<path d="M{ex - 11} {y0 + 44}L{ex} {y0 + 28}L{ex + 11} {y0 + 44}" fill="none" stroke="rgb(185,169,255)" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>',
          text((sx + ex) / 2, 494, "supplement → re-check", 28, SKY_SOFT, anchor="middle")]
    return svg_plain(1280, 530, b, "The review's score axis from 0 to 100: under 45 is rejected with the reason; 45 to 55 gets questions and "
                     "72 hours, then the seller supplements and it is checked again; 55 or more is published with a verdict card", seed=8)


def verdict_card() -> str:
    """A unit page's price panel and Review checks (api/src/market-web.ts), as a buyer sees them."""
    from xml.sax.saxutils import escape as esc
    def line(y: float, word: str, col: str, label: str) -> str:
        return (f'<text x="300" y="{y}" font-size="28" xml:space="preserve"><tspan fill="{col}">{esc(word)}</tspan>'
                f'<tspan fill="{SKY_INK}" dx="14">{esc(label)}</tspan></text>')
    b = [glass(40, 36, 780, 128), glass(40, 180, 780, 346),
         text(72, 96, "0.01 USDC", 32, SKY_INK, 700), text(790, 94, "72", 30, SKY_AMB, 700, "end", mono=True),
         text(740, 94, "score", 26, SKY_AMB, anchor="end"), text(72, 138, "per x402 read · free with an agent key", 23, SKY_DIM),
         text(72, 246, "Review checks", 26, SKY_DIM),
         line(246, "✔ verified", SKY_GOOD, "Numbers recomputed"), line(292, "declared by the author", SKY_DIM, "Versions"),
         line(338, "measured, no verdict", SKY_DIM, "Sources"), line(384, "△ not checked", SKY_DIM, "Reproduced"),
         text(300, 424, "2026-10-24 · rules v1", 22, SKY_DIM),
         text(72, 484, "Agents' checks", 26, SKY_DIM), text(300, 484, "9 worked · 1 failed", 28, SKY_INK),
         mlabel(880, 82, "every status"),
         text(880, 142, "✔ verified", 28, SKY_GOOD), text(880, 194, "✖ contradicts itself", 28, SKY_AMB),
         text(880, 246, "declared by the author", 28, SKY_DIM), text(880, 298, "measured, no verdict", 28, SKY_DIM),
         text(880, 350, "△ not checked", 28, SKY_DIM),
         text(880, 440, "A buyer reads statuses and", 23, SKY_SOFT), text(880, 472, "counts, never the reviewer's", 23, SKY_SOFT),
         text(880, 504, "reasons.", 23, SKY_SOFT)]
    return svg_plain(1280, 560, b, "An example unit page: the price panel with a score of 72, then Review checks: numbers recomputed and "
                     "verified, versions declared by the author, sources measured with no verdict, reproduction not checked, and "
                     "nine agents who said it worked and one that failed", seed=9)


def review_results() -> str:
    """False rejections of honest units on the synthetic set: seven stars, then one."""
    base = 400
    b = [text(40, 160, "False rejections", 34, SKY_INK, 500), text(40, 202, "of honest units", 28, SKY_SOFT),
         f'<line x1="380" y1="{base}" x2="1000" y2="{base}" stroke="{SKY_SOFT}" stroke-opacity=".45"/>']
    for i in range(7):
        b.append(star(520, base - 34 - 46 * i, 11, seed=90 + i))
    b += [star(840, base - 34, 11, seed=99), text(468, 262, "7", 48, SKY_INK, 500, "end", mono=True),
          text(788, 380, "1", 48, SKY_INK, 500, "end", mono=True),
          dotted(((580, 250), (650, 250), (700, 300), (770, 340)), 24),
          '<path d="M748 346L772 341L760 320" fill="none" stroke="rgb(185,169,255)" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>',
          
          mlabel(520, 442, "cut at 55", SKY_SOFT, 22, "middle"), mlabel(840, 442, "with the 45–55 band", SKY_SOFT, 22, "middle"),
          mlabel(40, 470, "synthetic set, draft labels", SKY_DIM, 19)]
    return svg_plain(1280, 490, b, "False rejections of honest units on the synthetic set, drawn as stars: seven with the cut at 55, "
                     "one with the 45 to 55 band", seed=13)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in [("how-it-works", how_it_works), ("why-witan", why_witan),
                     ("overview", overview), ("dataset-model", dataset_model),
                     ("trust-chain", trust_chain), ("node-topology", node_topology),
                     ("x402-flow", x402_flow), ("oauth-signin", oauth_signin),
                     ("oauth-grant", oauth_grant),
                     ("split-flow", split_flow), ("split-vs-custody", split_vs_custody),
                     ("review-flow", review_flow), ("verdict-card", verdict_card),
                     ("review-results", review_results)]:
        svg = fn()
        (OUT / f"{name}.svg").write_text(svg, encoding="utf-8", newline="\n")   # LF on every platform: the files are compared byte for byte
        OUT_API.mkdir(parents=True, exist_ok=True)
        (OUT_API / f"{name}.svg").write_text(svg, encoding="utf-8", newline="\n")
        print("wrote", OUT / f"{name}.svg")


if __name__ == "__main__":
    main()
