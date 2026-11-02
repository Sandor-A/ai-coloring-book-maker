"""
Turns one page of the book plan into the final image prompt.

The LLM only writes *what* to draw on each page. The style, framing and avoid list live
here in code, so every page of every book gets exactly the same instructions.
"""

AGE_GROUPS = {
    "toddler": {
        "label": "toddlers (ages 2-3)",
        "opener": "Children's coloring book illustration",
        "style": ("thick, even, marker-weight black outlines; every shape closed; pure white fills; "
                  "only big, simple shapes a toddler can color with a chunky crayon; friendly board-book look"),
        "avoid_extra": "tiny details, thin lines",
        # Thinnest acceptable line, as a share of the image width. Used by the automatic check.
        "min_line": 0.006,
    },
    "preschool": {
        "label": "preschoolers (ages 4-5)",
        "opener": "Children's coloring book illustration",
        "style": ("bold, even black outlines; every shape closed; pure white fills; simple shapes with a "
                  "little detail, easy for a 4-5-year-old to color with crayons; friendly picture-book look"),
        "avoid_extra": "tiny details, very thin lines",
        "min_line": 0.004,
    },
    "kids": {
        "label": "children (ages 6-9)",
        "opener": "Children's coloring book illustration",
        "style": ("clean, medium-weight black outlines; every shape closed; pure white fills; moderate detail "
                  "with clear, colorable areas for a 6-9-year-old; cheerful cartoon look"),
        "avoid_extra": "very thin lines",
        "min_line": 0.0025,
    },
    "adult": {
        "label": "adults (relaxing, intricate detail)",
        "opener": "Adult coloring book illustration",
        "style": ("fine, elegant black outlines with rich, intricate detail; layered decorative patterns, "
                  "flowing linework and lots of small enclosed shapes to color, in the style of a calm, "
                  "relaxing adult coloring page; every shape closed; pure white fills; no shading"),
        "avoid_extra": "",
        "min_line": 0.0015,
    },
}

FRAMING = "The drawing sits in the middle of a plain white square canvas with an even white margin around it."

AVOID = ("text, letters, words, numbers, title, signature, border, frame, shading, grey tones, gradients, "
         "hatching, texture, reflections, glare, sparkles, background scenery")


def build_prompt(scene, age, character=None, with_reference=False, fixes=None):
    """The full prompt for one picture.

    character       appearance of the recurring character, if this page shows them
    with_reference  True when a reference image of the character is attached
    fixes           problems found in the previous attempt, phrased as instructions
    """
    group = AGE_GROUPS[age]
    lines = [f"{group['opener']}, flat black-and-white line art, clean digital drawing on a plain white "
             "background, square image."]
    if character:
        character = character.strip().rstrip(".")
        if with_reference:
            lines.append("The attached reference image shows the character. Keep their look exactly the same "
                         f"and draw them in the same style: {character}.")
        else:
            lines.append(f"The character: {character}.")
    lines.append(scene.strip())
    lines.append(f"Style: {group['style']}. {FRAMING}")
    avoid = AVOID + (f", {group['avoid_extra']}" if group["avoid_extra"] else "")
    lines.append(f"Avoid: {avoid}.")
    if fixes:
        lines.append("Important: the previous attempt had these problems, so fix them: " + "; ".join(fixes) + ".")
    return "\n".join(lines)
