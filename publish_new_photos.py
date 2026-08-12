#!/usr/bin/env python3
"""
publish_new_photos.py — Forytography automated publishing pipeline
--------------------------------------------------------------------
What this does, in order:
  1. Exports any NEW photos from designated Apple Photos albums
  2. Applies the tiled calligraphy watermark + resizes them for the web
  3. Drops full-resolution clean copies into r2-originals/ for photos marked for sale
  4. Inserts a matching gallery card into index.html automatically
  5. Adds new for-sale photos to the Cloudflare Worker's price list
  6. Commits and pushes everything to your GitHub Pages repo
  7. Remembers what it already published, so re-running is always safe

Requirements (one-time):
    pip3 install osxphotos pillow
    brew install git   (already on macOS by default via Xcode tools)

Apple Photos setup (one-time):
  Create these albums in Photos.app and drop photos into whichever fits:
    "Forytography - Nature"
    "Forytography - Portraits"
    "Forytography - Boise Gems"

  Every photo is for sale by default, at DEFAULT_PRICE for its category
  (currently $20 across the board). To override the price for one photo,
  add a keyword formatted like:  price:35   (right-click photo -> Info ->
  Keywords). To publish a photo WITHOUT a Purchase option (e.g. a personal
  shot like your About/self-portrait photo), add the keyword  price:0

  Optional: add a keyword like caption:Sawtooth Range at Dusk to control
  the text shown under the photo. Without it, the script uses the photo's
  Photos.app title, or falls back to the filename.

Usage:
    python3 publish_new_photos.py

Safe to run as often as you like — already-published photos are skipped.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from PIL import Image
import numpy as np

# ---------------------------------------------------------------------------
# CONFIG — update these paths once for your machine
# ---------------------------------------------------------------------------
SITE_DIR = Path.home() / "forytography-site"          # where index.html lives
IMAGES_DIR = SITE_DIR / "images"
R2_ORIGINALS_DIR = SITE_DIR / "r2-originals"
WORKER_FILE = SITE_DIR / "worker" / "src" / "index.js"
INDEX_HTML = SITE_DIR / "index.html"
STATE_FILE = SITE_DIR / ".publish_state.json"
STAGING_DIR = SITE_DIR / ".staging"
# Optional {"DSC_1234": "Alpine Lake at Dawn", ...} — takes priority over any
# caption: keyword or Photos.app title. Handy for naming a whole batch at once
# without setting a keyword on every photo individually.
NAME_OVERRIDES_FILE = SITE_DIR / ".publish_name_overrides.json"

ALBUMS = {
    "Forytography - Nature": "nature",
    "Forytography - Portraits": "portraits",
    "Forytography - Boise Gems": "events",
}
DEFAULT_PRICE = {"nature": 20, "portraits": 20, "events": 20}
CATEGORY_LABEL = {"nature": "Nature", "portraits": "Portrait", "events": "Event"}

LOGO_BLACK = IMAGES_DIR / "calligraphy-logo-black.png"
LOGO_WHITE = IMAGES_DIR / "calligraphy-logo-white.png"
LOGO_GOLD = IMAGES_DIR / "calligraphy-logo-gold.png"


# ---------------------------------------------------------------------------
# Step 1 — export new photos from Apple Photos via osxphotos
# ---------------------------------------------------------------------------
def export_from_photos():
    STAGING_DIR.mkdir(exist_ok=True)
    exported = []
    for album, category in ALBUMS.items():
        dest = STAGING_DIR / category
        dest.mkdir(exist_ok=True)
        print(f"Exporting from album '{album}'...")
        subprocess.run(
            [
                "osxphotos", "export", str(dest),
                "--album", album,
                "--update",                          # only export new/changed photos
                "--keyword", "{keyword}",             # pulls keywords for price/caption parsing
                "--convert-to-jpeg", "--jpeg-quality", "0.95",  # RAW-only photos have no .jpg
                "--skip-original-if-edited",          # if edited in Photos, use only the edit
                "--skip-raw",                          # skip the RAW half of a RAW+JPEG pair
            ],
            check=True,
        )
        for f in dest.glob("*"):
            if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".heic"):
                # osxphotos names an edited photo's export "<id>_edited.jpeg" — strip
                # that suffix so the photo_id matches the original filename elsewhere
                # (images/<id>.jpg, the Worker's PHOTOS table, etc).
                if f.stem.endswith("_edited"):
                    clean = f.with_stem(f.stem[: -len("_edited")])
                    f.rename(clean)
                    f = clean
                exported.append((f, category))
    return exported


def get_photo_metadata(photo_path):
    """Read keywords + title for a given exported photo via osxphotos query."""
    result = subprocess.run(
        ["osxphotos", "query", "--filepath", str(photo_path), "--json"],
        capture_output=True, text=True,
    )
    price = None
    caption = None
    try:
        data = json.loads(result.stdout)[0]
        title = data.get("title")
        keywords = data.get("keywords", [])
        for kw in keywords:
            if kw.lower().startswith("price:"):
                price = int(re.sub(r"[^\d]", "", kw.split(":", 1)[1]))
            if kw.lower().startswith("caption:"):
                caption = kw.split(":", 1)[1].strip()
        caption = caption or title
    except Exception:
        pass
    return price, caption


# ---------------------------------------------------------------------------
# Step 2 — watermark + resize for the web gallery
# ---------------------------------------------------------------------------
def _ink_luminance(logo_path, alpha_thresh=40):
    """Mean perceptual luminance (0-255) of a logo's opaque ink pixels."""
    arr = np.array(Image.open(logo_path).convert("RGBA"))
    mask = arr[..., 3] > alpha_thresh
    rgb = arr[..., :3][mask].astype(float)
    return float((rgb @ [0.299, 0.587, 0.114]).mean()) if rgb.size else 128.0


