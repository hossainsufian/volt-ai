"""Image creation for Volt AI, through Pollinations.ai.

Without a key Volt uses Pollinations' free anonymous service (one basic model,
about one image every 45 seconds). With a free Pollinations account key
(enter.pollinations.ai, an ``sk_`` key) it uses the good models and can edit
an existing image. The key is stored in ~/.config/volt-ai/pollinations_key
(0600) and is never backed up.

Image descriptions are sent to Pollinations' servers; the Settings switch says so.
"""

from __future__ import annotations

import base64
import os
import re
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

CLASSIC_URL = "https://image.pollinations.ai/prompt/"
GEN_URL = "https://gen.pollinations.ai"
KEY_PAGE = "https://enter.pollinations.ai/keys"

# Models a free Pollinations account can use (not "paid only" in /image/models).
MODELS = {
    "tongyi-mai/z-image-turbo": "Z-Image Turbo (fast, great photos)",
    "black-forest-labs/flux.1-schnell": "FLUX.1 schnell (fastest)",
    "black-forest-labs/flux.2-klein-4b": "FLUX.2 klein (sharp, good with text)",
    "microsoft/mai-image-2.6-flash": "MAI Image 2.6 Flash",
    "black-forest-labs/flux.1.1-pro": "FLUX 1.1 Pro (best detail, uses more pollen)",
}
DEFAULT_MODEL = "tongyi-mai/z-image-turbo"
EDIT_MODEL = "black-forest-labs/flux.1-kontext-pro"  # made for "change this picture" requests

STYLES = {
    "auto": ("✨ Any style", ""),
    "photo": ("📷 Realistic photo", "ultra realistic photograph, natural light, sharp focus, fine detail"),
    "anime": ("🌸 Anime", "anime style illustration, vibrant colours, clean line art, studio quality"),
    "3d": ("🧊 3D render", "3D render, soft studio lighting, global illumination, high detail"),
    "digital": ("🎨 Digital art", "digital painting, concept art, rich colours, highly detailed"),
    "oil": ("🖼 Oil painting", "oil painting on canvas, visible brush strokes, rich colours"),
    "watercolor": ("💧 Watercolour", "watercolour painting, soft washes, paper texture"),
    "pixel": ("👾 Pixel art", "pixel art, 16-bit game style, crisp pixels, limited palette"),
    "sketch": ("✏️ Pencil sketch", "detailed pencil sketch, hatching, white paper"),
    "logo": ("🔷 Logo (picture)", "minimal flat vector logo mark, simple bold shapes, centred, plain white background"),
    "icon": ("🏷 Sticker / icon", "cute sticker icon, bold outline, flat colours, white background"),
    "anatomy": ("🫀 Anatomy illustration", "scientific medical illustration, anatomically accurate, textbook style, clean white background"),
}
# Drawn by Volt's text brain as SVG, so labels and text come out right.
VECTOR_STYLES = {
    "diagram": "📐 Diagram (correct labels)",
    "vector_logo": "🔤 Logo with text (vector)",
}
SHAPES = {
    "square": ("⬛ Square", 1024, 1024),
    "portrait": ("📱 Portrait", 768, 1344),
    "landscape": ("🖥 Landscape", 1344, 768),
}

ENHANCE_PROMPT = (
    "You write prompts for an AI image generator. Rewrite the user's idea as ONE vivid, "
    "specific image description in English (40 to 80 words): the subject, setting, "
    "composition, lighting, colours and mood. Keep everything the user asked for and add "
    "nothing they didn't imply. Reply with the description only: no quotes, no title, no "
    "explanation."
)

DIAGRAM_PROMPT = (
    "Draw what the user asks for as ONE complete, self-contained SVG image in a single ```svg "
    "code block. Use a viewBox, clear shapes and readable text labels, and make every label "
    "accurate. For electronic circuits use standard schematic symbols with component names "
    "and values; for anatomy, label each part with a thin leader line; for charts and "
    "flowcharts, keep a clean layout. Start with one short sentence, then the SVG."
)
VECTOR_LOGO_PROMPT = (
    "Design the logo the user asks for as ONE complete, self-contained SVG in a single ```svg "
    "code block: a clean, modern mark with the exact name or text they give, spelled "
    "correctly, balanced spacing, at most three colours, on a transparent background. Start "
    "with one short sentence, then the SVG."
)


class ImageError(Exception):
    """Image creation failed; the message is shown to the user."""


class RateLimited(ImageError):
    def __init__(self, seconds: int, message: str):
        super().__init__(message)
        self.seconds = seconds


# --- Key ---------------------------------------------------------------------------

