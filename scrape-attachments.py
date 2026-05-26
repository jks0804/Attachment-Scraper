#!/usr/bin/env python3
"""
Mail attachment scraper (Yahoo + Gmail).

Connects to the chosen mail provider over IMAP, walks every message in the
selected folder, and saves attachments into category subfolders (images,
pdfs, documents, archives, video, audio, other) bucketed by the message
date.

Layout:
    attachments/<provider>/<category>/YYYYMMDD/<filename>

SETUP
-----
Yahoo:
  1. Generate a Yahoo app password:
     https://login.yahoo.com/account/security  ->  "Generate app password"
  2. Export:
       export YAHOO_EMAIL="you@yahoo.com"
       export YAHOO_APP_PASSWORD="xxxx xxxx xxxx xxxx"
  3. python3 scraper.py --provider yahoo

Gmail:
  1. Turn on 2-Step Verification on the Google account.
  2. Create an app password:
     https://myaccount.google.com/apppasswords
  3. Export:
       export GMAIL_EMAIL="you@gmail.com"
       export GMAIL_APP_PASSWORD="xxxxxxxxxxxxxxxx"
  4. python3 scraper.py --provider gmail
     (To grab everything including archived mail, pass
      --mailbox "[Gmail]/All Mail")

Credentials can also live in a .env file next to this script.
The script keeps a per-provider manifest so re-running only fetches new
attachments. Safe to interrupt and resume.
"""

from __future__ import annotations

import argparse
import email
import hashlib
import imaplib
import json
import os
import re
import sys
import time
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

PROVIDERS = {
    "yahoo": {
        "host": "imap.mail.yahoo.com",
        "port": 993,
        "env_user": "YAHOO_EMAIL",
        "env_pass": "YAHOO_APP_PASSWORD",
        "default_mailbox": "INBOX",
    },
    "gmail": {
        "host": "imap.gmail.com",
        "port": 993,
        "env_user": "GMAIL_EMAIL",
        "env_pass": "GMAIL_APP_PASSWORD",
        # "[Gmail]/All Mail" pulls archived messages too. Override with
        # --mailbox if you only want the inbox.
        "default_mailbox": "[Gmail]/All Mail",
    },
}

BASE_DIR = Path(__file__).parent
OUTPUT_ROOT = BASE_DIR / "attachments"

# How often to flush the manifest to disk and pause briefly. IMAP servers
# will throttle if you hammer them.
BATCH_SIZE = 50

# Categories: lowercase extension -> folder name.
CATEGORY_MAP = {
    # images
    "jpg": "images", "jpeg": "images", "png": "images", "gif": "images",
    "bmp": "images", "tif": "images", "tiff": "images", "webp": "images",
    "heic": "images", "heif": "images", "svg": "images", "raw": "images",
    "cr2": "images", "nef": "images", "arw": "images", "dng": "images",
    # documents
    "pdf": "pdfs",
    "doc": "documents", "docx": "documents", "odt": "documents",
    "rtf": "documents", "txt": "documents", "md": "documents",
    "xls": "documents", "xlsx": "documents", "ods": "documents",
    "csv": "documents", "tsv": "documents",
    "ppt": "documents", "pptx": "documents", "odp": "documents",
    # archives
    "zip": "archives", "rar": "archives", "7z": "archives",
    "tar": "archives", "gz": "archives", "bz2": "archives", "xz": "archives",
    # video
    "mp4": "video", "mov": "video", "avi": "video", "mkv": "video",
    "wmv": "video", "flv": "video", "webm": "video", "m4v": "video",
    "mpg": "video", "mpeg": "video", "3gp": "video",
    # audio
    "mp3": "audio", "wav": "audio", "flac": "audio", "aac": "audio",
    "ogg": "audio", "m4a": "audio", "wma": "audio", "aiff": "audio",
}
DEFAULT_CATEGORY = "other"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_env_file(path: Path) -> None:
    """Cheap .env loader so credentials can live in a sibling file."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def decode_mime(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out)


_FILENAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')

def safe_filename(name: str) -> str:
    name = _FILENAME_BAD.sub("_", name).strip(" .")
    return name or "unnamed"


def category_for(filename: str) -> str:
    ext = Path(filename).suffix.lower().lstrip(".")
    return CATEGORY_MAP.get(ext, DEFAULT_CATEGORY)


def date_folder_for(msg: Message) -> str:
    """Return YYYYMMDD parsed from the message's Date header (or 'undated')."""
    raw = msg.get("Date")
    if not raw:
        return "undated"
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return "undated"
    if dt is None:
        return "undated"
    return dt.strftime("%Y%m%d")


