"""
Checks every generated picture before it goes into the book.

1. Pixel checks (free, instant): blank image, a border around the picture,
   grey shading / big black areas, and lines too thin for the age group.
2. Vision check (one cheap AI call per picture, optional): text or letters in the
   picture, shading, tiny details, and whether it actually shows the planned scene.

Problems are written as instructions, because they're fed straight back into the
prompt when the picture is regenerated.
"""

import io

import numpy as np
from PIL import Image

from prompts import AGE_GROUPS

INK_LEVEL = 160          # anything darker than this counts as ink


def load_gray(image_bytes):
    img = Image.open(io.BytesIO(image_bytes))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        white = Image.new("RGBA", img.size, "white")
        white.alpha_composite(img)
        img = white
    return img.convert("L")


def run_lengths(mask):
    """Lengths of every unbroken run of ink along the rows of a boolean array."""
    padded = np.pad(mask.astype(np.int8), ((0, 0), (1, 1)))
    changes = np.diff(padded, axis=1)
    starts = np.argwhere(changes == 1)
    ends = np.argwhere(changes == -1)
    return ends[:, 1] - starts[:, 1]


def pixel_issues(image_bytes, age):
    gray = load_gray(image_bytes)
    ink = np.asarray(gray) < INK_LEVEL
    height, width = ink.shape
    if ink.sum() < 0.002 * ink.size:
        return ["the picture is blank; draw the described scene"]

    issues = []
    # A frame shows up as ink along almost the whole length of all four edges.
    strip = max(2, int(0.02 * min(height, width)))
    edges = [ink[:strip, :].any(axis=0), ink[-strip:, :].any(axis=0),
             ink[:, :strip].any(axis=1), ink[:, -strip:].any(axis=1)]
    if all(edge.mean() > 0.9 for edge in edges):
        issues.append("remove the border or frame around the picture")

    # Inside the drawing, too much solid black means shading or filled areas.
    rows, cols = np.where(ink)
    drawing = ink[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    if drawing.mean() > 0.30:
        issues.append("remove grey shading and large solid black areas; keep only outlines "
                      "with white inside every shape")

    # Typical line thickness: the median width of ink runs, across and down the image.
    runs = np.concatenate([run_lengths(ink[::3, :]), run_lengths(ink[:, ::3].T)])
    if runs.size == 0:
        # The sparse sample missed every line; fall back to the full-resolution scan.
        runs = np.concatenate([run_lengths(ink), run_lengths(ink.T)])
    thickness = np.median(runs) / width if runs.size else 0.0
    if thickness < AGE_GROUPS[age]["min_line"]:
        issues.append("make all outlines much thicker and bolder, and drop small details")
    return issues


VISION_SYSTEM = """You check pages of a coloring book before they're printed.
Reply with JSON only: {"pass": true or false, "problems": ["...", "..."]}
Fail the picture for any of these:
- any text, letters, numbers or signature anywhere in the picture
- grey shading, gradients, hatching, texture, or large solid black areas (small solid black details are fine)
- lines too thin or details too small for the stated age group (adult pages are meant to have fine,
  intricate detail and thin lines; only fail those for genuinely illegible micro-detail)
- a border or frame around the picture, or a background scene that wasn't asked for
- the main subject is missing or clearly different from the description
Don't fail it for small artistic differences. Write each problem as a short instruction an
artist can act on, for example "remove the word SPACE at the top". If it passes, problems is []."""


def vision_issues(text_provider, image_bytes, scene, age, character=None):
    prompt = (f"Age group: {AGE_GROUPS[age]['label']}.\n"
              f"The picture should show: {scene}")
    if character:
        prompt += f"\nThe character looks like this: {character}"
    verdict = text_provider.ask_json(VISION_SYSTEM, prompt, image=image_bytes, purpose="qa", max_tokens=800)
    if verdict.get("pass", False):
        return []
    return [str(p) for p in verdict.get("problems") or []] or ["the picture didn't pass the review; redraw it"]