def key_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "volt-ai" / "pollinations_key"


def load_key() -> Optional[str]:
    try:
        return key_path().read_text().strip() or None
    except OSError:
        return None


def save_key(key: str) -> None:
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key.strip())
    tmp.replace(path)


def clear_key() -> None:
    try:
        key_path().unlink()
    except FileNotFoundError:
        pass


def masked_key() -> Optional[str]:
    key = load_key()
    return (key[:5] + "…" + key[-3:]) if key else None


# --- Prompts -----------------------------------------------------------------------

def styled_prompt(prompt: str, style: str) -> str:
    extra = STYLES.get(style, ("", ""))[1]
    return f"{prompt.strip()}, {extra}" if extra else prompt.strip()


def clean_enhanced(text: str, original: str) -> str:
    """The rewritten prompt without quotes or chatter; the original if it looks wrong."""
    first = (text or "").strip().split("\n\n")[0].strip().strip('"“”\'').strip()
    first = re.sub(r"^(here(?:'s| is)[^:]*:|prompt:)\s*", "", first, flags=re.I).strip().strip('"“”')
    if len(first) < max(20, len(original)):
        return original
    return first[:700]


# --- Making and editing images ---------------------------------------------------------

def _suffix(content_type: str) -> str:
    return ".png" if "png" in content_type else ".webp" if "webp" in content_type else ".jpg"


def _raise_for(resp) -> None:
    if resp.status_code < 400:
        return
    try:
        message = (resp.json().get("error") or {}).get("message", "")
    except Exception:
        message = ""
    if resp.status_code == 402:
        wait = re.search(r"Retry after (\d+)s", message)
        if wait:
            raise RateLimited(int(wait.group(1)), "The free image service allows one picture about every 45 seconds.")
        raise ImageError("Your Pollinations account is out of pollen for now. It refills over time, or top it up at enter.pollinations.ai.")
    if resp.status_code in (401, 403):
        raise ImageError("Pollinations did not accept your key. Check it in Settings.")
    if resp.status_code == 400:
        raise ImageError("The image service refused this request. Try different words.")
    raise ImageError("The image service had a problem. Try again in a moment.")


def generate(prompt: str, shape: str, seed: int, model: Optional[str] = None,
             get: Optional[Callable] = None) -> tuple[bytes, str]:
    """One picture as (bytes, file suffix). Raises ImageError or RateLimited."""
    if get is None:
        import requests

        get = requests.get
    _, width, height = SHAPES.get(shape, SHAPES["square"])
    key = load_key()
    query = {"width": width, "height": height, "seed": seed}
    if key:
        query["model"] = model if model in MODELS else DEFAULT_MODEL
        url = f"{GEN_URL}/image/{urllib.parse.quote(prompt, safe='')}?{urllib.parse.urlencode(query)}"
        headers = {"Authorization": f"Bearer {key}"}
    else:
        query["nologo"] = "true"
        url = f"{CLASSIC_URL}{urllib.parse.quote(prompt, safe='')}?{urllib.parse.urlencode(query)}"
        headers = {}
    try:
        resp = get(url, headers=headers, timeout=180)
    except Exception as exc:
        raise ImageError("Couldn't reach the image service. Check the internet connection.") from exc
    _raise_for(resp)
    ctype = resp.headers.get("Content-Type", "")
    if not ctype.startswith("image/"):
        raise ImageError("The image service sent something that isn't a picture. Try again.")
    return resp.content, _suffix(ctype)


def edit(image: bytes, instruction: str, post: Optional[Callable] = None) -> bytes:
    """Change an existing picture following ``instruction`` (needs a key)."""
    key = load_key()
    if not key:
        raise ImageError("Editing a picture needs a free Pollinations key (Settings).")
    if post is None:
        import requests

        post = requests.post
    mime = "image/png" if image[:4] == b"\x89PNG" else "image/jpeg"
    try:
        resp = post(f"{GEN_URL}/v1/images/edits", headers={"Authorization": f"Bearer {key}"},
                    files={"image": ("image" + (".png" if mime == "image/png" else ".jpg"), image, mime)},
                    data={"prompt": instruction, "model": EDIT_MODEL}, timeout=240)
    except Exception as exc:
        raise ImageError("Couldn't reach the image service. Check the internet connection.") from exc
    _raise_for(resp)
    try:
        item = resp.json()["data"][0]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ImageError("The image service sent an unexpected answer. Try again.") from exc
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        import requests

        got = requests.get(item["url"], timeout=120)
        got.raise_for_status()
        return got.content
    raise ImageError("The image service sent no picture. Try again.")