def unique_path(folder: Path, filename: str) -> Path:
    """Return a path that doesn't collide with an existing file."""
    candidate = folder / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    i = 1
    while True:
        candidate = folder / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def hash_payload(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def load_manifest(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            print("warning: manifest unreadable, starting fresh", file=sys.stderr)
    return {"hashes": [], "message_ids": []}


def save_manifest(manifest: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2))


# ---------------------------------------------------------------------------
# IMAP work
# ---------------------------------------------------------------------------

def connect(provider: dict) -> imaplib.IMAP4_SSL:
    user = os.environ.get(provider["env_user"])
    pw = os.environ.get(provider["env_pass"])
    if not user or not pw:
        sys.exit(
            f"Missing {provider['env_user']} or {provider['env_pass']}. "
            "Export them or put them in a .env file next to this script."
        )
    print(f"Connecting to {provider['host']} as {user} ...")
    M = imaplib.IMAP4_SSL(provider["host"], provider["port"])
    M.login(user, pw)
    return M


def pick_mailbox(M: imaplib.IMAP4_SSL, default: str, override: str | None) -> str:
    if override:
        return override
    return default


def iter_message_ids(M: imaplib.IMAP4_SSL) -> list[bytes]:
    typ, data = M.search(None, "ALL")
    if typ != "OK":
        raise RuntimeError(f"IMAP search failed: {typ}")
    return data[0].split()


def extract_attachments(msg: Message):
    """Yield (filename, payload_bytes) for each attachment part."""
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        content_disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" not in content_disp and "inline" not in content_disp:
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        yield decode_mime(filename), payload


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scrape attachments from Yahoo or Gmail over IMAP.")
    p.add_argument("--provider", choices=sorted(PROVIDERS), default="yahoo",
                   help="Mail provider to scrape (default: yahoo).")
    p.add_argument("--mailbox", default=None,
                   help="IMAP folder to scan. Defaults to the provider's natural full-archive folder.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file(BASE_DIR / ".env")

    provider = PROVIDERS[args.provider]
    output_dir = OUTPUT_ROOT / args.provider
    manifest_path = output_dir / "downloaded.json"
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(manifest_path)
    seen_hashes = set(manifest["hashes"])
    seen_msg_ids = set(manifest["message_ids"])

    M = connect(provider)
    try:
        mailbox = pick_mailbox(M, provider["default_mailbox"], args.mailbox)
        typ, data = M.select(mailbox, readonly=True)
        if typ != "OK":
            sys.exit(f"Could not select mailbox {mailbox!r}: {data}")
        total_in_mailbox = int(data[0])
        print(f"Mailbox {mailbox!r} has {total_in_mailbox} messages.")

        ids = iter_message_ids(M)
        print(f"Found {len(ids)} message IDs. Walking ...")

        saved = 0
        skipped = 0
        for i, msg_id in enumerate(ids, start=1):
            if msg_id.decode() in seen_msg_ids:
                continue
            try:
                typ, data = M.fetch(msg_id, "(RFC822)")
            except imaplib.IMAP4.abort:
                print("IMAP aborted, reconnecting ...")
                M = connect(provider)
                M.select(mailbox, readonly=True)
                typ, data = M.fetch(msg_id, "(RFC822)")
            if typ != "OK" or not data or not data[0]:
                print(f"  msg {msg_id!r}: fetch failed ({typ})")
                continue

            raw = data[0][1]
            msg = email.message_from_bytes(raw)
            subject = decode_mime(msg.get("Subject"))[:80]
            datestamp = date_folder_for(msg)

            for fname, payload in extract_attachments(msg):
                h = hash_payload(payload)
                if h in seen_hashes:
                    skipped += 1
                    continue
                category = category_for(fname)
                folder = output_dir / category / datestamp
                folder.mkdir(parents=True, exist_ok=True)
                dest = unique_path(folder, safe_filename(fname))
                dest.write_bytes(payload)
                seen_hashes.add(h)
                saved += 1
                print(f"  [{category}/{datestamp}] {dest.name}   <-  {subject!r}")

            seen_msg_ids.add(msg_id.decode())

            if i % BATCH_SIZE == 0:
                manifest["hashes"] = sorted(seen_hashes)
                manifest["message_ids"] = sorted(seen_msg_ids)
                save_manifest(manifest, manifest_path)
                print(f"... progress: {i}/{len(ids)} msgs, "
                      f"{saved} saved, {skipped} duplicates")
                time.sleep(0.5)

        manifest["hashes"] = sorted(seen_hashes)
        manifest["message_ids"] = sorted(seen_msg_ids)
        save_manifest(manifest, manifest_path)
        print(f"\nDone. Saved {saved} new attachments, "
              f"skipped {skipped} duplicates.")
        print(f"Files are in: {output_dir}")
        return 0
    finally:
        try:
            M.logout()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
