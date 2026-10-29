"""Database backup and restore with integrity manifests."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.utils import timezone


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup(dest_dir=None):
    dest = Path(dest_dir or settings.VCOOS_BACKUP_ROOT)
    dest.mkdir(parents=True, exist_ok=True)
    stamp = timezone.now().strftime("%Y%m%dT%H%M%S%f")
    vendor = connection.vendor
    if vendor == "sqlite":
        out = dest / f"vcoos-{stamp}.sqlite3"
        connection.ensure_connection()
        target = sqlite3.connect(out)
        with target:
            connection.connection.backup(target)  # consistent online snapshot
        target.close()
    elif vendor == "postgresql":
        db = settings.DATABASES["default"]
        out = dest / f"vcoos-{stamp}.pgdump"
        env = _pg_env(db)
        subprocess.run(["pg_dump", "-Fc", "-h", db["HOST"], "-p", str(db["PORT"]), "-U", db["USER"], "-f", str(out), db["NAME"]],
                       check=True, env=env, timeout=3600)
    else:
        raise RuntimeError(f"unsupported database vendor {vendor}")
    manifest = {"file": out.name, "sha256": _sha256(out), "vendor": vendor, "created_at": timezone.now().isoformat()}
    (dest / f"{out.name}.manifest.json").write_text(json.dumps(manifest, indent=2))
    return out, manifest


def verify_backup(path):
    path = Path(path)
    manifest = json.loads(Path(f"{path}.manifest.json").read_text())
    if _sha256(path) != manifest["sha256"]:
        return False, "checksum mismatch"
    if manifest["vendor"] == "sqlite":
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            res = con.execute("PRAGMA integrity_check").fetchone()[0]
            tables = con.execute("SELECT count(*) FROM sqlite_master WHERE name='core_auditevent'").fetchone()[0]
        finally:
            con.close()
        if res != "ok" or not tables:
            return False, f"integrity check failed: {res}"
    elif manifest["vendor"] == "postgresql":
        # pg_restore --list parses the whole archive table of contents; a truncated/corrupt dump fails here.
        proc = subprocess.run(["pg_restore", "--list", str(path)], capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            return False, f"archive unreadable: {proc.stderr.strip()[:300]}"
        if "core_auditevent" not in proc.stdout:
            return False, "archive does not contain the audit table"
    return True, "ok"


def _pg_env(db):
    return {**os.environ, "PGPASSWORD": db.get("PASSWORD", "")}


def restore_postgres(path, target_db=None):
    """Restore a verified pg_dump archive into `target_db` (default: the configured database).
    Stop the web and engine services first; existing objects are dropped and recreated."""
    ok, why = verify_backup(path)
    if not ok:
        raise RuntimeError(f"refusing to restore: {why}")
    db = settings.DATABASES["default"]
    target_db = target_db or db["NAME"]
    proc = subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner", "--exit-on-error", "-h", db["HOST"], "-p", str(db["PORT"]),
                           "-U", db["USER"], "-d", target_db, str(path)], capture_output=True, text=True, env=_pg_env(db), timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(f"pg_restore failed: {proc.stderr.strip()[:500]}")
    return target_db


def restore_sqlite(path, target=None):
    """Restore a verified SQLite backup over the configured database (stop all services first)."""
    ok, why = verify_backup(path)
    if not ok:
        raise RuntimeError(f"refusing to restore: {why}")
    target = Path(target or settings.DATABASES["default"]["NAME"])
    connection.close()
    if target.exists():
        shutil.copy2(target, f"{target}.pre-restore")
    shutil.copy2(path, target)
    return target
