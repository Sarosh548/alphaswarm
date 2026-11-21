"""Daily backups for the AlphaSwarm platform (in-app, no OS cron).

Once a day (and once at startup) the platform database and every bot
journal are copied into <data_dir>/backups/YYYY-MM-DD/ using SQLite's
own backup API, so a copy is always a consistent snapshot even while
the server is running. Only the newest RETENTION_DAYS dated folders
are kept. An admin can also trigger a backup from the API and
download the latest one as a zip.

Restore = stop the server, copy the dated folder's platform.db back
to <data_dir>/platform.db and each bots/<id>/journal.db back to
<data_dir>/bots/<id>/journal.db, then start the server (see
deploy/DEPLOY_RUNBOOK.md).
"""

import io
import json
import os
import shutil
import sqlite3
import time
import zipfile
from datetime import date

RETENTION_DAYS = 7


def backups_root(data_dir):
    return os.path.join(data_dir, "backups")


def _sqlite_copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def backup_once(data_dir, keep=RETENTION_DAYS):
    """Take one backup of the platform DB + every bot journal into
    backups/<today>/. Returns (folder_path, [relative file names]).
    Idempotent within a day: re-running refreshes today's folder."""
    today = date.today().isoformat()
    root = backups_root(data_dir)
    dest = os.path.join(root, today)
    os.makedirs(dest, exist_ok=True)
    files = []

    platform_db = os.path.join(data_dir, "platform.db")
    if os.path.exists(platform_db):
        _sqlite_copy(platform_db, os.path.join(dest, "platform.db"))
        files.append("platform.db")

    bots_dir = os.path.join(data_dir, "bots")
    if os.path.isdir(bots_dir):
        for bot_id in sorted(os.listdir(bots_dir)):
            journal = os.path.join(bots_dir, bot_id, "journal.db")
            if os.path.exists(journal):
                rel = os.path.join("bots", bot_id, "journal.db")
                _sqlite_copy(journal, os.path.join(dest, rel))
                files.append(rel)

    # The shared news/alert logs are small and part of the record.
    for name in ("events.jsonl", "alerts.jsonl"):
        src = os.path.join(data_dir, name)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(dest, name))
            files.append(name)

    manifest = {"date": today, "created_at_ms": int(time.time() * 1000),
                "files": files}
    with open(os.path.join(dest, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    _prune(root, keep)
    return dest, files


def _prune(root, keep):
    """Keep only the newest `keep` dated backup folders."""
    if not os.path.isdir(root):
        return
    dated = []
    for name in os.listdir(root):
        try:
            d = date.fromisoformat(name)
        except ValueError:
            continue
        if os.path.isdir(os.path.join(root, name)):
            dated.append((d, name))
    dated.sort(reverse=True)
    for _, name in dated[keep:]:
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)


def latest_backup(data_dir):
    root = backups_root(data_dir)
    if not os.path.isdir(root):
        return None
    dated = sorted(
        (name for name in os.listdir(root)
         if os.path.isdir(os.path.join(root, name))
         and _is_date(name)),
        reverse=True)
    return os.path.join(root, dated[0]) if dated else None


def _is_date(name):
    try:
        date.fromisoformat(name)
        return True
    except ValueError:
        return False


def latest_backup_zip(data_dir):
    """Zip bytes of the newest backup folder (taking a fresh backup
    first if none exists). Returns (filename, bytes)."""
    folder = latest_backup(data_dir)
    if folder is None:
        folder, _ = backup_once(data_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _, filenames in os.walk(folder):
            for name in filenames:
                full = os.path.join(dirpath, name)
                z.write(full, os.path.relpath(full, folder))
    return f"alphaswarm-backup-{os.path.basename(folder)}.zip", \
        buf.getvalue()


def backup_loop(data_dir, stop_event):
    """Daily backup thread: one backup at startup, then one per day.
    Failures are reported on stdout but never kill the thread."""
    while True:
        try:
            folder, files = backup_once(data_dir)
            print(f"Backup done: {folder} ({len(files)} files)",
                  flush=True)
        except Exception as exc:  # keep the platform alive
            print(f"Backup failed: {type(exc).__name__}: {exc}",
                  flush=True)
        # Sleep in 1-hour slices so a stop signal is honoured promptly.
        for _ in range(24):
            if stop_event.wait(3600):
                return
