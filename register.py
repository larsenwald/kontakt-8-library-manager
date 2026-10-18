"""
register.py — adds a library to Kontakt 8 from its .nicnt (the inverse of cleanup.py)

Writes the library's system registration; Kontakt picks the library up and indexes it the next
time it starts (it re-registers anything with an HKLM\\SOFTWARE\\Native Instruments\\<RegKey> key).
"""

import json
import os
import winreg

from cleanup import (NI_KEY, SERVICE_CENTER_DIR, INSTALLED_PRODUCTS_DIR, NI_RESOURCES_IMAGE_DIR,
                     _WOW64, _norm, is_admin)
from nicnt import NicntError, extract, find_nicnt, read_nicnt

CONTENT_VERSION = "1.0.0"


def _registered_dir(reg_key):
    """ContentDir of an existing HKLM registration, or None."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, NI_KEY + "\\" + reg_key, 0,
                            winreg.KEY_READ | _WOW64) as k:
            return winreg.QueryValueEx(k, "ContentDir")[0]
    except OSError:
        return None


def inspect_folder(folder):
    """
    What adding this folder would do, without changing anything. Returns:
      { ok, message, nicnt, product, already_registered }
    """
    folder = os.path.normpath(folder)
    try:
        nicnt_path = find_nicnt(folder)
        if not nicnt_path:
            return {"ok": False, "message": "This folder has no .nicnt file.", "nicnt": None,
                    "product": None, "already_registered": False}
        product = read_nicnt(nicnt_path)["product"]
    except (NicntError, OSError) as e:
        return {"ok": False, "message": f"Could not read the .nicnt: {e}", "nicnt": None,
                "product": None, "already_registered": False}

    if not product["reg_key"]:
        return {"ok": False, "message": "The .nicnt has no library name (RegKey/Name).",
                "nicnt": nicnt_path, "product": product, "already_registered": False}

    existing = _registered_dir(product["reg_key"])
    if existing and _norm(existing) != _norm(folder):
        return {"ok": False,
                "message": f"'{product['reg_key']}' is already registered to another folder: {existing}",
                "nicnt": nicnt_path, "product": product, "already_registered": True}

    return {"ok": True, "message": "", "nicnt": nicnt_path, "product": product,
            "already_registered": bool(existing)}


def _set_values(hive, subkey, values):
    with winreg.CreateKeyEx(hive, subkey, 0, winreg.KEY_ALL_ACCESS | _WOW64) as k:
        for name, (kind, value) in values.items():
            winreg.SetValueEx(k, name, 0, kind, value)


def _extract_artwork(nicnt_path, files):
    """Copy the .nicnt's per-library artwork to NI Resources\\image\\<name>, as installed libraries have it."""
    written = 0
    for name, _, _ in files:
        parts = name.split("|")
        if len(parts) != 4 or parts[0] != ".PAResources" or parts[1] != "image":
            continue
        lib = parts[2].lower()
        file_name = parts[3].lower() if parts[3].endswith(".meta") else parts[3]
        target_dir = os.path.join(NI_RESOURCES_IMAGE_DIR, lib)
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, file_name), "wb") as f:
            f.write(extract(nicnt_path, name))
        written += 1
    return written


def register_library(folder):
    """
    Register the library in `folder` so Kontakt adds it on its next launch.
    Returns dict: success (bool), message (str), details (list of str), reg_key (str|None).
    """
    folder = os.path.normpath(folder)
    info = inspect_folder(folder)
    if not info["ok"]:
        return {"success": False, "message": info["message"], "details": [], "reg_key": None}
    if not is_admin():
        return {"success": False, "message": "KLib must run as administrator to add libraries.",
                "details": [], "reg_key": None}

    product, nicnt_path = info["product"], info["nicnt"]
    reg_key = product["reg_key"]
    details = []
    parsed = read_nicnt(nicnt_path)

    try:
        # ── HKLM product key: what Kontakt registers the library from ──
        values = {
            "ContentDir": (winreg.REG_SZ, folder),
            "ContentVersion": (winreg.REG_SZ, CONTENT_VERSION),
            "Visibility": (winreg.REG_DWORD, product["visibility"]),
        }
        if product["hu"]:
            values["HU"] = (winreg.REG_SZ, product["hu"])
        if product["jdx"]:
            values["JDX"] = (winreg.REG_SZ, product["jdx"])
        _set_values(winreg.HKEY_LOCAL_MACHINE, NI_KEY + "\\" + reg_key, values)
        details.append(f"✓ Registry key HKLM\\{NI_KEY}\\{reg_key}")
    except OSError as e:
        return {"success": False, "message": f"Could not write the registry key: {e}", "details": details,
                "reg_key": reg_key}

    # ── HKCU per-user key ──
    try:
        _set_values(winreg.HKEY_CURRENT_USER, NI_KEY + "\\" + reg_key, {
            "UserRemoved": (winreg.REG_DWORD, 0),
            "UserListIndex": (winreg.REG_DWORD, 0),
        })
        details.append(f"✓ Registry key HKCU\\{NI_KEY}\\{reg_key}")
    except OSError as e:
        details.append(f"✗ Could not write HKCU key: {e}")

    # ── Service Center product file (the .nicnt's ProductHints) + installed_products json ──
    for target, content in [
        (os.path.join(SERVICE_CENTER_DIR, reg_key + ".xml"), parsed["xml"]),
        (os.path.join(INSTALLED_PRODUCTS_DIR, reg_key + ".json"),
         json.dumps({"ContentDir": folder, "ContentVersion": CONTENT_VERSION}, separators=(",", ":")).encode()),
    ]:
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(content)
            details.append(f"✓ {target}")
        except OSError as e:
            details.append(f"✗ Could not write {target}: {e}")

    # ── Library artwork, if the .nicnt carries any ──
    try:
        n = _extract_artwork(nicnt_path, parsed["files"])
        if n:
            details.append(f"✓ Extracted {n} artwork file{'s' if n != 1 else ''} to NI Resources")
    except OSError as e:
        details.append(f"✗ Could not extract artwork: {e}")

    verb = "updated" if info["already_registered"] else "added"
    return {"success": True,
            "message": f"'{reg_key}' {verb}. Open Kontakt to finish — it indexes the library on launch.",
            "details": details, "reg_key": reg_key}
