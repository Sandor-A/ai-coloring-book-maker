"""
Builds the printable A4 PDF from the plan and the finished pictures.

Each page gets the cleaned-up drawing (cropped, upscaled to 300 dpi, pure black and white),
a hollow outline letter at the top for ABC books (kids can color it in), and the caption
underneath, all in one font. The cover gets the title, the cover picture and a
"This book belongs to" line.
"""

import sys
from pathlib import Path

from PIL import Image, ImageFilter, ImageFont, ImageOps
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFError, TTFont
from reportlab.pdfgen import canvas

from qa import INK_LEVEL

# Page layout in millimetres (A4 is 210 x 297).
MARGIN = 15          # white border around everything
LETTER_H = 55        # hollow letter area at the top (ABC pages only)
CAPTION_H = 20       # caption area at the bottom
GAP = 7              # space between the drawing and the text
OUTLINE_MM = 2.5     # outline thickness of hollow letters and the title
DPI = 300
PRINT_BW_LEVEL = 150 # final black/white cutoff, after upscaling and blur (deliberately different from INK_LEVEL)

FONT_CANDIDATES = [
    Path(__file__).with_name("Fredoka-Bold.ttf"),
    Path.cwd() / "Fredoka-Bold.ttf",
    Path("C:/Windows/Fonts/ARLRDBD.TTF"),                              # Arial Rounded MT Bold
    Path("/Library/Fonts/Arial Rounded Bold.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Rounded Bold.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]

PAGE_W, PAGE_H = A4


# ------------------------------------------------------------------ fonts

class BookFont:
    """One TrueType font, used for drawing (reportlab) and for measuring glyph shapes (Pillow)."""

    NAME = "BookFont"

    def __init__(self, path):
        try:
            pdfmetrics.registerFont(TTFont(self.NAME, str(path)))
        except TTFError as err:
            sys.exit(f"Could not load the font {path}:\n  {err}\n"
                     "Use a static TrueType .ttf file (not .otf, not a variable font).")
        self._measure = ImageFont.truetype(str(path), 1000, layout_engine=ImageFont.Layout.BASIC)

    def ink_box(self, text):
        """Visible ink of `text` at size 1, relative to its baseline start, y up: (left, bottom, right, top)."""
        x0, y0, x1, y1 = self._measure.getbbox(text, anchor="ls")
        return x0 / 1000, -y1 / 1000, x1 / 1000, -y0 / 1000

    def metrics(self, text):
        """(width, x_shift, bottom, top) at size 1. A single letter is measured by its real shape so
        it sits dead centre; a word by capital height, so all captions share one baseline and a
        letter with a tail (the Q in QUEST) hangs below instead of shrinking the word."""
        left, bottom, right, top = self.ink_box(text)
        if len(text) == 1:
            return right - left, left, bottom, top
        _, cap_bottom, _, cap_top = self.ink_box("H")
        return pdfmetrics.stringWidth(text, self.NAME, 1), 0.0, cap_bottom, cap_top


def find_font(user_path=None):
    if user_path:
        path = Path(user_path)
        if not path.is_file():
            sys.exit(f"Font file not found: {path}")
        return path
    for path in FONT_CANDIDATES:
        if path.is_file():
            return path
    sys.exit("No font found. Download Fredoka from fonts.google.com and pass --font path/to/Fredoka-Bold.ttf")


def fit_size(font, text, width, height, stroke=0.0):
    """Largest font size at which `text` fits in width x height points (a hollow outline sticks out
    by half its thickness on each side, hence `stroke`)."""
    text_w, _, bottom, top = font.metrics(text)
    return min((height - stroke) / (top - bottom), (width - stroke) / text_w)


def draw_text(c, font, text, box, size, hollow=False, stroke=0.0):
    x, y, w, h = box
    text_w, x_shift, bottom, top = font.metrics(text)
    c.saveState()
    t = c.beginText()
    t.setFont(font.NAME, size)
    t.setTextOrigin(x + (w - text_w * size) / 2 - x_shift * size,
                    y + (h - (top - bottom) * size) / 2 - bottom * size)
    if hollow:
        c.setStrokeColorRGB(0, 0, 0)
        c.setLineWidth(stroke)
        c.setLineJoin(1)
        c.setLineCap(1)
        t.setTextRenderMode(1)              # outline only, white inside
    else:
        c.setFillColorRGB(0, 0, 0)
    t.textOut(text)
    c.drawText(t)
    c.restoreState()


# ------------------------------------------------------------------ artwork

def clean_art(path, box_w, box_h):
    """Crop to the drawing, upscale to its printed size at 300 dpi, and force pure black/white."""
    img = ImageOps.exif_transpose(Image.open(path))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        white = Image.new("RGBA", img.size, "white")
        white.alpha_composite(img)
        img = white
    gray = img.convert("L")
    bbox = gray.point(lambda v: 255 if v < INK_LEVEL else 0).getbbox()
    if bbox is None:
        raise ValueError("the image is blank")
    pad = int(0.02 * max(bbox[2] - bbox[0], bbox[3] - bbox[1]))
    gray = gray.crop((max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                      min(gray.width, bbox[2] + pad), min(gray.height, bbox[3] + pad)))
    scale = min(box_w / 72 * DPI / gray.width, box_h / 72 * DPI / gray.height)
    big = gray.resize((round(gray.width * scale), round(gray.height * scale)), Image.LANCZOS)
    big = big.filter(ImageFilter.GaussianBlur(radius=max(1.0, 0.6 * scale)))
    return big.point(lambda v: 0 if v < PRINT_BW_LEVEL else 255)


def draw_art(c, path, box):
    x, y, w, h = box
    bw = clean_art(path, w, h)
    draw_w, draw_h = bw.width / DPI * 72, bw.height / DPI * 72
    c.drawImage(ImageReader(bw), x + (w - draw_w) / 2, y + (h - draw_h) / 2, draw_w, draw_h)


# ------------------------------------------------------------------ pages

def draw_cover(c, font, title, age, art_path):
    x, w = MARGIN * mm, PAGE_W - 2 * MARGIN * mm
    stroke = OUTLINE_MM * mm
    lines = [line.strip() for line in title.split("|") if line.strip()]

    def layout_for(lines):
        line_h = 38 * mm if len(lines) > 1 else 50 * mm
        return lines, line_h, min(fit_size(font, line, w, line_h, stroke) for line in lines)

    best = layout_for(lines)
    words = lines[0].split()
    if len(lines) == 1 and len(words) > 1:      # a long one-line title may print bigger on two lines
        for cut in range(1, len(words)):
            option = layout_for([" ".join(words[:cut]), " ".join(words[cut:])])
            if option[2] > best[2]:
                best = option
    lines, line_h, size = best
    top = PAGE_H - MARGIN * mm
    for i, line in enumerate(lines):
        draw_text(c, font, line, (x, top - (i + 1) * line_h - i * 3 * mm, w, line_h), size,
                  hollow=True, stroke=stroke)
    title_bottom = top - len(lines) * line_h - (len(lines) - 1) * 3 * mm

    # "This book belongs to" fits a child's own copy; a relaxing adult book skips it and
    # gives that space back to the cover art instead.
    art_bottom = MARGIN * mm
    if age != "adult":
        name_line_y = (MARGIN + 8) * mm
        label = (x, name_line_y + 6 * mm, w, 8 * mm)
        draw_text(c, font, "This book belongs to:", label, fit_size(font, "H", w, 8 * mm))
        c.setLineWidth(1.5)
        c.line(x + 30 * mm, name_line_y, x + w - 30 * mm, name_line_y)
        art_bottom = label[1] + label[3] + GAP * mm

    if art_path:
        draw_art(c, art_path, (x, art_bottom, w, title_bottom - GAP * mm - art_bottom))


def draw_page(c, font, page, art_path):
    x, w = MARGIN * mm, PAGE_W - 2 * MARGIN * mm
    caption_box = (x, MARGIN * mm, w, CAPTION_H * mm)
    art_top = PAGE_H - MARGIN * mm
    if page["letter"]:
        stroke = OUTLINE_MM * mm
        letter_box = (x, PAGE_H - (MARGIN + LETTER_H) * mm, w, LETTER_H * mm)
        draw_text(c, font, page["letter"], letter_box,
                  fit_size(font, page["letter"], w, letter_box[3], stroke), hollow=True, stroke=stroke)
        art_top = letter_box[1] - GAP * mm
    draw_text(c, font, page["caption"], caption_box, fit_size(font, page["caption"], w, caption_box[3]))
    if art_path:
        art_bottom = (MARGIN + CAPTION_H + GAP) * mm
        draw_art(c, art_path, (x, art_bottom, w, art_top - art_bottom))


def build_pdf(plan, art_dir, out_path, font_path=None, blank_backs=False, cover=True):
    """Write the PDF. Returns (keys with no picture file, [(key, error) for pages that
    had a picture but couldn't be placed]). A page that fails still gets its letter/caption
    and a showPage(), so one bad picture never loses the rest of the book."""
    font = BookFont(find_font(font_path))
    c = canvas.Canvas(str(out_path), pagesize=A4)
    c.setTitle(plan["title"].replace("|", " "))
    missing, failed = [], []

    def art_for(key):
        path = Path(art_dir) / f"{key}.png"
        if path.is_file():
            return path
        missing.append(key)
        return None

    def finish():
        c.showPage()
        if blank_backs:
            c.showPage()

    def safe(key, fn, *fn_args):
        try:
            fn(*fn_args)
        except Exception as err:
            failed.append((key, f"{type(err).__name__}: {err}"))
        finish()

    if cover:
        safe("cover", draw_cover, c, font, plan["title"], plan["age"], art_for("cover"))
    for page in plan["pages"]:
        safe(page["key"], draw_page, c, font, page, art_for(page["key"]))
    c.save()
    return missing, failed
