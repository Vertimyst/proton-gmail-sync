#!/usr/bin/env python3
"""
proton_gmail_sync.py
--------------------
Syncs Proton Mail actions to Gmail by monitoring Proton folders.

Actions synced every run:
  - Proton Trash    → Gmail Trash
  - Proton Spam     → Gmail Spam
  - Proton Archive  → Gmail Archive (removes INBOX label)
  - Proton folders  → Gmail labels (created automatically if missing)

Messages are matched by Message-ID header. Only Gmail INBOX messages
are affected — already-organised Gmail messages are left alone.

No cache needed. Every run is fully self-contained.

SETUP (one-time):
  1. Install dependencies:
       pip install protonmail-api-client python-dotenv

  2. Create a .env file in the same directory:
       PROTON_USERNAME=you@proton.me
       PROTON_PASSWORD=your-proton-password
       GMAIL_USERNAME=you@gmail.com
       GMAIL_PASSWORD=xxxx-xxxx-xxxx-xxxx  # Gmail App Password

  3. Authenticate to Proton (run interactively, once):
       venv/bin/python3 proton_gmail_sync.py --login

  4. Dry run to verify:
       venv/bin/python3 proton_gmail_sync.py --dry-run

  5. Add to cron:
       * * * * * cd /home/vertimyst/mail-sync && venv/bin/python3 proton_gmail_sync.py > /dev/null
"""

import argparse
import imaplib
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
CONFIG = {
    "proton": {
        "username": os.environ.get("PROTON_USERNAME", "you@proton.me"),
        "password": os.environ.get("PROTON_PASSWORD", "your-proton-password"),
        "session_file": "/home/vertimyst/mail-sync/session.pickle",
        "page_size": 100,
    },
    "gmail": {
        "host": "imap.gmail.com",
        "port": 993,
        "username": os.environ.get("GMAIL_USERNAME", "you@gmail.com"),
        "password": os.environ.get("GMAIL_PASSWORD", "xxxx-xxxx-xxxx-xxxx"),
    },
    "log_level": "INFO",
    "log_file": "/home/vertimyst/mail-sync/mail-sync.log",
}

# Proton system label IDs
PROTON_LABELS = {
    "inbox":   "0",
    "drafts":  "1",
    "sent":    "2",
    "trash":   "3",
    "spam":    "4",
    "archive": "6",
}
# ---------------------------------------------------------------------------


