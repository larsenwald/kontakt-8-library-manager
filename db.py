"""
db.py — Kontakt 8 komplete.db3 read/write logic for KLib
"""

import ctypes
import os
import glob
import shutil
import subprocess
import sqlite3
import apsw
from artwork import forget as forget_artwork
from cleanup import remove_remnants


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


KONTAKT_PROCESSES = ("kontakt 8.exe",)


def _db_in_use(db_path):
    """True if another process has the db open (Kontakt standalone, or Kontakt as a plugin in a DAW)."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    # Ask for the file with no sharing: Windows refuses (ERROR_SHARING_VIOLATION) while anyone has it open
    handle = kernel32.CreateFileW(db_path, 0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ, OPEN_EXISTING
    if handle in (None, ctypes.c_void_p(-1).value):
        return ctypes.GetLastError() == 32
    kernel32.CloseHandle(ctypes.c_void_p(handle))
    return False


def _kontakt_process_running():
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                             creationflags=subprocess.CREATE_NO_WINDOW, timeout=10).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return any(f'"{name}"' in out for name in KONTAKT_PROCESSES)


def is_kontakt_running(db_path):
    """Kontakt standalone is running, or something (e.g. Kontakt inside a DAW) has its db open."""
    return _kontakt_process_running() or (os.path.isfile(db_path) and _db_in_use(db_path))


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


def _repair_fts(cur):
    """Rebuild the f_sound_info FTS index if it has drifted out of sync with its content."""
    try:
        cur.execute("INSERT INTO f_sound_info(f_sound_info, rank) VALUES('integrity-check', 1)")
    except apsw.CorruptError:
        cur.execute("INSERT INTO f_sound_info(f_sound_info) VALUES('rebuild')")


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
        _repair_fts(cur)

        # Fetch the library row first
        rows = list(cur.execute(
            "SELECT path, product_id, alias, upid FROM k_content_path WHERE id = ?", (lib_id,)
        ))
        if not rows:
            return {"success": False, "message": f"Library ID {lib_id} not found.", "details": []}

        lib_path, product_id, alias, upid = rows[0]

        # ── 1. Delete database entries ──
        if delete_db:
            # Names this library's artwork folder may go by, and names other libraries still use
            own_banks = {r[0] for r in cur.execute(
                "SELECT DISTINCT b.entry1 FROM k_sound_info s JOIN k_bank_chain b ON b.id = s.bank_chain_id "
                "WHERE s.content_path_id = ?", (lib_id,))}
            other_names = {r[0] for r in cur.execute(
                "SELECT alias FROM k_content_path WHERE id != ? AND alias IS NOT NULL "
                "UNION SELECT DISTINCT b.entry1 FROM k_sound_info s JOIN k_bank_chain b ON b.id = s.bank_chain_id "
                "WHERE s.content_path_id != ?", (lib_id, lib_id))}

            # Registration first: if the HKLM key survives, Kontakt re-adds the library on launch
            ok, reg_details = remove_remnants(lib_path, product_id, upid,
                                              extra_names={alias} | own_banks, protected_names=other_names)
            details.extend(reg_details)
            if not ok:
                return {
                    "success": False,
                    "message": reg_details[-1].lstrip("✗ ") + ". Nothing was deleted.",
                    "details": details
                }
            # KLib's own header colour / banner text for this library (vendor rows are left alone)
            try:
                forget_artwork(({alias} | own_banks) - other_names)
            except (OSError, sqlite3.Error) as e:
                details.append(f"✗ Could not clear header colour / banner text: {e}")

            # Get all sound_info IDs for this library (needed for junction tables)
            sound_ids = [r[0] for r in cur.execute(
                "SELECT id FROM k_sound_info WHERE content_path_id = ?", (lib_id,)
            )]

            # One transaction: a failure part-way rolls back instead of leaving half-deleted rows
            with conn:
                if sound_ids:
                    placeholders = ",".join("?" * len(sound_ids))

                    # Delete category/mode junction rows.
                    # The f_sound_info FTS index is maintained by Kontakt's triggers — never
                    # delete from it directly, or the index desyncs and reports "malformed".
                    cur.execute(f"DELETE FROM k_sound_info_category WHERE sound_info_id IN ({placeholders})", sound_ids)
                    cur.execute(f"DELETE FROM k_sound_info_mode WHERE sound_info_id IN ({placeholders})", sound_ids)

                    # Delete from all preset tables
                    for table in PRESET_TABLES:
                        try:
                            cur.execute(f"DELETE FROM {table} WHERE content_path_id = ?", (lib_id,))
                        except apsw.SQLError:
                            pass  # table may not exist in every db version

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


def browser_name(db_path, lib_id):
    """The library's folder name in Kontakt's browser: its patches' most common top-level bank."""
    conn = _open(db_path)
    try:
        rows = list(conn.cursor().execute(
            "SELECT b.entry1, COUNT(*) AS n FROM k_sound_info s JOIN k_bank_chain b ON b.id = s.bank_chain_id "
            "WHERE s.content_path_id = ? GROUP BY b.entry1 ORDER BY n DESC LIMIT 1", (lib_id,)))
        return rows[0][0] if rows else None
    finally:
        conn.close()


def set_bank_name(db_path, lib_id, new_name, changed_paths):
    """
    Point the given patches (files already renamed on disk) at bank `new_name`, keeping their
    sub-levels, and refresh file_size / mod_date so the db matches the edited files.
    Returns the number of patches updated.
    """
    wanted = {os.path.normcase(os.path.normpath(p)) for p in changed_paths}
    conn = _open(db_path)
    try:
        _checkpoint(conn)
        cur = conn.cursor()
        _repair_fts(cur)
        rows = [r for r in cur.execute(
            "SELECT s.id, s.file_name, b.bcvendor, b.entry2, b.entry3 FROM k_sound_info s "
            "JOIN k_bank_chain b ON b.id = s.bank_chain_id WHERE s.content_path_id = ?", (lib_id,))
            if os.path.normcase(os.path.normpath(r[1])) in wanted]

        with conn:
            chains = {}
            for sound_id, file_name, bcvendor, entry2, entry3 in rows:
                key = (bcvendor or "", entry2, entry3)
                if key not in chains:
                    found = list(cur.execute(
                        "SELECT id FROM k_bank_chain WHERE bcvendor = ? AND entry1 = ? "
                        "AND entry2 IS ? AND entry3 IS ?", (key[0], new_name, entry2, entry3)))
                    if found:
                        chains[key] = found[0][0]
                    else:
                        cur.execute("INSERT INTO k_bank_chain(bcvendor, entry1, entry2, entry3) VALUES(?, ?, ?, ?)",
                                    (key[0], new_name, entry2, entry3))
                        chains[key] = conn.last_insert_rowid()
                st = os.stat(file_name)
                cur.execute("UPDATE k_sound_info SET bank_chain_id = ?, file_size = ?, mod_date = ? WHERE id = ?",
                            (chains[key], st.st_size, int(st.st_mtime) * 1_000_000_000, sound_id))

            # Bank chains nothing points at any more (e.g. the old 'Kontakt' one)
            cur.execute("DELETE FROM k_bank_chain WHERE id NOT IN "
                        "(SELECT bank_chain_id FROM k_sound_info WHERE bank_chain_id IS NOT NULL)")
            # Kontakt's update_f_sound_info trigger runs AFTER the update, so its 'delete' reads the
            # new bank name and desyncs the FTS index — rebuild it from v_sound_info instead
            cur.execute("INSERT INTO f_sound_info(f_sound_info) VALUES('rebuild')")

        _checkpoint(conn)
        return len(rows)
    finally:
        conn.close()


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