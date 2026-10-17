"""
main.py — KLib entry point
Launches a pywebview window and exposes the Python API to the HTML frontend.
"""

import ctypes
import os
import subprocess
import sys
import webview
from db import get_libraries, delete_library, default_db_path, is_kontakt_running
from cleanup import is_admin


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
