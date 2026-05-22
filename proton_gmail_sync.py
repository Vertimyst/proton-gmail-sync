#!/usr/bin/env python3
"""
proton_gmail_sync.py
--------------------
Syncs message deletions from Proton Mail to Gmail.

If a message is deleted in Proton that also exists in Gmail (matched by
Message-ID header), it will be moved to Gmail Trash.

SETUP (one-time, run interactively):
  1. Install dependencies:
       pip install protonmail-api-client

  2. Set your credentials in CONFIG below (or use environment variables).

  3. Run once interactively to authenticate and save your Proton session:
       python3 proton_gmail_sync.py --login
     This handles any CAPTCHA challenge and saves session.pickle.

  4. Test a dry-run (no deletions, just shows what would be deleted):
       python3 proton_gmail_sync.py --dry-run

  5. Add to cron for automatic sync (every 5 minutes):
       */5 * * * * /usr/bin/python3 /home/vertimyst/mail-sync/proton_gmail_sync.py >> /var/log/proton_gmail_sync.log 2>&1

NOTE: The session.pickle file contains sensitive auth tokens.
      chmod 600 it and store it somewhere safe.
"""

import argparse
import imaplib
import json
import logging
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# CONFIGURATION
# Credentials can also be set via environment variables to avoid hardcoding.
# ---------------------------------------------------------------------------
CONFIG = {
    "proton": {
        # Your Proton Mail address and password.
        # Or set env vars: PROTON_USERNAME, PROTON_PASSWORD
        "username": os.environ.get("PROTON_USERNAME", "you@proton.me"),
        "password": os.environ.get("PROTON_PASSWORD", "your-proton-password"),
        # Path to save the Proton session (avoids re-authenticating every run).
        # WARNING: contains sensitive tokens — chmod 600 this file.
        "session_file": "/home/vertimyst/mail-sync/session.pickle",
        # Max messages to fetch per sync. Increase if you have a large inbox.
        "page_size": 500,
    },
    "gmail": {
        "host": "imap.gmail.com",
        "port": 993,
        # Your Gmail address and App Password.
        # Generate at: Google Account > Security > App Passwords
        # Or set env vars: GMAIL_USERNAME, GMAIL_PASSWORD
        "username": os.environ.get("GMAIL_USERNAME", "you@gmail.com"),
        "password": os.environ.get("GMAIL_PASSWORD", "xxxx-xxxx-xxxx-xxxx"),
        # Gmail folder to search for messages to delete.
        "folder": "INBOX",
    },
    # Path to the Message-ID cache (tracks what was in Proton last run).
    "cache_file": "/home/vertimyst/mail-sync/proton_message_ids.json",
    # Logging: DEBUG, INFO, WARNING, ERROR
    "log_level": "INFO",
}
# ---------------------------------------------------------------------------


