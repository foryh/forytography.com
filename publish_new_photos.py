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
    "Forytography - Events"

  To mark a photo FOR SALE with a price, add a keyword to it in Photos.app
  formatted like:  price:20     (right-click photo -> Info -> Keywords)
  Photos without a "price:" keyword are still published to the gallery,
  just without a Purchase button (e.g. your About/self-portrait shots).

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

ALBUMS = {
    "Forytography - Nature": "nature",
    "Forytography - Portraits": "portraits",
    "Forytography - Events": "events",
}
DEFAULT_PRICE = {"nature": 15, "portraits": 25, "events": 20}

LOGO_BLACK = IMAGES_DIR / "calligraphy-logo-black.png"
LOGO_WHITE = IMAGES_DIR / "calligraphy-logo-white.png"


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
                "--update",                # only export new/changed photos
                "--keyword", "{keyword}",  # pulls keywords for price/caption parsing
            ],
            check=True,
        )
        for f in dest.glob("*"):
            if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".heic"):
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
def make_watermarked_preview(src_path, out_path, max_dim=2000):
    photo = Image.open(src_path).convert("RGB")
    w, h = photo.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        photo = photo.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    w, h = photo.size

    small = photo.resize((60, 60))
    mean_lum = np.array(small.convert("L")).mean()
    logo = Image.open(LOGO_BLACK if mean_lum > 140 else LOGO_WHITE).convert("RGBA")

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


# ---------------------------------------------------------------------------
# Step 3 — insert a gallery card into index.html
# ---------------------------------------------------------------------------
def build_card_html(photo_id, category, caption, price):
    buy_block = ""
    if price is not None:
        buy_block = f"""
      <div class="card-buy">
        <span class="mono price">${price}</span>
        <button class="buy-btn mono" data-photo-id="{photo_id}">Purchase</button>
      </div>"""
    return f"""
    <div class="card" data-cat="{category}">
      <img src="images/{photo_id}.jpg" alt="{caption}" class="lightbox-trigger" data-cap="{caption}">
      <div class="card-cap mono">{caption}</div>{buy_block}
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
def add_to_worker_pricelist(photo_id, caption, price):
    js = WORKER_FILE.read_text()
    entry = f'  "{photo_id}": {{ name: "{caption}", priceCents: {price * 100}, r2Key: "originals/{photo_id}.jpg" }},\n'
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

    exported = export_from_photos()
    new_photo_ids = []
    new_sale_ids = []

    for src_path, category in exported:
        photo_id = src_path.stem
        if photo_id in published_ids:
            continue

        price, caption = get_photo_metadata(src_path)
        caption = caption or photo_id
        price = price if price is not None else None

        preview_out = IMAGES_DIR / f"{photo_id}.jpg"
        make_watermarked_preview(src_path, preview_out)

        if price is not None:
            clean_out = R2_ORIGINALS_DIR / f"{photo_id}.jpg"
            make_clean_original(src_path, clean_out)
            add_to_worker_pricelist(photo_id, caption, price)
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
