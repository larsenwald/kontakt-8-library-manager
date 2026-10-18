"""
register.py — adds a library to Kontakt 8 from its .nicnt (the inverse of cleanup.py)

Writes the library's system registration; Kontakt picks the library up and indexes it the next
time it starts (it re-registers anything with an HKLM\\SOFTWARE\\Native Instruments\\<RegKey> key).
"""

import glob
import json
import os
import random
import string
import winreg
import xml.etree.ElementTree as ET

from cleanup import (NI_KEY, SERVICE_CENTER_DIR, INSTALLED_PRODUCTS_DIR, NI_RESOURCES_IMAGE_DIR,
                     _WOW64, _norm, is_admin)
from nicnt import NicntError, extract, find_nicnt, read_nicnt

CONTENT_VERSION = "1.0.0"


# ── New .nicnt: SNPID + name checks ──

NATIVE_ACCESS_CATALOG = os.path.join(SERVICE_CENTER_DIR, "NativeAccess.xml")
SNPID_CHARS = string.digits + string.ascii_uppercase
INVALID_NAME_CHARS = set('\\/:*?"<>|')


def _known_products():
    """(snpids, names) — upper-cased SNPIDs and lower-cased RegKeys/Names of NI's catalog and installed products."""
    snpids, names = set(), set()
    files = glob.glob(os.path.join(SERVICE_CENTER_DIR, "*.xml"))
    for path in files:
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        for product in root.iter("Product"):
            for el in product.findall("SNPID"):
                if el.text and el.text.strip():
                    snpids.add(el.text.strip().upper())
            for tag in ("RegKey", "Name"):
                text = (product.findtext(tag) or "").strip()
                if text:
                    names.add(text.lower())
    return snpids, names


def check_new_library(name, snpid, db_product_ids=()):
    """Problems with generating a .nicnt for (name, snpid); [] if it's good to go."""
    problems = []
    name, snpid = (name or "").strip(), (snpid or "").strip().upper()
    snpids, names = _known_products()
    snpids |= {p.upper() for p in db_product_ids if p}

    if not name:
        problems.append("Library name is required.")
    elif INVALID_NAME_CHARS & set(name) or name.endswith((".", " ")):
        problems.append('Library name can\'t contain \\ / : * ? " < > | or end with a dot or space.')
    elif len(name) > 100:
        problems.append("Library name is too long.")
    elif name.lower() in names:
        problems.append(f"'{name}' is already the name of a Native Instruments product or installed library.")

    if len(snpid) != 3 or any(c not in SNPID_CHARS for c in snpid):
        problems.append("SNPID must be exactly 3 letters or digits.")
    elif snpid in snpids:
        problems.append(f"SNPID {snpid} is already used by another product.")
    return problems


def suggest_snpid(db_product_ids=()):
    """A random SNPID no known product uses (always contains a letter, like most custom ones)."""
    used = _known_products()[0] | {p.upper() for p in db_product_ids if p}
    rng = random.SystemRandom()
    while True:
        candidate = "".join(rng.choice(SNPID_CHARS) for _ in range(3))
        if candidate not in used and not candidate.isdigit():
            return candidate


PLACEHOLDER_COMPANY = "custom ["  # what third-party .nicnt generators put in <Company>


def _product_entry(product):
    text = lambda tag: (product.findtext(tag) or "").strip()
    company = text("Company")
    return {"upid": text("UPID").lower(), "snpid": text("SNPID").upper(),
            "company": "" if company.lower().startswith(PLACEHOLDER_COMPANY) else company,
            "name": text("Name"), "reg_key": text("RegKey") or text("Name")}


_catalog_cache = {"mtime": None, "products": []}


def _catalog_products():
    """Native Access's catalogue (~3 MB), parsed once and reused until the file changes."""
    try:
        mtime = os.path.getmtime(NATIVE_ACCESS_CATALOG)
    except OSError:
        return []
    if _catalog_cache["mtime"] != mtime:
        try:
            root = ET.parse(NATIVE_ACCESS_CATALOG).getroot()
        except (ET.ParseError, OSError):
            return []
        _catalog_cache.update(mtime=mtime, products=[_product_entry(p) for p in root.iter("Product")])
    return _catalog_cache["products"]


def installed_products():
    """
    [{upid, snpid, company, name, reg_key}]: installed products' Service Center files first, then NI's
    catalogue (NI products like The Grandeur have no Service Center file of their own).
    """
    products = []
    for path in glob.glob(os.path.join(SERVICE_CENTER_DIR, "*.xml")):
        if os.path.normcase(path) == os.path.normcase(NATIVE_ACCESS_CATALOG):
            continue
        try:
            product = ET.parse(path).getroot().find("Product")
        except (ET.ParseError, OSError):
            continue
        if product is not None:
            products.append(_product_entry(product))
    return products + _catalog_products()


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
            return {"ok": False, "needs_nicnt": True, "message": "This folder has no .nicnt file.",
                    "nicnt": None, "product": None, "already_registered": False}
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