def setup_logging(level_str: str) -> logging.Logger:
    level = getattr(logging, level_str.upper(), logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("proton_gmail_sync")
    logger.setLevel(level)

    # Console: only warnings and above go to stderr (triggers cron email)
    console = logging.StreamHandler()  # defaults to stderr
    console.setFormatter(formatter)
    console.setLevel(logging.WARNING)
    logger.addHandler(console)

    # File output
    log_path = Path(__file__).parent / "mail-sync.log"
    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


# ---------------------------------------------------------------------------
# Proton helpers
# ---------------------------------------------------------------------------

def proton_login(cfg: dict, log: logging.Logger):
    """Authenticate to Proton and save session. Run interactively."""
    from protonmail import ProtonMail
    from protonmail.models import CaptchaConfig
    proton = ProtonMail()
    log.info("Logging in to Proton as %s...", cfg["username"])
    log.info("Manual CAPTCHA required. Follow the instructions below.")
    proton.login(cfg["username"], cfg["password"],
                 captcha_config=CaptchaConfig(type=CaptchaConfig.CaptchaType.MANUAL))
    session_path = cfg["session_file"]
    Path(session_path).parent.mkdir(parents=True, exist_ok=True)
    proton.save_session(session_path)
    os.chmod(session_path, 0o600)
    log.info("Session saved to %s (chmod 600 applied).", session_path)
    return proton

def proton_load_session(cfg: dict, log: logging.Logger):
    """Load a saved Proton session. Tokens refresh automatically."""
    from protonmail import ProtonMail
    session_path = cfg["session_file"]
    if not Path(session_path).exists():
        log.error(
            "No session file found at %s. Run with --login first.", session_path
        )
        sys.exit(1)
    proton = ProtonMail()
    proton.load_session(session_path, auto_save=True)
    log.info("Proton session loaded from %s.", session_path)
    return proton


def fetch_proton_message_ids(proton, cfg: dict, log: logging.Logger) -> set:
    """
    Return the set of ExternalIDs (Message-ID headers) currently in Proton Inbox.
    Calls the API directly to avoid pagination bugs in the library.
    """
    ids = set()
    page = 0
    page_size = 100  # Safe page size

    while True:
        log.debug("Fetching Proton messages page %d...", page)
        r = proton.session.get(
            "https://mail.proton.me/api/mail/v4/messages",
            params={"Page": page, "PageSize": page_size, "LabelID": "0"},
        )
        data = r.json()
        messages = data.get("Messages", [])
        if not messages:
            break
        for msg in messages:
            mid = msg.get("ExternalID", "").strip()
            if mid:
                ids.add(mid)
        log.info("Page %d: fetched %d messages (%d with Message-IDs so far).",
                 page, len(messages), len(ids))
        if len(messages) < page_size:
            break
        page += 1

    log.info("Found %d messages with Message-IDs in Proton Inbox.", len(ids))
    return ids


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def load_cache(path: str) -> set:
    if not Path(path).exists():
        return set()
    try:
        with open(path) as f:
            data = json.load(f)
        return set(data)
    except (json.JSONDecodeError, OSError):
        return set()


def save_cache(path: str, message_ids: set) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(sorted(message_ids), f, indent=2)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def gmail_connect(cfg: dict, log: logging.Logger) -> imaplib.IMAP4_SSL:
    log.info("Connecting to Gmail IMAP as %s...", cfg["username"])
    conn = imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
    conn.login(cfg["username"], cfg["password"])
    return conn


def find_gmail_uid(conn: imaplib.IMAP4_SSL, folder: str, message_id: str) -> bytes | None:
    """Search Gmail for a message by its Message-ID. Returns UID bytes or None."""
    conn.select(folder)
    _, data = conn.uid("SEARCH", None, f'HEADER Message-ID "{message_id}"')
    uids = data[0].split() if data[0] else []
    return uids[0] if uids else None


def trash_gmail_message(conn: imaplib.IMAP4_SSL, uid: bytes, log: logging.Logger) -> bool:
    """
    Move a Gmail message to Trash (not permanent delete).
    Gmail's IMAP Trash folder is [Gmail]/Trash.
    Messages auto-purge from Trash after 30 days.
    """
    try:
        result, _ = conn.uid("COPY", uid, "[Gmail]/Trash")
        if result != "OK":
            log.warning("COPY to [Gmail]/Trash failed for UID %s", uid.decode())
            return False
        conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        conn.expunge()
        return True
    except imaplib.IMAP4.error as e:
        log.error("IMAP error trashing UID %s: %s", uid.decode(), e)
        return False


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def run_sync(dry_run: bool, log: logging.Logger) -> None:
    proton_cfg = CONFIG["proton"]
    gmail_cfg = CONFIG["gmail"]
    cache_path = CONFIG["cache_file"]

    # 1. Load previous cache
    previous_ids = load_cache(cache_path)
    log.info("Loaded %d Message-IDs from cache.", len(previous_ids))

    # 2. Connect to Proton and get current inbox Message-IDs
    proton = proton_load_session(proton_cfg, log)
    current_ids = fetch_proton_message_ids(proton, proton_cfg, log)

    # 3. First run: just save cache, don't delete anything yet
    if not previous_ids:
        log.info(
            "First run — no previous cache. Saving %d Message-IDs as baseline.",
            len(current_ids),
        )
        log.info("No deletions will be mirrored until the next run.")
        save_cache(cache_path, current_ids)
        return

    # 4. Detect deletions: IDs that were present before but are gone now
    deleted_ids = previous_ids - current_ids
    new_ids = current_ids - previous_ids  # arrived since last run (informational)
    log.info(
        "Since last run: %d deleted from Proton, %d new in Proton.",
        len(deleted_ids),
        len(new_ids),
    )

    if not deleted_ids:
        log.info("Nothing to mirror. Updating cache.")
        save_cache(cache_path, current_ids)
        return

    # 5. Mirror deletions to Gmail
    if dry_run:
        log.info("[DRY RUN] Would search Gmail for %d deleted Message-ID(s):", len(deleted_ids))
        for mid in sorted(deleted_ids):
            log.info("  %s", mid)
        log.info("[DRY RUN] No changes made to Gmail or cache.")
        return

    gmail = gmail_connect(gmail_cfg, log)
    trashed = 0
    not_found = 0
    errors = 0

    for mid in deleted_ids:
        log.debug("Searching Gmail for: %s", mid)
        try:
            uid = find_gmail_uid(gmail, gmail_cfg["folder"], mid)
            if uid:
                success = trash_gmail_message(gmail, uid, log)
                if success:
                    log.info("Trashed in Gmail: %s", mid)
                    trashed += 1
                else:
                    log.warning("Failed to trash in Gmail: %s", mid)
                    errors += 1
            else:
                log.debug("Not found in Gmail (may not have been forwarded): %s", mid)
                not_found += 1
        except Exception as e:
            log.error("Unexpected error processing %s: %s", mid, e)
            errors += 1

    gmail.logout()

    log.info(
        "Sync complete. Trashed: %d | Not in Gmail: %d | Errors: %d",
        trashed, not_found, errors,
    )

    # 6. Update cache with current Proton state
    save_cache(cache_path, current_ids)
    log.info("Cache updated with %d current Message-IDs.", len(current_ids))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Sync Proton Mail deletions to Gmail."
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Authenticate to Proton interactively and save session. Run this once.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted in Gmail without making any changes.",
    )
    args = parser.parse_args()

    log = setup_logging(CONFIG["log_level"])

    if args.login:
        log.info("=== Interactive Proton login ===")
        proton_login(CONFIG["proton"], log)
        log.info("Login complete. You can now run without --login.")
        return

    log.info("=== Proton → Gmail sync run ===")
    run_sync(dry_run=args.dry_run, log=log)


if __name__ == "__main__":
    main()