_GOLD_INK_LUM = _ink_luminance(LOGO_GOLD)


def choose_watermark_logo(photo):
    """Pick black / white / gold ink for a given (already-resized) RGB photo.

    Black/white falls back to the original luminance rule (bright photo ->
    black ink, dark photo -> white ink). Gold is preferred when the photo
    reads as warm-toned and reasonably saturated (golden-hour light, warm
    landscapes) and isn't too close to the gold ink's own luminance — the
    watermark is only 16% opacity, so gold just needs to avoid disappearing
    into a similarly-toned photo, not achieve strong contrast.
    """
    small = photo.resize((60, 60))
    mean_lum = np.array(small.convert("L")).mean()

    hsv = np.array(small.convert("HSV")).astype(float)
    hue_deg = hsv[..., 0] / 255.0 * 360.0
    sat = hsv[..., 1].mean() / 255.0
    theta = np.radians(hue_deg)
    mean_hue = np.degrees(np.arctan2(np.sin(theta).mean(), np.cos(theta).mean())) % 360

    is_warm_and_saturated = 15 <= mean_hue <= 55 and sat > 0.22
    gold_contrast_ok = abs(mean_lum - _GOLD_INK_LUM) > 20

    if is_warm_and_saturated and gold_contrast_ok:
        return LOGO_GOLD
    return LOGO_BLACK if mean_lum > 140 else LOGO_WHITE


