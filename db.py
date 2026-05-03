"""
db.py — Kontakt 8 komplete.db3 read/write logic for KLib
"""

import os
import glob
import shutil
import apsw


LIBRARIES_CACHE_DIR = "LibrariesCache"

# Tables that reference k_content_path.id via content_path_id
PRESET_TABLES = [
    "k_sound_info",
    "p_sound_info_Instrument_1",
    "p_sound_info_Instrument_2",
    "p_sound_info_Combined_1",
    "p_sound_info_Combined_2",
    "p_sound_info_Tool_1",
    "p_sound_info_Tool_2",
    "p_sound_info_Leap_1",
    "p_sound_info_Leap_2",
    "p_sound_info_Loop_1",
    "p_sound_info_Loop_2",
    "p_sound_info_Oneshot_1",
    "p_sound_info_Oneshot_2",
]


def default_db_path():
    """Return the standard Kontakt 8 db path, or None if not found."""
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    path = os.path.join(local_app_data, "Native Instruments", "Kontakt 8", "komplete.db3")
    return path if os.path.isfile(path) else None


def is_kontakt_running(db_path):
    """Check if Kontakt is running by inspecting lock.lck size."""
    lock = os.path.join(os.path.dirname(db_path), "lock.lck")
    try:
        return os.path.isfile(lock) and os.path.getsize(lock) > 0
    except OSError:
        return False


def _open(db_path):
    """Open a connection with the KOMPLETE collation registered."""
    conn = apsw.Connection(db_path)
    # NI registers a custom 'KOMPLETE' collation at runtime.
    # We register a simple passthrough so SQLite doesn't throw on columns that use it.
    conn.createcollation("KOMPLETE", lambda a, b: (a > b) - (a < b))
    return conn


def _checkpoint(conn):
    """Merge WAL into main db file before any read/write."""
    conn.cursor().execute("PRAGMA wal_checkpoint(TRUNCATE)")


def get_libraries(db_path):
    """
    Return all libraries from k_content_path as a list of dicts.
    Includes preset count and whether the library is NI-protected.
    """
    conn = _open(db_path)
    cur = conn.cursor()

    rows = list(cur.execute(
        "SELECT id, path, content_type, alias, state, product_id, upid, product_version_s "
        "FROM k_content_path ORDER BY id"
    ))

    libraries = []
    for row in rows:
        lib_id, path, content_type, alias, state, product_id, upid, version = row

        # Count presets indexed for this library
        preset_count = list(cur.execute(
            "SELECT COUNT(*) FROM k_sound_info WHERE content_path_id = ?", (lib_id,)
        ))[0][0]

        # content_type 1 = NI native (protected), 2 = user library
        is_native = content_type == 1

        libraries.append({
            "id": lib_id,
            "path": path,
            "alias": alias,
            "state": state,
            "product_id": product_id or "",
            "upid": upid or "",
            "product_version": version or "1.0.0.0",
            "preset_count": preset_count,
            "is_native": is_native,
            "vendor": _infer_vendor(path),
        })

    conn.close()
    return libraries


