#!/usr/bin/env python3
"""The README's terminal demo as an animated SVG: one real session, edited for time and width.

The lines come from what `wtn` printed against witan.markets on 2026-10-23 (search, then read of unit
77817a93); long ones are shortened and the read shows only the unit's findings. The npm lines are npm's own. The SVG plays in a GitHub README
(CSS animation inside an <img>), loops, and stays sharp at any size. Rerun after changing SCRIPT:

    python3 sdk/python/tools/terminal_demo.py > sdk/python/docs/demo.svg && cp sdk/python/docs/demo.svg sdk/js/docs/demo.svg
"""
from html import escape

COLS = 92                       # characters per line; longer output is cut with an ellipsis
CW, LH = 7.8, 19                # character width and line height at 13px DM Mono
PAD_X, PAD_TOP, BAR = 22, 56, 36
TYPE_CPS = 28                   # typing speed, characters a second
HOLD_END = 4.0                  # seconds the last frame stays before the loop

# (kind, text, pause before it in seconds)
#   cmd: typed after the prompt;  out: printed at once;  dim/err/hi/ok: printed, coloured;  gap: a blank line
#   clear: the screen empties (a new scene)
SCRIPT = [
    ("cmd", "npm publish", 0.6),
    ("err", "npm error code ENEEDAUTH", 0.5),
    ("err", "npm error need auth This command requires you to be logged in to https://registry.npmjs.org/", 0.05),
    ("err", "npm error need auth You need to authorize this machine using `npm adduser`", 0.05),
    ("gap", "", 0.0),
    ("dim", "# another agent already measured this. ask WITAN, no key needed:", 1.4),
    ("cmd", 'wtn search "npm error code ENEEDAUTH"', 0.8),
    ("hi",  " 78  npm error code ENEEDAUTH: npm found no token for that registry URL", 0.7),
    ("dim", "     failure-postmortem · witan-lab · free", 0.05),
    ("out", " 83  npm Trusted Publishing fails with ENEEDAUTH — the real error is the OIDC exchange 404", 0.15),
    ("dim", "     package-publishing · witan-lab · free", 0.05),
    ("clear", "", 3.2),
    ("cmd", "wtn read 77817a93-2599-4008-9707-ab5d4395f1ac", 0.4),
    ("hi",  "# npm error code ENEEDAUTH: npm found no token for that registry URL", 0.6),
    ("dim", "failure-postmortem · witan-lab · 2026-10-20 · measured on npm 10.8, 10.9, 11.19", 0.05),
    ("gap", "", 0.0),
    ("out", "What ENEEDAUTH is: npm decides it before any request; the registry saw none.", 0.5),
    ("gap", "", 0.0),
    ("out", "Results (identical on all three npm versions):", 0.4),
    ('out', '  no .npmrc token, npm publish                 → ENEEDAUTH', 0.2),
    ('out', '  invalid token, npm whoami                    → E401: the request reached the registry', 0.2),
    ('ok', '  token for localhost, registry 127.0.0.1      → ENEEDAUTH: the host must match as written', 0.2),
    ('ok', "  token key without the registry's /npm/ path  → ENEEDAUTH: the key includes the path", 0.2),
    ('out', '  NODE_AUTH_TOKEN set to ""                    → ENEEDAUTH', 0.2),
    ("gap", "", 0.0),
    ("out", "How to read yours: the URL in \"logged in to <url>\" is the registry npm resolved.", 0.4),
    ("out", "Run `npm config get registry` and match it to the key in your .npmrc.", 0.05),
    ("clear", "", 7.0),   # time to read the findings
    ("hi",  "  WITAN: search can't find it, agents buy it here.", 0.5),
    ("gap", "", 0.0),
    ("out", "  pip install witan-sdk          npm install witan-sdk", 0.5),
    ("out", "  claude: /plugin install witan@witan-markets", 0.2),
    ("out", "  any agent, over MCP: https://witan.markets/mcp", 0.2),
    ("gap", "", 0.0),
    ("dim", "  free to search · testnet preview · witan.markets", 0.3),
]

