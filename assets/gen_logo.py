"""Generate the Egernia pixel-art logo (banner + square mark) as SVG."""

import pathlib
import sys

CELL = 14

PALETTE = {
    "B": "#c8562b",  # body base rust
    "L": "#e2793c",  # dorsal highlight
    "H": "#e8c69c",  # pale head / cream belly
    "W": "#fdf1e0",  # white marking edge / speckle
    "M": "#201208",  # black transverse blotch
    "S": "#8c3616",  # keeled spine
    "F": "#a04a22",  # foot / toes
    "D": "#140c06",  # eye, mouth line
}

# Egernia epsisolus in profile, facing left. 32 x 13 cells.
SPRITE = [
    "................................",
    "................................",
    "............................S.SS",
    ".........................S.SLMLL",
    ".......S.S.S.S.S.S.S.SS.SLMLLMLL",
    "......LLLMMLLMMLLMMLLLMLLMLLLL..",
    ".HWDHHBBBMMBBMMBBMMBBBBMBBBB....",
    "HHDDHHBBBWWBBWWBBWWBBBBBB.......",
    "HHHHHHBBBBBBBBBBBBBBBB..........",
    "DDDHHHHHHHHHHHHHHHHHHB..........",
    "..HHHHHHHHHHHHHHHHHHHB..........",
    "......BB..........BB............",
    ".....FFF.........FFF............",
]

FONT = {
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
    "G": [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "N": ["#...#", "##..#", "##..#", "#.#.#", "#..##", "#..##", "#...#"],
    "I": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "#####"],
    "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
}
WORD = "EGERNIA"
WORD_PX = 11
WORD_FILL = "#d2622e"


def rects(rows, cell, lookup, indent="    "):
    out = []
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            fill = lookup(ch)
            if not fill:
                continue
            out.append(
                f'{indent}<rect x="{x * cell}" y="{y * cell}" '
                f'width="{cell}" height="{cell}" fill="{fill}"/>'
            )
    return "\n".join(out)


def sprite_rects(cell, indent="    "):
    return rects(SPRITE, cell, lambda c: PALETTE.get(c), indent)


def wordmark_rects(indent="    "):
    out = []
    advance = 0
    for letter in WORD:
        glyph = FONT[letter]
        for y, row in enumerate(glyph):
            for x, ch in enumerate(row):
                if ch != "#":
                    continue
                out.append(
                    f'{indent}<rect x="{(advance + x) * WORD_PX}" y="{y * WORD_PX}" '
                    f'width="{WORD_PX}" height="{WORD_PX}" fill="{WORD_FILL}"/>'
                )
        advance += 6
    return "\n".join(out)


def banner_svg():
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1040" height="260"\
 viewBox="0 0 1040 260">
  <title>egernia — IVOA TAP 1.1 for the SKA</title>
  <g transform="translate(40 38)" shape-rendering="crispEdges">
{sprite_rects(CELL)}
  </g>
  <g transform="translate(540 76)" shape-rendering="crispEdges">
{wordmark_rects()}
  </g>
  <text x="542" y="196"
        font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
        font-size="24" fill="#808080" font-style="italic">spiny tail, wide sky</text>
</svg>
"""


def mark_svg():
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512"\
 viewBox="0 0 512 512">
  <title>egernia</title>
  <g transform="translate(32 165)" shape-rendering="crispEdges">
{sprite_rects(CELL)}
  </g>
</svg>
"""


def main(outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "egernia-logo.svg").write_text(banner_svg())
    (outdir / "egernia-mark.svg").write_text(mark_svg())
    print("wrote", outdir / "egernia-logo.svg", outdir / "egernia-mark.svg")


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1]))