def delete_library(db_path, lib_id, delete_db=True, delete_files=False, delete_cache=True):
    """
    Remove a library from Kontakt 8.

    Args:
        db_path:      Full path to komplete.db3
        lib_id:       The k_content_path.id of the library to remove
        delete_db:    Remove all db entries (k_content_path + preset rows)
        delete_files: Delete the sample folder on disk
        delete_cache: Delete the LibrariesCache .cache file

    Returns:
        dict with keys: success (bool), message (str), details (list of str)
    """
    details = []
    conn = None

    try:
        # ── Safety: refuse if Kontakt is running ──
        if is_kontakt_running(db_path):
            return {
                "success": False,
                "message": "Kontakt is currently running. Close it before making changes.",
                "details": []
            }

        conn = _open(db_path)
        _checkpoint(conn)
        cur = conn.cursor()

        # Rebuild any broken indexes before attempting writes
        cur.execute("REINDEX")

        # Fetch the library row first
        rows = list(cur.execute(
            "SELECT path, product_id, alias FROM k_content_path WHERE id = ?", (lib_id,)
        ))
        if not rows:
            return {"success": False, "message": f"Library ID {lib_id} not found.", "details": []}

        lib_path, product_id, alias = rows[0]

        # ── 1. Delete database entries ──
        if delete_db:
            # Get all sound_info IDs for this library (needed for junction tables)
            sound_ids = [r[0] for r in cur.execute(
                "SELECT id FROM k_sound_info WHERE content_path_id = ?", (lib_id,)
            )]

            if sound_ids:
                placeholders = ",".join("?" * len(sound_ids))

                # Delete category/mode junction rows
                cur.execute(f"DELETE FROM k_sound_info_category WHERE sound_info_id IN ({placeholders})", sound_ids)
                cur.execute(f"DELETE FROM k_sound_info_mode WHERE sound_info_id IN ({placeholders})", sound_ids)

                # Delete FTS index rows
                try:
                    cur.execute(f"DELETE FROM f_sound_info WHERE rowid IN ({placeholders})", sound_ids)
                except Exception:
                    pass  # FTS table may not support direct delete on all builds

                # Delete from all preset tables
                for table in PRESET_TABLES:
                    try:
                        cur.execute(f"DELETE FROM {table} WHERE content_path_id = ?", (lib_id,))
                    except Exception:
                        pass

                # Delete bank_chain rows unique to this library
                bc_ids = [r[0] for r in cur.execute(
                    "SELECT DISTINCT bank_chain_id FROM k_sound_info WHERE content_path_id = ?", (lib_id,)
                )]
                for bc_id in bc_ids:
                    shared = list(cur.execute(
                        "SELECT COUNT(*) FROM k_sound_info WHERE bank_chain_id = ? AND content_path_id != ?",
                        (bc_id, lib_id)
                    ))[0][0]
                    if shared == 0:
                        cur.execute("DELETE FROM k_bank_chain WHERE id = ?", (bc_id,))

                # Delete main sound_info rows
                cur.execute(f"DELETE FROM k_sound_info WHERE content_path_id = ?", (lib_id,))

            # Delete the library entry itself
            cur.execute("DELETE FROM k_content_path WHERE id = ?", (lib_id,))

            # Checkpoint again to flush WAL after writes
            _checkpoint(conn)
            details.append(f"✓ Removed '{alias}' from database ({len(sound_ids)} presets cleared)")

        # ── 2. Delete cache file ──
        if delete_cache and product_id:
            cache_dir = os.path.join(os.path.dirname(db_path), LIBRARIES_CACHE_DIR)
            pattern = os.path.join(cache_dir, f"K{product_id}*.cache")
            matches = glob.glob(pattern)
            for f in matches:
                try:
                    os.remove(f)
                    details.append(f"✓ Deleted cache: {os.path.basename(f)}")
                except OSError as e:
                    details.append(f"✗ Could not delete cache {os.path.basename(f)}: {e}")

        # ── 3. Delete files on disk ──
        if delete_files and lib_path:
            if os.path.isdir(lib_path):
                try:
                    shutil.rmtree(lib_path)
                    details.append(f"✓ Deleted folder: {lib_path}")
                except OSError as e:
                    details.append(f"✗ Could not delete folder (try running as admin): {e}")
            else:
                details.append(f"⚠ Folder not found on disk (may have already been deleted): {lib_path}")

        return {
            "success": True,
            "message": f"'{alias}' successfully removed.",
            "details": details
        }

    except apsw.Error as e:
        return {
            "success": False,
            "message": f"Database error: {e}",
            "details": details
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Unexpected error: {e}",
            "details": details
        }
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _infer_vendor(path):
    known = [
        "Audio Imperia", "Heavyocity", "Keepforest", "Soundiron",
        "ProjectSAM", "Embertone", "Spitfire", "Output", "Zero-G",
        "Musical Sampling", "Cinematic Studio", "Impact Soundworks",
        "Void & Vista", "Native Instruments", "Red Room Audio",
        "Best Service",
    ]
    path_lower = path.lower()
    for v in known:
        if v.lower() in path_lower:
            return v
    return None