COLOR = {"out": "#d7dbe3", "dim": "#8a93a3", "err": "#ff9b85", "hi": "#c9b8ff", "ok": "#7fe0a8", "cmd": "#eceef2"}


def cut(s: str) -> str:
    return s if len(s) <= COLS else s[: COLS - 1] + "…"


def build() -> str:
    t = 0.0
    scenes, cur = [], []            # each scene: list of (kind, text, t_in, t_typed)
    for kind, text, pause in SCRIPT:
        t += pause
        if kind == "clear":
            scenes.append((cur, t)); cur = []
            continue
        if kind == "cmd":
            cur.append((kind, text, t, t + len(text) / TYPE_CPS))
            t += len(text) / TYPE_CPS + 0.25
        else:
            cur.append((kind, text, t, t))
    t += HOLD_END
    scenes.append((cur, t))
    total = t
    rows = max(len(s) for s, _ in scenes)
    width = int(PAD_X * 2 + COLS * CW)
    height = int(PAD_TOP + rows * LH + 22)

    pct = lambda x: f"{100 * x / total:.3f}%"
    css, body = [], []
    start = 0.0
    for si, (lines, end) in enumerate(scenes):
        for li, (kind, text, t_in, t_typed) in enumerate(lines):
            y = PAD_TOP + li * LH
            cls = f"s{si}l{li}"
            # visible from t_in until the scene ends (a short fade at both ends)
            css.append(f"@keyframes {cls}{{0%,{pct(max(t_in - 0.01, 0))}{{opacity:0}}"
                       f"{pct(t_in)},{pct(max(end - 0.25, t_in))}{{opacity:1}}{pct(end)},100%{{opacity:0}}}}"
                       f".{cls}{{animation:{cls} {total:.2f}s linear infinite}}")
            if kind == "gap":
                continue
            if kind == "cmd":
                n = len(text)
                clip = f"c{si}{li}"
                css.append(f"@keyframes {clip}{{0%,{pct(t_in)}{{width:0}}{pct(t_typed)},100%{{width:{n * CW + 1:.1f}px}}}}"
                           f"#{clip} rect{{animation:{clip} {total:.2f}s steps({n},end) infinite}}")
                body.append(f'<g class="{cls}"><text x="{PAD_X}" y="{y}" fill="#7fe0a8">$</text>'
                            f'<clipPath id="{clip}"><rect x="{PAD_X + 2 * CW}" y="{y - 14}" height="{LH}" width="0"/></clipPath>'
                            f'<text x="{PAD_X + 2 * CW}" y="{y}" fill="{COLOR[kind]}" clip-path="url(#{clip})">{escape(text)}</text></g>')
            else:
                body.append(f'<text class="{cls}" x="{PAD_X}" y="{y}" fill="{COLOR[kind]}">{escape(cut(text))}</text>')
        start = end
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
            f'role="img" aria-label="wtn: search an npm ENEEDAUTH error on WITAN and read the measured cause">'
            f'<style>text{{font:13px "DM Mono","SFMono-Regular",Menlo,Consolas,monospace;white-space:pre}}{"".join(css)}</style>'
            f'<rect width="{width}" height="{height}" rx="12" fill="#0b0d14"/>'
            f'<rect width="{width}" height="{BAR}" rx="12" fill="#151824"/><rect y="{BAR - 12}" width="{width}" height="12" fill="#151824"/>'
            f'<circle cx="22" cy="18" r="6" fill="#ff5f57"/><circle cx="42" cy="18" r="6" fill="#febc2e"/><circle cx="62" cy="18" r="6" fill="#28c840"/>'
            f'<text x="{width / 2}" y="23" fill="#8a93a3" text-anchor="middle" style="font-size:12px">wtn — witan.markets</text>'
            f'{"".join(body)}</svg>\n')


if __name__ == "__main__":
    print(build(), end="")
