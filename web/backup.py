import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", str(REPO_ROOT / "data")))
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", str(DATA_DIR / "backups")))
PLAYERS_DIR = DATA_DIR / "players"
PHOTOS_DIR = REPO_ROOT / "web" / "static" / "player-photos"
OUTPUT_DIR = DATA_DIR / "output"   # bot model JSONs live here as {job_id}.json
CACHE_DB = DATA_DIR / "cache.sqlite"  # opening explorer cache (accumulated from API calls)

_status: dict = {}
_lock = threading.Lock()


def _clean_db_url(url: str) -> str:
    """Strip query params (e.g. ?connect_timeout=10) before passing to pg tools."""
    return re.sub(r"\?.*$", "", url)


def _update(op_id: str, **kwargs) -> None:
    with _lock:
        _status[op_id].update(kwargs)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n/1024:.1f} KB"
    return f"{n/1024**2:.1f} MB"


def get_status(op_id: str) -> dict:
    with _lock:
        return dict(_status.get(op_id, {"status": "unknown"}))


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def create_backup(description: str = "") -> dict:
    backup_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = BACKUP_DIR / backup_id

    with _lock:
        _status[backup_id] = {
            "id": backup_id,
            "status": "running",
            "message": "Starting backup…",
            "description": description,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
        }

    def _run():
        try:
            backup_path.mkdir(parents=True, exist_ok=True)

            # 1. Git commit hash
            try:
                git_commit = subprocess.check_output(
                    ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                    text=True, stderr=subprocess.DEVNULL,
                ).strip()
            except Exception:
                git_commit = "unknown"

            # 2. pg_dump
            db_url = _clean_db_url(os.environ.get("DATABASE_URL", ""))
            db_path = backup_path / "db.sql"
            if db_url:
                _update(backup_id, message="Dumping database…")
                result = subprocess.run(
                    ["pg_dump", "--clean", "--if-exists", "--no-password", "--dbname", db_url],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"pg_dump failed: {result.stderr.strip()}")
                db_path.write_text(result.stdout)
            else:
                db_path.write_text("-- No DATABASE_URL configured\n")

            # 3. Archive players dir, photos, and bot model JSONs
            _update(backup_id, message="Archiving files…")
            files_path = backup_path / "files.tar.gz"
            with tarfile.open(files_path, "w:gz") as tar:
                if PLAYERS_DIR.exists():
                    tar.add(PLAYERS_DIR, arcname="players")
                if PHOTOS_DIR.exists():
                    tar.add(PHOTOS_DIR, arcname="player-photos")
                # Only back up JSON files from output — these are bot models.
                # PGN files (.pgn) are large job outputs that can be regenerated.
                if OUTPUT_DIR.exists():
                    for f in OUTPUT_DIR.glob("*.json"):
                        tar.add(f, arcname=f"bots/{f.name}")

            # 4. cache.sqlite — use SQLite online backup API for a consistent live snapshot
            _update(backup_id, message="Archiving cache database…")
            if CACHE_DB.exists():
                cache_backup_path = backup_path / "cache.sqlite"
                src = sqlite3.connect(str(CACHE_DB))
                dst = sqlite3.connect(str(cache_backup_path))
                src.backup(dst)
                dst.close()
                src.close()

            # 5. meta.json
            db_size = db_path.stat().st_size if db_path.exists() else 0
            files_size = files_path.stat().st_size if files_path.exists() else 0
            meta = {
                "id": backup_id,
                "description": description,
                "git_commit": git_commit,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "db_size": db_size,
                "db_size_fmt": _fmt_size(db_size),
                "files_size": files_size,
                "files_size_fmt": _fmt_size(files_size),
            }
            (backup_path / "meta.json").write_text(json.dumps(meta, indent=2))

            _update(backup_id,
                    status="done",
                    message="Backup complete.",
                    finished_at=datetime.now(timezone.utc).isoformat())

        except Exception as exc:
            _update(backup_id,
                    status="failed",
                    message=str(exc),
                    finished_at=datetime.now(timezone.utc).isoformat())
            shutil.rmtree(backup_path, ignore_errors=True)

    threading.Thread(target=_run, daemon=True).start()
    return {"id": backup_id}


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def list_backups() -> list:
    if not BACKUP_DIR.exists():
        return []
    backups = []
    for d in sorted(BACKUP_DIR.iterdir(), reverse=True):
        meta_path = d / "meta.json"
        if meta_path.exists():
            try:
                backups.append(json.loads(meta_path.read_text()))
            except Exception:
                pass
    return backups


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def restore_backup(backup_id: str) -> dict:
    backup_path = BACKUP_DIR / backup_id
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup {backup_id!r} not found")

    op_id = f"restore-{backup_id}"
    with _lock:
        _status[op_id] = {
            "id": op_id,
            "status": "running",
            "message": "Starting restore…",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
        }

    def _run():
        try:
            # 1. Restore database
            db_url = _clean_db_url(os.environ.get("DATABASE_URL", ""))
            db_path = backup_path / "db.sql"
            if db_url and db_path.exists():
                _update(op_id, message="Restoring database…")
                result = subprocess.run(
                    ["psql", "--no-password", "--dbname", db_url, "--file", str(db_path)],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"psql failed: {result.stderr.strip()}")

            # 2. Restore files
            files_path = backup_path / "files.tar.gz"
            if files_path.exists():
                _update(op_id, message="Restoring files…")
                if PLAYERS_DIR.exists():
                    shutil.rmtree(PLAYERS_DIR)
                if PHOTOS_DIR.exists():
                    shutil.rmtree(PHOTOS_DIR)
                PLAYERS_DIR.mkdir(parents=True, exist_ok=True)
                PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
                OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

                with tarfile.open(files_path, "r:gz") as tar:
                    for member in tar.getmembers():
                        if member.name.startswith("players/"):
                            member.name = member.name[len("players/"):]
                            if member.name:
                                tar.extract(member, PLAYERS_DIR)
                        elif member.name.startswith("player-photos/"):
                            member.name = member.name[len("player-photos/"):]
                            if member.name:
                                tar.extract(member, PHOTOS_DIR)
                        elif member.name.startswith("bots/"):
                            member.name = member.name[len("bots/"):]
                            if member.name:
                                tar.extract(member, OUTPUT_DIR)

            # 3. Restore cache.sqlite
            cache_backup = backup_path / "cache.sqlite"
            if cache_backup.exists():
                _update(op_id, message="Restoring cache database…")
                shutil.copy2(str(cache_backup), str(CACHE_DB))

            # 4. Read git commit from meta
            meta_path = backup_path / "meta.json"
            git_commit = "unknown"
            if meta_path.exists():
                git_commit = json.loads(meta_path.read_text()).get("git_commit", "unknown")

            _update(op_id,
                    status="done",
                    message=(
                        f"Restore complete. "
                        f"Git commit at backup time: {git_commit[:8]}. "
                        f"Restart the server to apply all changes."
                    ),
                    git_commit=git_commit,
                    finished_at=datetime.now(timezone.utc).isoformat())

        except Exception as exc:
            _update(op_id,
                    status="failed",
                    message=str(exc),
                    finished_at=datetime.now(timezone.utc).isoformat())

    threading.Thread(target=_run, daemon=True).start()
    return {"id": op_id}


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def delete_backup(backup_id: str) -> None:
    backup_path = BACKUP_DIR / backup_id
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup {backup_id!r} not found")
    shutil.rmtree(backup_path)
