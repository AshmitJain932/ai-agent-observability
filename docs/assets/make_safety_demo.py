"""Render the action-safety demo animation (``safety-demo.gif`` and ``safety-demo.svg``) from the
real output of ``examples/support_agent/protect_example.py``.

    uv run --with pillow python docs/assets/make_safety_demo.py

Pillow is not a project dependency; ``--with pillow`` adds it for this one run. The script:

1. runs the example as a subprocess, as the animation shows it, and takes its standard output:
   the text in the animation is that output, unchanged (only colours are added);
2. runs it once more in this process with a counter around the agent's ``issue_refund`` tool, and
   refuses to render unless that output is identical and the tool ran exactly once (what the
   caption says);
3. draws the frames with Pillow in a dark terminal style: the command is typed, the output lines
   appear one by one, and the last frame is held for three seconds.

It writes ``safety-demo.gif`` and ``safety-demo.svg`` (the same session as an animated SVG: SMIL,
no script) next to this file, or into ``--out-dir``. ``--frames DIR`` also saves every GIF frame as
a PNG, to check them. The example is deterministic, so with the same font file and Pillow version
the GIF comes out byte for byte the same. Font: DejaVu Sans Mono (Debian/Ubuntu
``fonts-dejavu-core``) or Liberation Mono, found in the usual places, or ``--font PATH`` for
another monospace TTF. Without one, Pillow's built-in font is used, which is not monospaced.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "examples" / "support_agent" / "protect_example.py"
COMMAND = "uv run python examples/support_agent/protect_example.py"
CAPTION = "aobs.safety: the same refund asked 3 times (once after a restart), the tool runs once"

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/local/share/fonts/DejaVuSansMono.ttf",
    "/Library/Fonts/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/usr/share/fonts/liberation-mono/LiberationMono-Regular.ttf",
]

# Layout (pixels) and colours: a dark terminal.
WIDTH = 800
FONT_SIZE = 16
CAPTION_SIZE = 13
LINE_HEIGHT = 25
PAD_X = 22
PAD_Y = 18
TITLE_BAR = 34
CAPTION_X = WIDTH / 2 + 25  # the caption's centre, a little right of the window's (the dots are left)
COLORS: dict[str, str] = {
    "bg": "#0d1117",
    "bar": "#161b22",
    "border": "#30363d",
    "text": "#e6edf3",
    "dim": "#8b949e",
    "prompt": "#3fb950",
    "label": "#79c0ff",
    "ok": "#3fb950",
}
DOTS = ("#ff5f56", "#ffbd2e", "#27c93f")

# Timing (milliseconds).
START_MS = 700  # the empty prompt, before typing
TYPE_CHARS_PER_FRAME = 2
TYPE_FRAME_MS = 40  # GIF delays are in hundredths of a second
RUN_MS = 900  # after Enter, before the first output line
LINE_MS = 750  # between output lines
HOLD_MS = 3000  # the final screen, with a blinking cursor
BLINK_MS = 500


# --------------------------------------------------------------------------- the real output


def run_example() -> list[str]:
    """The example's standard output, run as a subprocess the way the animation shows it."""
    done = subprocess.run(
        [sys.executable, str(EXAMPLE)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, check=False
    )
    if done.returncode != 0:
        raise SystemExit(f"{EXAMPLE.name} failed ({done.returncode}):\n{done.stderr}")
    if done.stderr.strip():
        print(f"note: {EXAMPLE.name} wrote to stderr (not shown):\n{done.stderr}", file=sys.stderr)
    return done.stdout.rstrip("\n").splitlines()


def count_refunds() -> tuple[list[str], int]:
    """Run the example in this process with a counter around the agent's refund tool: its output,
    and how many times the tool really ran."""
    sys.path.insert(0, str(EXAMPLE.parent))
    try:
        import agent  # type: ignore[import-not-found]
        import protect_example  # type: ignore[import-not-found]
    finally:
        sys.path.remove(str(EXAMPLE.parent))
    original: Callable[..., Any] = agent.issue_refund
    calls = 0

    def issue_refund(order_id: str, amount: float) -> Any:
        nonlocal calls
        calls += 1
        return original(order_id=order_id, amount=amount)

    agent.issue_refund = issue_refund
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            protect_example.main()
    finally:
        agent.issue_refund = original
    return out.getvalue().rstrip("\n").splitlines(), calls


# --------------------------------------------------------------------------- what is on the screen

Segment = tuple[str, str]  # (text, colour name)


def colour(line: str) -> list[Segment]:
    """Colours for one output line; the text itself is unchanged."""
    label, sep, rest = line.partition(": ")
    segments: list[Segment] = [(label + sep, "label"), (rest, "text")] if sep else [(line, "text")]
    out: list[Segment] = []
    for text, name in segments:
        head, found, tail = text.partition("SUCCEEDED")
        out += [(head, name), (found, "ok"), (tail, name)] if found else [(text, name)]
    return [s for s in out if s[0]]


def prompt(typed: str = "") -> list[Segment]:
    return [("$ ", "prompt"), (typed, "text")] if typed else [("$ ", "prompt")]


@dataclass(frozen=True)
class Screen:
    rows: tuple[tuple[Segment, ...], ...]
    cursor: tuple[int, int] | None  # (row, column) of the block cursor, if shown


def typing_steps() -> int:
    return -(-len(COMMAND) // TYPE_CHARS_PER_FRAME)


def timeline(output: list[str]) -> list[tuple[Screen, int]]:
    """Every frame of the session and how long it is shown (ms)."""
    frames: list[tuple[Screen, int]] = [(Screen((tuple(prompt()),), (0, 2)), START_MS)]
    for k in range(1, typing_steps() + 1):
        typed = COMMAND[: k * TYPE_CHARS_PER_FRAME]
        frames.append((Screen((tuple(prompt(typed)),), (0, 2 + len(typed))), TYPE_FRAME_MS))
    frames[-1] = (frames[-1][0], RUN_MS)  # Enter: the example runs
    rows: tuple[tuple[Segment, ...], ...] = (tuple(prompt(COMMAND)),)
    for line in output:
        rows += (tuple(colour(line)),)
        frames.append((Screen(rows, None), LINE_MS))
    rows += (tuple(prompt()),)
    for i in range(HOLD_MS // BLINK_MS):
        frames.append((Screen(rows, (len(rows) - 1, 2) if i % 2 == 0 else None), BLINK_MS))
    return frames


def height(rows: int) -> int:
    return TITLE_BAR + 2 * PAD_Y + rows * LINE_HEIGHT


# --------------------------------------------------------------------------- GIF


def find_font(path: str | None) -> str | None:
    if path:
        return path
    return next((c for c in FONT_CANDIDATES if Path(c).is_file()), None)


def load(path: str | None, size: int) -> Any:
    if path is None:
        print("warning: no monospace TTF found (use --font); using Pillow's built-in font", file=sys.stderr)
        return ImageFont.load_default(size=size)
    return ImageFont.truetype(path, size)


def draw_frame(screen: Screen, rows: int, font: Any, caption_font: Any, char_w: float) -> Image.Image:
    image = Image.new("RGB", (WIDTH, height(rows)), COLORS["bg"])
    d = ImageDraw.Draw(image)
    d.rounded_rectangle((0, 0, WIDTH - 1, image.height - 1), radius=10, fill=COLORS["bg"], outline=COLORS["border"])
    d.rounded_rectangle((1, 1, WIDTH - 2, TITLE_BAR), radius=9, fill=COLORS["bar"])
    d.rectangle((1, TITLE_BAR - 9, WIDTH - 2, TITLE_BAR), fill=COLORS["bar"])
    d.line((1, TITLE_BAR, WIDTH - 2, TITLE_BAR), fill=COLORS["border"])
    for i, dot in enumerate(DOTS):
        cx, cy = 20 + i * 20, TITLE_BAR // 2
        d.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=dot)
    d.text((CAPTION_X, TITLE_BAR / 2), CAPTION, font=caption_font, fill=COLORS["dim"], anchor="mm")
    for r, segments in enumerate(screen.rows):
        x, y = float(PAD_X), TITLE_BAR + PAD_Y + r * LINE_HEIGHT
        for text, name in segments:
            d.text((x, y), text, font=font, fill=COLORS[name])
            x += len(text) * char_w
    if screen.cursor is not None:
        r, c = screen.cursor
        x0, y0 = PAD_X + c * char_w, TITLE_BAR + PAD_Y + r * LINE_HEIGHT
        d.rectangle((x0, y0 + 1, x0 + char_w - 1, y0 + FONT_SIZE + 3), fill=COLORS["text"])
    return image


def write_gif(frames: list[tuple[Screen, int]], rows: int, font_path: str | None, out: Path) -> list[Image.Image]:
    font, caption_font = load(font_path, FONT_SIZE), load(font_path, CAPTION_SIZE)
    char_w = font.getlength("M")
    if caption_font.getlength(CAPTION) / 2 > min(CAPTION_X - 70, WIDTH - 12 - CAPTION_X):  # clear of the dots
        raise SystemExit("the caption does not fit in the title bar: shorten it")
    images = [draw_frame(screen, rows, font, caption_font, char_w) for screen, _ in frames]
    palette = images[-2].quantize(colors=128, dither=Image.Dither.NONE)  # the fullest frame, cursor shown
    indexed = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image in images]
    indexed[0].save(out, save_all=True, append_images=indexed[1:], duration=[ms for _, ms in frames], loop=0)
    return images


# --------------------------------------------------------------------------- SVG


def write_svg(output: list[str], out: Path) -> None:
    """The same session as an animated SVG (SMIL, no script), with the GIF's timing. Every
    animation lasts the whole session and repeats, so they stay in step. Character cells are fixed
    with ``textLength``, so the layout holds with whichever monospace font the viewer has."""
    rows = len(output) + 2
    h = height(rows)
    char_w = FONT_SIZE * 0.6
    steps = typing_steps()
    first_line = START_MS + (steps - 1) * TYPE_FRAME_MS + RUN_MS
    final = first_line + len(output) * LINE_MS  # the last prompt appears
    total = final + HOLD_MS

    def discrete(attribute: str, changes: list[tuple[int, str]]) -> str:
        """``attribute`` takes each value from its time (ms) on, in every loop."""
        values = ";".join(v for _, v in changes)
        times = ";".join(f"{t / total:.4f}" for t, _ in changes)
        return (
            f'<animate attributeName="{attribute}" values="{values}" keyTimes="{times}" calcMode="discrete" '
            f'dur="{total / 1000:.2f}s" repeatCount="indefinite"/>'
        )

    def baseline(r: int) -> float:
        return TITLE_BAR + PAD_Y + r * LINE_HEIGHT + FONT_SIZE - 2

    def row(segments: list[Segment], r: int, appear_ms: int | None = None, clip: str = "") -> str:
        x, parts = float(PAD_X), []
        for text, name in segments:
            core = text.strip(" ")  # outer spaces only move the text: in textLength they would stretch it
            if core:
                lead = (len(text) - len(text.lstrip(" "))) * char_w
                parts.append(
                    f'<text x="{x + lead:.1f}" y="{baseline(r):.1f}" fill="{COLORS[name]}" '
                    f'textLength="{len(core) * char_w:.1f}" lengthAdjust="spacingAndGlyphs">{escape(core)}</text>'
                )
            x += len(text) * char_w
        attributes = f' clip-path="url(#{clip})"' if clip else ""
        if appear_ms is not None:
            attributes += ' opacity="0"'
            parts.insert(0, discrete("opacity", [(0, "0"), (appear_ms, "1")]))
        return f"<g{attributes}>{''.join(parts)}</g>"

    prompt_w = 2 * char_w
    typed = [(START_MS + k * TYPE_FRAME_MS, min((k + 1) * TYPE_CHARS_PER_FRAME, len(COMMAND))) for k in range(steps)]
    clip_widths = [(0, "0"), *((t, f"{n * char_w:.1f}") for t, n in typed)]
    cursor_x = [
        (0, f"{PAD_X + prompt_w:.1f}"),
        *((t, f"{PAD_X + prompt_w + n * char_w:.1f}") for t, n in typed),
        (final, f"{PAD_X + prompt_w:.1f}"),
    ]
    tops = (baseline(0) - FONT_SIZE + 3, baseline(rows - 1) - FONT_SIZE + 3)
    blink = [(final + i * BLINK_MS, "1" if i % 2 == 0 else "0") for i in range(HOLD_MS // BLINK_MS)]
    cursor_on = [(0, "1"), (first_line - RUN_MS, "0"), *blink]  # hidden while the example runs
    body = [
        f'<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{h - 1}" rx="10" fill="{COLORS["bg"]}" '
        f'stroke="{COLORS["border"]}"/>',
        f'<path d="M1 {TITLE_BAR}V11a10 10 0 0 1 10-10H{WIDTH - 11}a10 10 0 0 1 10 10V{TITLE_BAR}Z" '
        f'fill="{COLORS["bar"]}"/>',
        f'<line x1="1" y1="{TITLE_BAR}" x2="{WIDTH - 1}" y2="{TITLE_BAR}" stroke="{COLORS["border"]}"/>',
        "".join(f'<circle cx="{20 + i * 20}" cy="{TITLE_BAR // 2}" r="6" fill="{dot}"/>' for i, dot in enumerate(DOTS)),
        f'<text x="{CAPTION_X}" y="{TITLE_BAR / 2 + 4.5}" fill="{COLORS["dim"]}" font-size="{CAPTION_SIZE}" '
        f'text-anchor="middle" textLength="{len(CAPTION) * CAPTION_SIZE * 0.6:.1f}" lengthAdjust="spacingAndGlyphs">'
        f"{escape(CAPTION)}</text>",
        f'<clipPath id="typed"><rect x="{PAD_X + prompt_w:.1f}" y="{TITLE_BAR}" width="0" '
        f'height="{2 * LINE_HEIGHT}">{discrete("width", clip_widths)}</rect></clipPath>',
        row(prompt(), 0),
        row([(" " * 2, "text"), (COMMAND, "text")], 0, clip="typed"),
        *(row(colour(line), i + 1, appear_ms=first_line + i * LINE_MS) for i, line in enumerate(output)),
        row(prompt(), rows - 1, appear_ms=final),
        f'<rect x="{PAD_X + prompt_w:.1f}" y="{tops[0]:.1f}" width="{char_w - 1:.1f}" height="{FONT_SIZE + 2}" '
        f'fill="{COLORS["text"]}">{discrete("x", cursor_x)}'
        f"{discrete('y', [(0, f'{tops[0]:.1f}'), (final, f'{tops[1]:.1f}')])}{discrete('opacity', cursor_on)}</rect>",
    ]
    out.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{h}" viewBox="0 0 {WIDTH} {h}" '
        f'font-family="DejaVu Sans Mono, Menlo, Consolas, Liberation Mono, monospace" font-size="{FONT_SIZE}" '
        'xml:space="preserve">\n'
        f"<title>{escape(CAPTION)}</title>\n" + "\n".join(body) + "\n</svg>\n"
    )


# --------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--font", help="a monospace TrueType font file (default: DejaVu Sans Mono if found)")
    parser.add_argument("--frames", type=Path, help="also save every GIF frame as a PNG in this directory")
    args = parser.parse_args(argv)

    output = run_example()
    same, refunds = count_refunds()
    if same != output:
        raise SystemExit(f"the example's output differs between runs:\n{output}\n{same}")
    if refunds != 1:
        raise SystemExit(f"the refund tool ran {refunds} times, not once: the caption would be wrong")
    print("\n".join(f"  | {line}" for line in output))

    frames = timeline(output)
    font_path = find_font(args.font)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    gif, svg = args.out_dir / "safety-demo.gif", args.out_dir / "safety-demo.svg"
    images = write_gif(frames, len(output) + 2, font_path, gif)
    write_svg(output, svg)
    if args.frames is not None:
        args.frames.mkdir(parents=True, exist_ok=True)
        for i, image in enumerate(images):
            image.save(args.frames / f"frame-{i:03}.png")
    seconds = sum(ms for _, ms in frames) / 1000
    print(f"font: {font_path or 'Pillow built-in'}")
    print(f"wrote {gif} ({gif.stat().st_size / 1024:.0f} kB, {len(frames)} frames, {seconds:.1f}s)")
    print(f"wrote {svg} ({svg.stat().st_size / 1024:.0f} kB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
