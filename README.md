# Mail Attachment Scraper

A small Python script that connects to Yahoo Mail or Gmail over IMAP, walks
every message in a folder, and saves attachments to disk — sorted by media
type and grouped by the date of the email.

```
attachments/
└── <provider>/
    └── <category>/         # images, pdfs, documents, archives, video, audio, other
        └── YYYYMMDD/
            └── <filename>
```

## Features

- Yahoo and Gmail support out of the box (same script, `--provider` flag).
- Attachments sorted into category folders by file extension.
- Per-message date folders (`YYYYMMDD`) inside each category.
- SHA-256 de-duplication — the same image attached to dozens of messages is
  saved once.
- Resumable: progress is written to a per-provider manifest every 50
  messages, so an interrupted run picks up where it left off.
- Read-only IMAP sessions — nothing in your mailbox is modified.
- Zero third-party dependencies (Python standard library only).

## Requirements

- Python 3.9 or newer.
- An app password for the provider you want to scrape (see below). Regular
  account passwords will not work over IMAP for either Yahoo or Gmail.

## Setup

```sh
git clone <your-repo-url> mail-attachment-scraper
cd mail-attachment-scraper
cp .env.example .env
# edit .env and fill in the credentials for the provider(s) you'll use
```

### Yahoo app password

1. Sign in at <https://login.yahoo.com/account/security>.
2. Click **Generate app password**, give it a name (e.g. `scraper`).
3. Yahoo shows a 16-character password — copy it into `YAHOO_APP_PASSWORD`
   in your `.env`. Spaces in the displayed password are fine.

### Gmail app password

1. Turn on **2-Step Verification** at
   <https://myaccount.google.com/security> (required before app passwords
   are available).
2. Visit <https://myaccount.google.com/apppasswords>, create a new app
   password.
3. Copy the 16-character password into `GMAIL_APP_PASSWORD` in your `.env`.
   Remove the spaces if Google displays them with spaces.

## Usage

```sh
python3 scraper.py --provider yahoo
python3 scraper.py --provider gmail
```

By default Yahoo reads from `INBOX` and Gmail reads from
`[Gmail]/All Mail` (the archive, which includes the inbox). Override with
`--mailbox`:

```sh
python3 scraper.py --provider gmail --mailbox INBOX
python3 scraper.py --provider yahoo --mailbox "Bulk Mail"
```

The first run on a large mailbox can take a while — the script fetches the
full RFC822 source of every message. Subsequent runs only fetch messages
whose UIDs aren't in the manifest yet.

## Configuration

Edit the constants at the top of `scraper.py` if you want to:

- Add or change extension-to-category mappings (`CATEGORY_MAP`).
- Add another IMAP provider (`PROVIDERS`).
- Change how often progress is flushed (`BATCH_SIZE`).

## How resume works

For each provider, the script writes:

```
attachments/<provider>/downloaded.json
```

This file holds two lists:

- `hashes` — SHA-256 of every attachment saved. Used to drop duplicates.
- `message_ids` — IMAP UIDs of every message already walked. Used to skip
  ahead on resume.

Delete this file to force a fresh scan of the mailbox. Existing files in
the `attachments/` tree are left alone in either case — incoming files
that would collide on disk get auto-numbered (`name.jpg`, `name_1.jpg`).

## Caveats

- **Outlook / Microsoft 365 is not supported.** Microsoft has deprecated
  IMAP basic-auth across most accounts; supporting Outlook requires an
  OAuth2 flow and an Azure app registration.
- **Yahoo and Gmail will throttle** if you blast IMAP. The script pauses
  briefly between batches; if you still see disconnects, increase
  `BATCH_SIZE` or add a longer `time.sleep()`.
- **Inline images** that have a filename are downloaded. Images that are
  truly embedded with no filename hint are skipped (most clients give them
  a filename, but a few don't).
- **The IMAP UID isn't globally unique** — it's only unique per mailbox on
  one server. The manifest is therefore per-provider; if you run against
  multiple Gmail accounts, give each its own checkout (or extend the
  script to scope the manifest by email address).

## Files

- `scraper.py` — the script.
- `.env.example` — credentials template. Copy to `.env` and fill in.
- `.gitignore` — keeps `.env` and the `attachments/` output out of the
  repo. Add one if you don't already have it.
