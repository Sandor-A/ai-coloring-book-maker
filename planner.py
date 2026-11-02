"""
Turns a request like "space ABC coloring book for my 2-year-old" into a book plan:
title, age group, an optional recurring character, and one scene description per page.

The plan is saved as plan.json in the book folder, so you can read it, edit it and
re-run with --resume before any money is spent on pictures.
"""

import re
import string

from prompts import AGE_GROUPS

MAX_PAGES = 40

SYSTEM = """You plan coloring books for people of any age, from toddlers to adults looking for a relaxing,
detailed coloring experience. Turn the request into a JSON plan for an image generator.
Every picture is generated separately as black-and-white line art, so each scene must stand on its own.

Reply with JSON only, no markdown, in exactly this shape:
{
  "title": "SHORT TITLE IN CAPITALS, use | to split it over at most 2 lines",
  "age": "toddler" | "preschool" | "kids" | "adult",
  "kind": "abc" | "themed",
  "character": "appearance of the recurring character, or null",
  "cover_scene": "what to draw on the cover",
  "pages": [
    {"letter": "A" or null, "caption": "ASTRONAUT", "scene": "what to draw", "features_character": true or false}
  ]
}

Rules:
- Age groups: toddler = ages 2-3, preschool = 4-5, kids = 6-9, adult = relaxing, intricately detailed pages for
  adults (mandalas, botanicals, patterns, nature, calm scenes). Use the age the request states or implies
  (words like "adult", "relaxing", "grown-up", "mandala", "intricate" imply adult); default to toddler.
- An ABC book has exactly 26 pages, A to Z. Each caption is one word or a short name, at most 14 characters, in capitals, starting with the page's letter. (ABC books are for toddlers/preschool/kids, not adult.)
- A themed book has letter null on every page and 8 to 20 pages unless the request gives a number. Each caption names the picture, at most 14 characters, in capitals.
- Each scene is 40 to 90 words (up to 130 for adult pages, to describe layered pattern detail) and describes
  only what to draw. For toddler/preschool/kids: one main subject, its pose and a few big parts, plus at most
  two or three supporting objects, with counts ("three big stars"); for toddlers every part must be big enough
  to color with a chunky crayon. For adult: a rich, calming composition (e.g. a detailed mandala, a botanical
  arrangement, an intricate animal or landscape) with many layered elements to color, described in enough
  detail to guide fine linework.
- Describe what is there, not what to leave out ("the visor is plain white inside", not "no reflections").
- Never describe drawing style, line weight, colors, shading or background; those are added later. Never put text, letters, numbers, signs or labels in a picture.
- Make the pages clearly different from each other: vary the main subject, its shape and the composition.
- If a character appears on more than one page, describe their appearance once in "character" (30 to 60 words), set features_character to true on those pages, and call them "the character" in those scenes. Otherwise set character to null.
- Everything must be original. Never use trademarked or copyrighted characters, brands or logos. If the request names one, invent an original character with a similar feel and don't mention the name.
- The book must be wholesome and suit its age group: nothing violent, sexual, frightening or otherwise
  inappropriate at any age. If the request is not suitable for a coloring book, reply only with
  {"error": "short reason"}."""


class PlanError(Exception):
    pass


def make_plan(text_provider, request, age=None, pages=None):
    prompt = f"Request: {request}"
    if age:
        prompt += f"\nAge group: {age} (use this one)."
    if pages:
        prompt += f"\nNumber of pages: {pages} (unless it's an ABC book, which always has 26)."
    plan = text_provider.ask_json(SYSTEM, prompt, purpose="plan", max_tokens=12000)
    if "error" in plan:
        raise PlanError(f"The planner declined this request: {plan['error']}")
    if age:
        plan["age"] = age
    return validate_plan(plan)


def validate_plan(plan):
    """Check and tidy a plan (from the model or edited by hand). Returns (plan, warnings)."""
    warnings = []

    def text(value):
        return value.strip() if isinstance(value, str) else ""

    title = text(plan.get("title")).upper()
    if not title:
        raise PlanError("The plan has no title.")
    title_lines = [line for line in (part.strip() for part in title.split("|")) if line]
    if len(title_lines) > 2:
        warnings.append(f"title had {len(title_lines)} lines; keeping the first and joining the rest into a second")
        title_lines = [title_lines[0], " ".join(title_lines[1:])]
        title = "|".join(title_lines)
    age = text(plan.get("age")).lower()
    if age not in AGE_GROUPS:
        warnings.append(f"unknown age group {age!r}; using toddler")
        age = "toddler"
    character = text(plan.get("character")) or None

    raw_pages = plan.get("pages")
    if not isinstance(raw_pages, list) or not raw_pages:
        raise PlanError("The plan has no pages.")
    if len(raw_pages) > MAX_PAGES:
        raise PlanError(f"The plan has {len(raw_pages)} pages; the limit is {MAX_PAGES}.")

    pages, used_keys = [], set()
    for number, page in enumerate(raw_pages, start=1):
        caption = text(page.get("caption")).upper()
        scene = text(page.get("scene"))
        letter = text(page.get("letter")).upper() or None
        if not caption or not scene:
            raise PlanError(f"Page {number} needs both a caption and a scene.")
        if letter and not re.fullmatch(r"[A-Z]", letter):
            raise PlanError(f"Page {number}: the letter must be a single A-Z capital, not {letter!r}.")
        if letter and not caption.startswith(letter):
            warnings.append(f"page {letter}: caption {caption!r} doesn't start with {letter}")
        if len(caption) > 14:
            warnings.append(f"page {number}: caption {caption!r} is long and will print small")

        key = letter.lower() if letter else f"{number:02d}"
        if key in used_keys:
            key = f"{key}{number}"
        used_keys.add(key)
        pages.append({"key": key, "letter": letter, "caption": caption, "scene": scene,
                      "features_character": bool(page.get("features_character")) and character is not None})

    kind = "abc" if all(p["letter"] for p in pages) else "themed"
    if kind == "abc":
        letters = [p["letter"] for p in pages]
        missing_letters = sorted(set(string.ascii_uppercase) - set(letters))
        dupes = sorted({l for l in letters if letters.count(l) > 1})
        if missing_letters or dupes or len(pages) != 26:
            bits = []
            if len(pages) != 26:
                bits.append(f"{len(pages)} pages instead of 26")
            if missing_letters:
                bits.append("missing " + ",".join(missing_letters))
            if dupes:
                bits.append("duplicated " + ",".join(dupes))
            warnings.append("ABC book doesn't cleanly cover A-Z (" + "; ".join(bits) + ")")

    clean = {
        "title": title,
        "age": age,
        "kind": kind,
        "character": character,
        "cover_scene": text(plan.get("cover_scene")) or "A friendly scene showing the book's main theme, "
                                                         "with one big main subject and two or three supporting objects.",
        "pages": pages,
    }
    return clean, warnings
