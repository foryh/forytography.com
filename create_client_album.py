#!/usr/bin/env python3
"""
create_client_album.py — set up a new private client gallery
----------------------------------------------------------------
Uploads a folder of senior-photo (or any client) images to your private
R2 bucket, generates a passkey for the client, and registers the album
so it's instantly viewable at:

    https://forytography.com/album.html?album=<slug>

Usage:
    python3 create_client_album.py \\
        --slug smith-seniors-2026 \\
        --client "The Smith Family" \\
        --photos ~/Desktop/smith-shoot/

You (the photographer) can ALWAYS access any album using your master
passkey (the one you set with `wrangler secret put ALBUM_MASTER_KEY`),
so you never need to keep track of individual client passkeys.

Requirements: wrangler CLI installed and logged in (`wrangler login`).
"""

import argparse
import hashlib
import json
import secrets
import subprocess
import sys
from pathlib import Path

BUCKET_NAME = "forytography-originals"
KV_NAMESPACE_BINDING = "ALBUM_KV"  # must match wrangler.toml


def hash_key(key: str) -> str:
    return hashlib.sha256(key.strip().encode("utf-8")).hexdigest()


def generate_passkey() -> str:
    # Short, easy for a client to type on a phone: e.g. "amber-otter-42"
    words = ["amber", "cedar", "otter", "willow", "canyon", "aspen", "ridge", "harbor", "quartz", "meadow"]
    return f"{secrets.choice(words)}-{secrets.choice(words)}-{secrets.randbelow(90) + 10}"


def upload_photo(local_path: Path, r2_key: str):
    print(f"  Uploading {local_path.name} -> {r2_key}")
    subprocess.run(
        ["wrangler", "r2", "object", "put", f"{BUCKET_NAME}/{r2_key}",
         "--file", str(local_path), "--remote"],
        check=True,
    )


def write_album_kv(slug: str, client_name: str, passkey_hash: str, photo_keys: list):
    value = json.dumps({
        "clientName": client_name,
        "passkeyHash": passkey_hash,
        "photoKeys": photo_keys,
    })
    subprocess.run(
        ["wrangler", "kv", "key", "put",
         f"--binding={KV_NAMESPACE_BINDING}",
         f"album:{slug}", value, "--remote"],
        check=True,
    )


def main():
    parser = argparse.ArgumentParser(description="Create a new private client gallery")
    parser.add_argument("--slug", required=True, help="URL-safe album ID, e.g. smith-seniors-2026")
    parser.add_argument("--client", required=True, help="Display name shown to the client, e.g. 'The Smith Family'")
    parser.add_argument("--photos", required=True, help="Folder containing the photos to upload")
    parser.add_argument("--passkey", help="Set a specific passkey (default: auto-generate a friendly one)")
    args = parser.parse_args()

    photos_dir = Path(args.photos).expanduser()
    if not photos_dir.is_dir():
        sys.exit(f"Not a folder: {photos_dir}")

    photo_files = sorted([
        f for f in photos_dir.iterdir()
        if f.suffix.lower() in (".jpg", ".jpeg", ".png")
    ])
    if not photo_files:
        sys.exit("No .jpg/.jpeg/.png files found in that folder.")

    passkey = args.passkey or generate_passkey()
    passkey_hash = hash_key(passkey)

    print(f"Creating album '{args.slug}' for {args.client} ({len(photo_files)} photos)...")

    photo_keys = []
    for f in photo_files:
        r2_key = f"clients/{args.slug}/{f.name}"
        upload_photo(f, r2_key)
        photo_keys.append(r2_key)

    write_album_kv(args.slug, args.client, passkey_hash, photo_keys)

    print("\nDone! Send your client this:\n")
    print(f"  Gallery link: https://forytography.com/album.html?album={args.slug}")
    print(f"  Passkey:      {passkey}")
    print("\n(You can always get in yourself using your master passkey instead.)")


if __name__ == "__main__":
    main()
