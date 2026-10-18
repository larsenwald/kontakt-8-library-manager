"""
main.py — KLib entry point
Launches a pywebview window and exposes the Python API to the HTML frontend.
"""

import ctypes
import os
import re
import subprocess
import sys
import webview
from db import get_libraries, delete_library, default_db_path, is_kontakt_running, set_bank_name
from patches import scan_library, set_library_name, summarize
from cleanup import is_admin
from register import check_new_library, inspect_folder, register_library, suggest_snpid
from nicnt import NicntError, generate_nicnt


def _name_from_folder(folder):
    """A starting library name from a download folder: drop [tags], 'rutracker.org' and version numbers."""
    name = os.path.basename(os.path.normpath(folder))
    name = re.sub(r"\[[^\]]*\]|\([^)]*\)|rutracker\.org", " ", name, flags=re.I)
    name = re.sub(r"\bv?\d+(\.\d+)+\b", " ", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip(" -_.")


def resource_path(relative):
    """Resolve path whether running as script or PyInstaller bundle."""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative)
    return os.path.join(os.path.dirname(__file__), relative)


class KLibAPI:
    """Methods exposed to the JS frontend via window.pywebview.api.*"""

    def __init__(self):
        self._db_path = None

    # ── Called on startup ──
    def init(self):
        """
        Auto-detect the Kontakt 8 db. Returns:
          { found: bool, db_path: str|None, locked: bool, libraries: list }
        """
        path = default_db_path()
        if path:
            self._db_path = path
            locked = is_kontakt_running(path)
            if locked:
                return {"found": True, "db_path": path, "locked": True, "libraries": []}
            try:
                libs = get_libraries(path)
                return {"found": True, "db_path": path, "locked": False, "libraries": libs}
            except Exception as e:
                return {"found": True, "db_path": path, "locked": False, "libraries": [], "error": str(e)}
        return {"found": False, "db_path": None, "locked": False, "libraries": []}

    # ── Manual db picker ──
    def pick_database(self):
        """Open a file picker and load a manually selected db."""
        result = webview.windows[0].create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("SQLite Database (*.db3)", "All files (*.*)"),
        )
        if not result:
            return {"found": False, "db_path": None, "libraries": []}

        path = result[0]
        self._db_path = path
        locked = is_kontakt_running(path)
        if locked:
            return {"found": True, "db_path": path, "locked": True, "libraries": []}
        try:
            libs = get_libraries(path)
            return {"found": True, "db_path": path, "locked": False, "libraries": libs}
        except Exception as e:
            return {"found": True, "db_path": path, "locked": False, "libraries": [], "error": str(e)}

    # ── Add a library ──
    def pick_library_folder(self):
        """Folder picker → preview of what adding it would register (nothing is written)."""
        result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return {"cancelled": True}
        folder = result[0] if isinstance(result, (list, tuple)) else result
        info = inspect_folder(folder)
        info["folder"] = os.path.normpath(folder)
        info["patches"] = summarize(scan_library(folder))
        if info.get("needs_nicnt"):
            own = [n for n in info["patches"]["names"] if n and n != "Kontakt"]
            info["suggested"] = {
                "name": own[0] if len(own) == 1 else _name_from_folder(folder),
                "snpid": suggest_snpid(self._db_product_ids()),
            }
        return info

    def check_new_nicnt(self, name, snpid):
        return check_new_library(name, snpid, self._db_product_ids())

    def _db_product_ids(self):
        db_path = self._db_path or default_db_path()
        try:
            return [l["product_id"] for l in get_libraries(db_path)] if db_path else []
        except Exception:
            return []

    def add_library(self, folder, library_name=None, new_nicnt=None):
        db_path = self._db_path or default_db_path()
        if db_path and is_kontakt_running(db_path):
            return {
                "success": False,
                "message": "Kontakt is open. Close it before adding libraries.",
                "details": []
            }
        details = []
        # No .nicnt: generate one first (name / company / SNPID from the add dialog)
        if new_nicnt:
            name, snpid = new_nicnt.get("name", "").strip(), new_nicnt.get("snpid", "").strip().upper()
            problems = check_new_library(name, snpid, self._db_product_ids())
            if problems:
                return {"success": False, "message": problems[0], "details": problems}
            try:
                path = generate_nicnt(folder, name, new_nicnt.get("company", "").strip(), snpid)
            except (NicntError, OSError) as e:
                return {"success": False, "message": f"Could not create the .nicnt: {e}", "details": []}
            details.append(f"✓ Created {path}")
            library_name = library_name or name
        # Name the patches before Kontakt's first scan, so it files them correctly from the start
        if library_name:
            renamed = set_library_name(folder, library_name)
            if renamed["failed"]:
                return {"success": False,
                        "message": f"Could not rename {len(renamed['failed'])} patch(es); nothing was registered.",
                        "details": [f"✗ {rel}: {err}" for rel, err in renamed["failed"]]}
            if renamed["changed"]:
                details.append(f"✓ Set library name on {len(renamed['changed'])} patch(es) "
                               f"(originals backed up to {renamed['backup_dir']})")
        result = register_library(folder)
        result["details"] = details + result["details"]
        return result

    # ── Library name stored in patches ──
    def library_patches(self, lib_id):
        lib = next((l for l in get_libraries(self._db_path) if l["id"] == lib_id), None) if self._db_path else None
        if not lib:
            return {"found": False}
        return {"found": True, "folder_exists": os.path.isdir(lib["path"]),
                "patches": summarize(scan_library(lib["path"]))}

    def set_library_name(self, lib_id, new_name):
        if not self._db_path:
            return {"success": False, "message": "No database loaded.", "details": []}
        if is_kontakt_running(self._db_path):
            return {"success": False, "message": "Kontakt is open. Close it before renaming.", "details": []}
        new_name = (new_name or "").strip()
        lib = next((l for l in get_libraries(self._db_path) if l["id"] == lib_id), None)
        if not lib or not new_name:
            return {"success": False, "message": "Library not found or name empty.", "details": []}

        renamed = set_library_name(lib["path"], new_name, label=lib["alias"])
        changed_paths = [os.path.join(lib["path"], rel) for rel in renamed["changed"]]
        details = [f"✗ {rel}: {err}" for rel, err in renamed["failed"]]
        try:
            updated = set_bank_name(self._db_path, lib_id, new_name, changed_paths) if changed_paths else 0
        except Exception as e:
            return {"success": False,
                    "message": f"Patches were renamed but the database update failed: {e}",
                    "details": details + [f"Originals: {renamed['backup_dir']}"]}
        if not renamed["changed"]:
            return {"success": True, "message": f"All patches already say '{new_name}'.", "details": details}
        return {"success": not renamed["failed"],
                "message": f"Renamed {len(renamed['changed'])} patch(es) to '{new_name}' "
                           f"({updated} updated in Kontakt's database).",
                "details": details + [f"Originals backed up to {renamed['backup_dir']}"]}

    # ── Delete a library ──
    def delete_library(self, lib_id, delete_db, delete_files, delete_cache):
        if not self._db_path:
            return {"success": False, "message": "No database loaded.", "details": []}
        if is_kontakt_running(self._db_path):
            return {
                "success": False,
                "message": "Kontakt is open. Close it before deleting libraries.",
                "details": []
            }
        return delete_library(
            self._db_path,
            lib_id,
            delete_db=delete_db,
            delete_files=delete_files,
            delete_cache=delete_cache,
        )

    # ── Utility: get folder size for the confirmation modal ──
    def get_folder_size(self, path):
        """Return total size of a folder in bytes."""
        total = 0
        try:
            for dirpath, _, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    try:
                        total += os.path.getsize(fp)
                    except OSError:
                        pass
        except Exception:
            pass
        return total


def relaunch_as_admin():
    """Re-run this process elevated (UAC prompt). Removing a library's HKLM registration needs admin."""
    if getattr(sys, "frozen", False):
        args = sys.argv[1:]
    else:
        args = [os.path.abspath(sys.argv[0])] + sys.argv[1:]
    params = subprocess.list2cmdline(args)
    # ShellExecuteW returns > 32 on success
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1) > 32


def main():
    if not is_admin():
        if relaunch_as_admin():
            return
        # UAC declined — run anyway; deletes will report that admin is required

    api = KLibAPI()
    html_path = resource_path("kontakt_manager.html")

    window = webview.create_window(
        title="KLib — Kontakt Library Manager",
        url=f"file:///{html_path.replace(os.sep, '/')}",
        js_api=api,
        width=960,
        height=640,
        min_size=(680, 480),
        resizable=True,
        frameless=False,
        easy_drag=False,
    )

    webview.start(debug=False)


if __name__ == "__main__":
    main()