def make_watermarked_preview(src_path, out_path, max_dim=2000):
    photo = Image.open(src_path).convert("RGB")
    w, h = photo.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        photo = photo.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    w, h = photo.size

    logo = Image.open(choose_watermark_logo(photo)).convert("RGBA")

    logo_w = int(w * 0.34)
    scale = logo_w / logo.width
    logo_resized = logo.resize((logo_w, int(logo.height * scale)))
    rotated = logo_resized.rotate(24, expand=True)
    r, g, b, a = rotated.split()
    a = a.point(lambda p: int(p * 0.16))
    rotated.putalpha(a)

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    step_x = rotated.width + int(w * 0.10)
    step_y = rotated.height + int(h * 0.14)
    y = -step_y
    row = 0
    while y < h + step_y:
        offset_x = (step_x // 2) if (row % 2) else 0
        x = -step_x + offset_x
        while x < w + step_x:
            overlay.alpha_composite(rotated, (x, y))
            x += step_x
        y += step_y
        row += 1

    combined = Image.alpha_composite(photo.convert("RGBA"), overlay).convert("RGB")
    combined.save(out_path, "JPEG", quality=88)


def make_clean_original(src_path, out_path):
    photo = Image.open(src_path).convert("RGB")
    photo.save(out_path, "JPEG", quality=95)


def make_hero_image(src_path, out_path, max_dim=2400, quality=88):
    """Full-bleed, no-watermark web copy for a hero/header banner use."""
    photo = Image.open(src_path).convert("RGB")
    w, h = photo.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        photo = photo.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    photo.save(out_path, "JPEG", quality=quality)


# ---------------------------------------------------------------------------
# Step 3 — insert a gallery card into index.html
# ---------------------------------------------------------------------------
def build_card_html(photo_id, category, caption, price):
    # Purchasing happens on the photo.html product page, reached via the
    # lightbox's "Buy This Photo" button — that button only appears when the
    # triggering <img> has a data-photo-id, so only add it for for-sale photos.
    photo_id_attr = f' data-photo-id="{photo_id}"' if price is not None else ""
    return f"""
    <div class="card" data-cat="{category}">
      <img src="images/{photo_id}.jpg" alt="{caption}"{photo_id_attr} class="lightbox-trigger" data-cap="{caption}">
      <div class="card-cap mono">{caption}</div>
    </div>
"""


def insert_card_into_html(card_html):
    html = INDEX_HTML.read_text()
    marker = "<!-- AUTO-GALLERY-INSERT: publish_new_photos.py adds new photo cards directly above this line. Do not remove. -->"
    if marker not in html:
        raise RuntimeError("Insert marker not found in index.html — has the file been edited?")
    html = html.replace(marker, card_html.strip() + "\n\n    " + marker)
    INDEX_HTML.write_text(html)


# ---------------------------------------------------------------------------
# Step 4 — register for-sale photos with the Worker's price list
# ---------------------------------------------------------------------------
def add_to_worker_pricelist(photo_id, category, name, caption, price):
    js = WORKER_FILE.read_text()
    entry = (
        f'  "{photo_id}": {{ name: "{name}", priceCents: {price * 100}, '
        f'category: "{category}", caption: "{caption}", r2Key: "originals/{photo_id}.jpg" }},\n'
    )
    marker = "const PHOTOS = {\n"
    if marker not in js:
        raise RuntimeError("Could not find PHOTOS table in worker/src/index.js")
    js = js.replace(marker, marker + entry)
    WORKER_FILE.write_text(js)


# ---------------------------------------------------------------------------
# Step 5 — git commit + push
# ---------------------------------------------------------------------------
def git_publish(photo_ids):
    subprocess.run(["git", "-C", str(SITE_DIR), "add", "."], check=True)
    msg = f"Add new photos: {', '.join(photo_ids)}"
    subprocess.run(["git", "-C", str(SITE_DIR), "commit", "-m", msg], check=True)
    subprocess.run(["git", "-C", str(SITE_DIR), "push"], check=True)


def maybe_redeploy_worker():
    worker_dir = SITE_DIR / "worker"
    try:
        subprocess.run(["wrangler", "deploy"], cwd=worker_dir, check=True)
        print("Worker redeployed with updated price list.")
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("NOTE: could not auto-redeploy the Worker. Run 'wrangler deploy' "
              "in the worker/ folder manually to activate new for-sale photos.")


# ---------------------------------------------------------------------------
# State tracking so re-runs never duplicate work
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"published": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    state = load_state()
    published_ids = set(state["published"])
    name_overrides = (
        json.loads(NAME_OVERRIDES_FILE.read_text()) if NAME_OVERRIDES_FILE.exists() else {}
    )

    exported = export_from_photos()
    new_photo_ids = []
    new_sale_ids = []

    for src_path, category in exported:
        photo_id = src_path.stem
        if photo_id in published_ids:
            continue

        price, name = get_photo_metadata(src_path)
        name = name_overrides.get(photo_id) or name or photo_id
        # No explicit price: keyword -> for sale at the category default.
        # An explicit price:0 keyword opts a photo OUT of being for sale.
        if price is None:
            price = DEFAULT_PRICE.get(category)
        elif price == 0:
            price = None
        caption = f"{name} · {CATEGORY_LABEL.get(category, category.title())}"

        preview_out = IMAGES_DIR / f"{photo_id}.jpg"
        make_watermarked_preview(src_path, preview_out)

        if price is not None:
            clean_out = R2_ORIGINALS_DIR / f"{photo_id}.jpg"
            make_clean_original(src_path, clean_out)
            add_to_worker_pricelist(photo_id, category, name, caption, price)
            new_sale_ids.append(photo_id)

        card_html = build_card_html(photo_id, category, caption, price)
        insert_card_into_html(card_html)

        published_ids.add(photo_id)
        new_photo_ids.append(photo_id)
        print(f"Processed {photo_id} -> {category}"
              + (f" (for sale: ${price})" if price is not None else " (gallery only)"))

    if not new_photo_ids:
        print("No new photos found. Nothing to publish.")
        return

    git_publish(new_photo_ids)

    if new_sale_ids:
        print(f"\n{len(new_sale_ids)} new for-sale photo(s) — remember to upload "
              f"the full-res files to R2:")
        for pid in new_sale_ids:
            print(f"  wrangler r2 object put forytography-originals/originals/{pid}.jpg "
                  f"--file={R2_ORIGINALS_DIR / (pid + '.jpg')}")
        maybe_redeploy_worker()

    state["published"] = sorted(published_ids)
    save_state(state)
    print(f"\nDone. Published {len(new_photo_ids)} new photo(s).")


if __name__ == "__main__":
    main()