def setup_logging(level_str: str, log_file: str) -> logging.Logger:
    level = getattr(logging, level_str.upper(), logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("proton_gmail_sync")
    logger.setLevel(level)

    # stderr only for WARNING+ (triggers cron email on errors)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.setLevel(logging.WARNING)
    logger.addHandler(console)

    # File handler for all levels
    fh = logging.FileHandler(log_file)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    return logger


# ---------------------------------------------------------------------------
# Proton helpers
# ---------------------------------------------------------------------------

def proton_login(cfg: dict, log: logging.Logger):
    """Authenticate to Proton interactively and save session."""
    from protonmail import ProtonMail
    from protonmail.models import CaptchaConfig
    proton = ProtonMail()
    log.info("Logging in to Proton as %s...", cfg["username"])
    log.info("Manual CAPTCHA required. A URL will be printed — open it in a browser.")
    log.info("In DevTools > Network, solve the CAPTCHA, find the 'init' request,")
    log.info("copy the token value from the response, and paste it here when prompted.")
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
        log.error("No session file at %s. Run --login first.", session_path)
        sys.exit(1)
    proton = ProtonMail()
    proton.load_session(session_path, auto_save=True)
    log.info("Proton session loaded.")
    return proton


def fetch_proton_folder_message_ids(proton, label_id: str, label_name: str,
                                     page_size: int, log: logging.Logger) -> set:
    """
    Return set of ExternalIDs (Message-ID headers) for all messages
    in a given Proton label/folder.
    """
    ids = set()
    page = 0
    while True:
        r = proton.session.get(
            "https://mail.proton.me/api/mail/v4/messages",
            params={"Page": page, "PageSize": page_size, "LabelID": label_id},
        )
        data = r.json()
        messages = data.get("Messages", [])
        if not messages:
            break
        for msg in messages:
            mid = msg.get("ExternalID", "").strip()
            if mid:
                ids.add(mid)
        if len(messages) < page_size:
            break
        page += 1
    log.info("Proton %-12s → %d messages with Message-IDs.", label_name, len(ids))
    return ids


def fetch_proton_custom_folders(proton, log: logging.Logger) -> list[dict]:
    """
    Return list of custom Proton folders (non-system labels).
    Each entry: {"id": str, "name": str}
    """
    r = proton.session.get("https://mail.proton.me/api/core/v4/labels?Type=1")
    data = r.json()
    folders = []
    for label in data.get("Labels", []):
        folders.append({"id": label["ID"], "name": label["Name"]})
    log.info("Found %d custom Proton folder(s): %s",
             len(folders), [f["name"] for f in folders])
    return folders


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def gmail_connect(cfg: dict, log: logging.Logger) -> imaplib.IMAP4_SSL:
    log.info("Connecting to Gmail IMAP as %s...", cfg["username"])
    conn = imaplib.IMAP4_SSL(cfg["host"], cfg["port"])
    conn.login(cfg["username"], cfg["password"])
    return conn


def gmail_find_inbox_uid(conn: imaplib.IMAP4_SSL, message_id: str) -> bytes | None:
    """Search Gmail INBOX for a message by Message-ID. Returns UID or None."""
    conn.select("INBOX")
    _, data = conn.uid("SEARCH", None, f'HEADER Message-ID "{message_id}"')
    uids = data[0].split() if data[0] else []
    return uids[0] if uids else None


def gmail_list_labels(conn: imaplib.IMAP4_SSL) -> set:
    """Return set of existing Gmail label names (lowercase for comparison)."""
    _, folders = conn.list()
    labels = set()
    for f in folders:
        decoded = f.decode()
        # Label name is after the last quote or delimiter
        parts = decoded.split('"/"')
        if len(parts) >= 2:
            name = parts[-1].strip().strip('"')
            labels.add(name.lower())
    return labels


def gmail_create_label(conn: imaplib.IMAP4_SSL, label_name: str,
                        log: logging.Logger) -> bool:
    """Create a Gmail label if it doesn't exist."""
    result, _ = conn.create(label_name)
    if result == "OK":
        log.info("Created Gmail label: %s", label_name)
        return True
    else:
        log.warning("Failed to create Gmail label: %s", label_name)
        return False


def gmail_ensure_label(conn: imaplib.IMAP4_SSL, label_name: str,
                        existing_labels: set, log: logging.Logger) -> bool:
    """Create Gmail label if it doesn't already exist. Returns True if usable."""
    if label_name.lower() in existing_labels:
        return True
    return gmail_create_label(conn, label_name, log)


def gmail_move_to_trash(conn: imaplib.IMAP4_SSL, uid: bytes,
                         log: logging.Logger) -> bool:
    """Move a Gmail INBOX message to Trash."""
    try:
        res, _ = conn.uid("COPY", uid, "[Gmail]/Trash")
        if res != "OK":
            log.warning("COPY to Trash failed for UID %s", uid.decode())
            return False
        conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        conn.expunge()
        return True
    except imaplib.IMAP4.error as e:
        log.error("IMAP error moving UID %s to Trash: %s", uid.decode(), e)
        return False


def gmail_move_to_spam(conn: imaplib.IMAP4_SSL, uid: bytes,
                        log: logging.Logger) -> bool:
    """Move a Gmail INBOX message to Spam."""
    try:
        res, _ = conn.uid("COPY", uid, "[Gmail]/Spam")
        if res != "OK":
            log.warning("COPY to Spam failed for UID %s", uid.decode())
            return False
        conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        conn.expunge()
        return True
    except imaplib.IMAP4.error as e:
        log.error("IMAP error moving UID %s to Spam: %s", uid.decode(), e)
        return False


def gmail_archive(conn: imaplib.IMAP4_SSL, uid: bytes,
                   log: logging.Logger) -> bool:
    """Archive a Gmail INBOX message (remove INBOX label, keep in All Mail)."""
    try:
        # Gmail archive = copy to All Mail, delete from INBOX
        res, _ = conn.uid("COPY", uid, "[Gmail]/All Mail")
        if res != "OK":
            log.warning("COPY to All Mail failed for UID %s", uid.decode())
            return False
        conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        conn.expunge()
        return True
    except imaplib.IMAP4.error as e:
        log.error("IMAP error archiving UID %s: %s", uid.decode(), e)
        return False


def gmail_apply_label(conn: imaplib.IMAP4_SSL, uid: bytes, label_name: str,
                       log: logging.Logger) -> bool:
    """
    Apply a label to a Gmail INBOX message and remove it from INBOX.
    This effectively moves it to that label/folder.
    """
    try:
        # Copy to label folder
        res, _ = conn.uid("COPY", uid, label_name)
        if res != "OK":
            log.warning("COPY to label '%s' failed for UID %s", label_name, uid.decode())
            return False
        # Remove from INBOX
        conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        conn.expunge()
        return True
    except imaplib.IMAP4.error as e:
        log.error("IMAP error applying label '%s' to UID %s: %s",
                  label_name, uid.decode(), e)
        return False


# ---------------------------------------------------------------------------
# Sync logic
# ---------------------------------------------------------------------------

def process_folder(proton, gmail: imaplib.IMAP4_SSL, label_id: str,
                   label_name: str, action, page_size: int,
                   dry_run: bool, log: logging.Logger) -> tuple[int, int, int]:
    """
    Fetch messages from a Proton folder, find matching Gmail INBOX messages,
    and apply the given action function to each.

    Returns (actioned, not_found, errors).
    """
    message_ids = fetch_proton_folder_message_ids(
        proton, label_id, label_name, page_size, log
    )
    if not message_ids:
        return 0, 0, 0

    actioned = not_found = errors = 0
    gmail.select("INBOX")

    for mid in message_ids:
        try:
            uid = gmail_find_inbox_uid(gmail, mid)
            if not uid:
                not_found += 1
                continue
            if dry_run:
                log.info("[DRY RUN] Would apply '%s' to: %s", label_name, mid)
                actioned += 1
                continue
            success = action(gmail, uid, log)
            if success:
                log.info("%-12s → applied to Gmail: %s", label_name, mid)
                actioned += 1
            else:
                errors += 1
        except Exception as e:
            log.error("Error processing %s in folder '%s': %s", mid, label_name, e)
            errors += 1

    return actioned, not_found, errors


def run_sync(dry_run: bool, log: logging.Logger) -> None:
    proton_cfg = CONFIG["proton"]
    gmail_cfg = CONFIG["gmail"]

    # Connect to Proton
    proton = proton_load_session(proton_cfg, log)

    # Connect to Gmail
    gmail = gmail_connect(gmail_cfg, log)
    existing_gmail_labels = gmail_list_labels(gmail)

    totals = {"actioned": 0, "not_found": 0, "errors": 0}

    def add_totals(result):
        totals["actioned"]  += result[0]
        totals["not_found"] += result[1]
        totals["errors"]    += result[2]

    # 1. Proton Trash → Gmail Trash
    add_totals(process_folder(
        proton, gmail,
        label_id=PROTON_LABELS["trash"],
        label_name="Trash",
        action=gmail_move_to_trash,
        page_size=proton_cfg["page_size"],
        dry_run=dry_run, log=log,
    ))

    # 2. Proton Spam → Gmail Spam
    add_totals(process_folder(
        proton, gmail,
        label_id=PROTON_LABELS["spam"],
        label_name="Spam",
        action=gmail_move_to_spam,
        page_size=proton_cfg["page_size"],
        dry_run=dry_run, log=log,
    ))

    # 3. Proton Archive → Gmail Archive
    add_totals(process_folder(
        proton, gmail,
        label_id=PROTON_LABELS["archive"],
        label_name="Archive",
        action=gmail_archive,
        page_size=proton_cfg["page_size"],
        dry_run=dry_run, log=log,
    ))

    # 4. Proton custom folders → Gmail labels
    custom_folders = fetch_proton_custom_folders(proton, log)
    for folder in custom_folders:
        folder_name = folder["name"]
        # Ensure Gmail label exists
        if not dry_run:
            gmail_ensure_label(gmail, folder_name, existing_gmail_labels, log)
            # Refresh label list in case we just created one
            existing_gmail_labels = gmail_list_labels(gmail)

        def make_label_action(lname):
            def action(conn, uid, log):
                return gmail_apply_label(conn, uid, lname, log)
            return action

        add_totals(process_folder(
            proton, gmail,
            label_id=folder["id"],
            label_name=folder_name,
            action=make_label_action(folder_name),
            page_size=proton_cfg["page_size"],
            dry_run=dry_run, log=log,
        ))

    gmail.logout()

    log.info(
        "Sync complete. Actioned: %d | Not in Gmail inbox: %d | Errors: %d",
        totals["actioned"], totals["not_found"], totals["errors"],
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Sync Proton Mail folder actions to Gmail."
    )
    parser.add_argument(
        "--login", action="store_true",
        help="Authenticate to Proton interactively and save session.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be changed in Gmail without making any changes.",
    )
    args = parser.parse_args()

    log = setup_logging(CONFIG["log_level"], CONFIG["log_file"])

    if args.login:
        log.info("=== Interactive Proton login ===")
        proton_login(CONFIG["proton"], log)
        log.info("Login complete. You can now run without --login.")
        return

    log.info("=== Proton → Gmail sync run ===")
    run_sync(dry_run=args.dry_run, log=log)


if __name__ == "__main__":
    main()