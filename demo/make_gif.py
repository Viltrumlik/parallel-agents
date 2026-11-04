"""Render demo/demo.gif from a real run of pa.py.

Every line of output in the GIF is what the CLI actually printed. Two agents
share one temporary git repository; the left pane is one agent, the right pane
the other.

    python3 demo/make_gif.py            # needs Pillow
"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
PA = os.path.join(HERE, os.pardir, "skills", "parallel-agents", "scripts", "pa.py")
OUT = os.path.join(HERE, "demo.gif")

W, H = 1280, 600
PANE_W = (W - 3 * 16) // 2
FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "/System/Library/Fonts/Menlo.ttc"]
BG, PANE, TEXT, DIM = (13, 17, 23), (22, 27, 34), (230, 237, 243), (125, 133, 144)
GREEN, AMBER, RED, BLUE = (63, 185, 80), (210, 153, 34), (248, 81, 73), (88, 166, 255)

AGENTS = [("claude-7f2c", GREEN), ("codex-a91e", AMBER)]
STEPS = [
    (0, 'pa claim src/billing/ --as claude-7f2c --why "add refund endpoint"'),
    (1, 'pa claim src/billing/views.py --as codex-a91e --why "fix currency rounding"'),
    (1, 'pa claim tests/billing/ --as codex-a91e --why "write billing tests"'),
    (0, 'pa note "refunds done, tests still missing" --as claude-7f2c'),
    (0, "pa release --as claude-7f2c"),
    (1, 'pa claim src/billing/views.py --as codex-a91e --why "fix currency rounding"'),
    (1, "pa log -n 1"),
]


def font(size):
    for path in FONT_PATHS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make_repo():
    tmp = tempfile.mkdtemp(prefix="pa-demo-")
    for rel in ("src/billing/views.py", "src/billing/models.py", "tests/billing/__init__.py"):
        path = os.path.join(tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w").close()
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    return tmp


def run_step(repo, command):
    import shlex
    argv = shlex.split(command)[1:]  # drop the "pa"
    out = subprocess.run([sys.executable, PA, *argv], cwd=repo, capture_output=True, text=True)
    return (out.stdout + out.stderr).rstrip("\n"), out.returncode


def wrap(line, width):
    indent = len(line) - len(line.lstrip())
    return textwrap.wrap(line, width, subsequent_indent=" " * (indent + 2)) or [""]


def color_for(line, code):
    if line.startswith("BLOCKED") or line.startswith("TAKEN"):
        return RED
    if line.startswith("OK") or line.startswith("FREE"):
        return GREEN
    if line.startswith("WARN"):
        return AMBER
    return TEXT if code == 0 else DIM


class Renderer:
    def __init__(self):
        self.f = font(16)
        self.small = font(14)
        self.cw = self.f.getbbox("M")[2]
        self.lh = 22
        self.cols = (PANE_W - 28) // self.cw
        self.rows = (H - 120) // self.lh
        self.panes = [[], []]  # list of (text, color)
        self.frames, self.durations = [], []

    def push(self, pane, text, color):
        for part in wrap(text, self.cols):
            self.panes[pane].append((part, color))
        self.panes[pane] = self.panes[pane][-self.rows:]

    def draw(self, typing=None):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.text((16, 14), "parallel-agents", font=self.f, fill=TEXT)
        d.text((16 + self.cw * 16, 14), "two coding agents, one repository", font=self.f, fill=DIM)
        for i, (name, color) in enumerate(AGENTS):
            x = 16 + i * (PANE_W + 16)
            d.rounded_rectangle((x, 48, x + PANE_W, H - 16), 10, fill=PANE)
            d.ellipse((x + 14, 62, x + 26, 74), fill=color)
            d.text((x + 34, 58), "agent " + name, font=self.small, fill=TEXT)
            y = 92
            lines = list(self.panes[i])
            if typing and typing[0] == i:
                for part in wrap("$ " + typing[1] + "█", self.cols):
                    lines.append((part, BLUE))
                lines = lines[-self.rows:]
            for text, c in lines:
                d.text((x + 14, y), text, font=self.f, fill=c)
                y += self.lh
        return img

    def frame(self, ms, typing=None):
        self.frames.append(self.draw(typing))
        self.durations.append(ms)

    def save(self, path):
        # Build the palette from the last frame plus big swatches of every theme
        # color, so no accent color gets merged into a neighbour.
        swatches = [BG, PANE, TEXT, DIM, GREEN, AMBER, RED, BLUE]
        src = Image.new("RGB", (W, H + 80 * len(swatches)), BG)
        src.paste(self.frames[-1], (0, 0))
        for i, c in enumerate(swatches):
            ImageDraw.Draw(src).rectangle((0, H + 80 * i, W, H + 80 * (i + 1)), fill=c)
        palette = src.quantize(colors=64, method=Image.Quantize.MEDIANCUT)
        frames = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in self.frames]
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=self.durations,
                       loop=0, optimize=True, disposal=1)


def main():
    repo = make_repo()
    r = Renderer()
    try:
        r.frame(900)
        for pane, command in STEPS:
            for n in range(0, len(command) + 1, 4):
                r.frame(45, typing=(pane, command[:n]))
            r.frame(350, typing=(pane, command))
            output, code = run_step(repo, command)
            r.push(pane, "$ " + command, BLUE)
            for line in output.splitlines():
                r.push(pane, line, color_for(line, code))
            r.push(pane, "", TEXT)
            r.frame(2200 if code else 1400)
        r.frame(3000)
        r.save(OUT)
    finally:
        shutil.rmtree(repo, ignore_errors=True)
    print("wrote %s (%d frames, %.0f KB)" % (OUT, len(r.frames), os.path.getsize(OUT) / 1024))


if __name__ == "__main__":
    main()
