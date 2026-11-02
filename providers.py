"""
Thin wrappers around the AI services, so the rest of the program doesn't care which one is used.

Text providers  (plan the book, check the pictures):  anthropic, gemini, mock
Image providers (draw the pictures):                  openai, gemini, mock

"mock" needs no API key and costs nothing: it uses the example space ABC plan and draws
placeholder shapes. Use it to try the whole pipeline before spending money.

Model names change often. Override them with --text-model / --image-model if a default
below has been retired.
"""

import base64
import io
import json
import math
import os
import random
import re
import time
from pathlib import Path

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "gemini-text": "gemini-flash-latest",
    "openai-image": "gpt-image-2",
    "gemini-image": "gemini-3.1-flash-image",
}

EXAMPLE_PLAN = Path(__file__).with_name("examples") / "space_abc_plan.json"


# ------------------------------------------------------------------ helpers

def with_retries(call, what, attempts=3):
    """Run call(), retrying temporary failures (network, rate limits, server errors).
    Mistakes on our side (bad key, bad request) are raised straight away."""
    delay = 4
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as err:
            status = getattr(err, "status_code", None) or getattr(err, "code", None)
            name = type(err).__name__.lower()
            permanent = (isinstance(status, int) and 400 <= status < 500 and status != 429) or any(
                word in name for word in ("auth", "permission", "invalid", "notfound", "badrequest"))
            if permanent or attempt == attempts:
                raise
            print(f"    {what} failed ({type(err).__name__}); retrying in {delay}s")
            time.sleep(delay)
            delay *= 3


def parse_json(text):
    """Pull the JSON object out of a model reply, even if it's wrapped in ``` fences or chatter."""
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"the model did not return JSON:\n{text[:500]}")
    return json.loads(text[start:end + 1])


def png_bytes(image_bytes, max_side=None):
    """Re-encode any image as PNG, optionally shrinking it (cheaper to send for checking)."""
    from PIL import Image
    img = Image.open(io.BytesIO(image_bytes))
    if max_side and max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def has_key(*names):
    return any(os.environ.get(n) for n in names)


# ------------------------------------------------------------ text providers

class AnthropicText:
    def __init__(self, model=None):
        import anthropic
        self.client = anthropic.Anthropic()          # reads ANTHROPIC_API_KEY
        self.model = model or DEFAULT_MODELS["anthropic"]

    def ask_json(self, system, prompt, image=None, purpose="", max_tokens=8000):
        content = []
        if image:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.b64encode(png_bytes(image, 1024)).decode()}})
        content.append({"type": "text", "text": prompt})

        def call():
            msg = self.client.messages.create(model=self.model, max_tokens=max_tokens, system=system,
                                              messages=[{"role": "user", "content": content}])
            return "".join(block.text for block in msg.content if block.type == "text")
        return parse_json(with_retries(call, "Claude request"))


class GeminiText:
    def __init__(self, model=None):
        from google import genai
        self.client = genai.Client()                 # reads GEMINI_API_KEY or GOOGLE_API_KEY
        self.model = model or DEFAULT_MODELS["gemini-text"]

    def ask_json(self, system, prompt, image=None, purpose="", max_tokens=8000):
        from google.genai import types
        contents = []
        if image:
            contents.append(types.Part.from_bytes(data=png_bytes(image, 1024), mime_type="image/png"))
        contents.append(prompt)
        config = types.GenerateContentConfig(system_instruction=system, max_output_tokens=max_tokens,
                                             response_mime_type="application/json")

        def call():
            return self.client.models.generate_content(model=self.model, contents=contents, config=config).text
        return parse_json(with_retries(call, "Gemini request"))


class MockText:
    """Offline stand-in: returns the example plan, and passes every picture check."""

    def ask_json(self, system, prompt, image=None, purpose="", max_tokens=0):
        if purpose == "plan":
            return json.loads(EXAMPLE_PLAN.read_text(encoding="utf-8"))
        return {"pass": True, "problems": []}


# ----------------------------------------------------------- image providers

class OpenAIImages:
    def __init__(self, model=None, quality="high"):
        from openai import OpenAI
        self.client = OpenAI()                       # reads OPENAI_API_KEY
        self.model = model or DEFAULT_MODELS["openai-image"]
        self.quality = quality

    def generate(self, prompt, reference=None):
        def call():
            if reference:
                with open(reference, "rb") as ref:
                    result = self.client.images.edit(model=self.model, image=[ref], prompt=prompt,
                                                     size="1024x1024", quality=self.quality)
            else:
                result = self.client.images.generate(model=self.model, prompt=prompt, size="1024x1024",
                                                     quality=self.quality, n=1)
            return base64.b64decode(result.data[0].b64_json)
        return with_retries(call, "OpenAI image")


class GeminiImages:
    def __init__(self, model=None, quality=None):
        from google import genai
        self.client = genai.Client()
        self.model = model or DEFAULT_MODELS["gemini-image"]

    def generate(self, prompt, reference=None):
        from google.genai import types
        from PIL import Image
        contents = [prompt]
        if reference:
            contents.append(Image.open(reference))
        config = types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"],
                                             image_config=types.ImageConfig(aspect_ratio="1:1"))

        def call():
            response = self.client.models.generate_content(model=self.model, contents=contents, config=config)
            for candidate in response.candidates or []:
                for part in (candidate.content.parts if candidate.content else None) or []:
                    if part.inline_data is not None and part.inline_data.data:
                        data = part.inline_data.data
                        return base64.b64decode(data) if isinstance(data, str) else data
            raise RuntimeError("Gemini returned no image (it may have declined the prompt)")
        return with_retries(call, "Gemini image")


class MockImages:
    """Offline stand-in: draws a placeholder with thick outlines (a circle and a star)."""

    def __init__(self, model=None, quality=None):
        pass

    def generate(self, prompt, reference=None):
        from PIL import Image, ImageDraw
        rng = random.Random(prompt)
        size, scale = 1024, 2
        img = Image.new("L", (size * scale, size * scale), 255)
        draw = ImageDraw.Draw(img)
        s = size * scale
        r = rng.randint(int(s * 0.18), int(s * 0.26))
        cx, cy = rng.randint(int(s * 0.38), int(s * 0.62)), int(s * 0.42)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=0, width=40)
        points = []
        for i in range(10):
            radius = s * (0.13 if i % 2 == 0 else 0.055)
            angle = -1.5708 + i * 0.6283
            points.append((s * 0.5 + radius * math.cos(angle),
                           s * 0.78 + radius * math.sin(angle)))
        draw.polygon(points, outline=0, width=40)
        img = img.resize((size, size))
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()


# ------------------------------------------------------------------ factory

def make_text_provider(name=None, model=None):
    if name is None:
        if has_key("ANTHROPIC_API_KEY"):
            name = "anthropic"
        elif has_key("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            name = "gemini"
        else:
            raise SystemExit("No API key found for planning. Set ANTHROPIC_API_KEY or GEMINI_API_KEY, "
                             "or try it offline with --text mock --images mock.")
    return name, {"anthropic": AnthropicText, "gemini": GeminiText,
                  "mock": lambda model=None: MockText()}[name](model=model)


def make_image_provider(name=None, model=None, quality="high"):
    if name is None:
        if has_key("OPENAI_API_KEY"):
            name = "openai"
        elif has_key("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            name = "gemini"
        else:
            raise SystemExit("No API key found for drawing. Set OPENAI_API_KEY or GEMINI_API_KEY, "
                             "or try it offline with --text mock --images mock.")
    return name, {"openai": OpenAIImages, "gemini": GeminiImages,
                  "mock": MockImages}[name](model=model, quality=quality)
