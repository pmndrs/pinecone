"""Draws the PINECONE word mark: the pmndrs mark as the P, then letters on the same grid as pine's logo
(squares of 240 with gaps of 40; one corner of a letter is joined, like the corner of the mark).

    python docs/images/make-logo.py        writes logo.svg, logo-dark.svg and logo-light.svg next to itself"""
from pathlib import Path

U, S, G = 280, 240, 40          # grid step, square, gap
MARK = [(1, 2), (1, 1), (0, 1)]  # the three loose squares of the pmndrs mark; its joined corner is drawn as a path
# Each letter: its squares as (column, row), and joins between neighbours: "h" joins a square to the one on its
# right, "v" to the one below.
LETTERS = {
    "I": ([(0, 0), (0, 1), (0, 2)], []),
    "N": ([(0, 0), (0, 1), (0, 2), (1, 0), (2, 0), (2, 1), (2, 2)], [("h", 1, 0), ("v", 2, 0)]),
    "E": ([(0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (0, 2), (1, 2), (2, 2)], [("h", 0, 0), ("v", 0, 0)]),
    "C": ([(0, 0), (1, 0), (2, 0), (0, 1), (0, 2), (1, 2), (2, 2)], [("h", 0, 0), ("v", 0, 0)]),
    "O": ([(0, 0), (1, 0), (2, 0), (0, 1), (2, 1), (0, 2), (1, 2), (2, 2)], [("h", 1, 0), ("v", 2, 0)]),
}
WIDTH = {"I": 1}


def shapes(word, x0, y0, fill):
    out = [f'<rect x="{x0 + c * U}" y="{y0 + r * U}" width="{S}" height="{S}" fill="{fill}"/>' for c, r in MARK]
    out.append(f'<path fill-rule="evenodd" clip-rule="evenodd" transform="translate({x0} {y0})" d="M560 0H280V240H560V520H800V0H560Z" fill="{fill}"/>')
    x = x0 + 4 * U - G  # the mark is three columns wide; a letter starts one grid step after the last one ends
    for ch in word[1:]:
        squares, joins = LETTERS[ch]
        out += [f'<rect x="{x + c * U}" y="{y0 + r * U}" width="{S}" height="{S}" fill="{fill}"/>' for c, r in squares]
        for kind, c, r in joins:
            if kind == "h":
                out.append(f'<rect x="{x + c * U + S}" y="{y0 + r * U}" width="{G}" height="{S}" fill="{fill}"/>')
            else:
                out.append(f'<rect x="{x + c * U}" y="{y0 + r * U + S}" width="{S}" height="{G}" fill="{fill}"/>')
        x += (WIDTH.get(ch, 3) + 1) * U - G
    return out, x - U - x0  # the shapes, and how wide the word is


def svg(word, fill, margin=0, background=None):
    body, w = shapes(word, margin, margin, fill)
    width, height = w + 2 * margin, 3 * U - G + 2 * margin
    head = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" fill="none" role="img" aria-label="{word}">'
    back = [f'<rect x="0" y="0" width="{width}" height="{height}" fill="{background}"/>'] if background else []
    return "\n".join([head, *back, *body, "</svg>"]) + "\n"


here = Path(__file__).parent
(here / "logo.svg").write_text(svg("PINECONE", "white", margin=480, background="black"))
(here / "logo-dark.svg").write_text(svg("PINECONE", "white"))
(here / "logo-light.svg").write_text(svg("PINECONE", "black"))
