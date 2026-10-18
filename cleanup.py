"""
cleanup.py — removes a library's system-wide registration (registry, Service Center, NI json)

Kontakt re-registers any library that still has an HKLM\\SOFTWARE\\Native Instruments\\<RegKey>
entry on startup, so these must go before the database rows do. Kontakt / Native Access
recreate all of it when the library is added again.
"""

import ctypes
import glob
import json
import os
import shutil
import winreg
import xml.etree.ElementTree as ET

from nicnt import NicntError, artwork_names, find_nicnt, read_nicnt


NI_KEY = r"SOFTWARE\Native Instruments"
KONTAKT_KEY = NI_KEY + r"\Kontakt 8"
SERVICE_CENTER_DIR = os.path.join(
    os.environ.get("COMMONPROGRAMFILES", r"C:\Program Files\Common Files"),
    "Native Instruments", "Service Center",
)
INSTALLED_PRODUCTS_DIR = os.path.join(
    os.environ.get("PUBLIC", r"C:\Users\Public"),
    "Documents", "Native Instruments", "installed_products",
)
NI_RESOURCES_IMAGE_DIR = os.path.join(
    os.environ.get("PUBLIC", r"C:\Users\Public"), "Documents", "NI Resources", "image",
)

_WOW64 = winreg.KEY_WOW64_64KEY


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _norm(path):
    return os.path.normcase(os.path.normpath(path)) if path else ""


# ── Discovery ──

def _service_center_matches(upid, product_id):
    """Yield (xml_path, reg_key) for Service Center product files matching this library."""
    for xml_path in glob.glob(os.path.join(SERVICE_CENTER_DIR, "*.xml")):
        try:
            product = ET.parse(xml_path).getroot().find("Product")
        except (ET.ParseError, OSError):
            continue
        if product is None:
            continue
        xml_upid = (product.findtext("UPID") or "").strip().lower()
        xml_snpid = (product.findtext("SNPID") or "").strip().lower()
        if (upid and xml_upid == upid.lower()) or (product_id and xml_snpid == product_id.lower()):
            reg_key = (product.findtext("RegKey") or product.findtext("Name") or "").strip()
            yield xml_path, reg_key


def _hklm_keys_for_path(lib_path):
    """Names of HKLM NI product keys whose ContentDir points at lib_path."""
    names = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, NI_KEY, 0, winreg.KEY_READ | _WOW64)
    except OSError:
        return names
    with root:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(root, name, 0, winreg.KEY_READ | _WOW64) as k:
                    content_dir = winreg.QueryValueEx(k, "ContentDir")[0]
            except OSError:
                continue
            if _norm(content_dir) == _norm(lib_path):
                names.append(name)
    return names


def _key_exists(hive, subkey):
    try:
        winreg.CloseKey(winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | _WOW64))
        return True
    except OSError:
        return False


def _artwork_dirs(lib_path, reg_keys, extra_names, protected_names):
    """NI Resources\\image\\<name> folders belonging to this library (never one another library uses)."""
    names = set(reg_keys) | set(extra_names)
    try:
        nicnt_path = find_nicnt(lib_path)
        if nicnt_path:
            names |= artwork_names(read_nicnt(nicnt_path)["files"])
    except (NicntError, OSError):
        pass
    protected = {n.lower() for n in protected_names}
    dirs = []
    for name in sorted({n.lower() for n in names if n} - protected - {"kontakt"}):
        path = os.path.join(NI_RESOURCES_IMAGE_DIR, name)
        if os.path.isdir(path):
            dirs.append(path)
    return dirs


def find_remnants(lib_path, product_id, upid, extra_names=(), protected_names=()):
    """
    Locate everything registering this library outside the Kontakt db. Returns:
      { reg_keys: [str], xml_files: [str], json_files: [str], image_dirs: [str] }
    extra_names:     other names the library goes by (db alias, its patches' bank names)
    protected_names: names other installed libraries use — their artwork folders are left alone
    """
    reg_keys, xml_files = set(), []
    for xml_path, reg_key in _service_center_matches(upid, product_id):
        xml_files.append(xml_path)
        if reg_key:
            reg_keys.add(reg_key)
    reg_keys.update(_hklm_keys_for_path(lib_path))

    json_files = []
    for json_path in glob.glob(os.path.join(INSTALLED_PRODUCTS_DIR, "*.json")):
        name = os.path.splitext(os.path.basename(json_path))[0]
        try:
            with open(json_path, encoding="utf-8") as f:
                content_dir = json.load(f).get("ContentDir", "")
        except (OSError, ValueError, AttributeError):
            content_dir = ""
        if name in reg_keys or (content_dir and _norm(content_dir) == _norm(lib_path)):
            json_files.append(json_path)

    image_dirs = _artwork_dirs(lib_path, reg_keys, extra_names, protected_names)
    return {"reg_keys": sorted(reg_keys), "xml_files": xml_files, "json_files": json_files,
            "image_dirs": image_dirs}


# ── Removal ──

def _delete_tree(hive, subkey):
    """Recursively delete a registry key (winreg.DeleteKey only removes leaf keys)."""
    with winreg.OpenKey(hive, subkey, 0, winreg.KEY_ALL_ACCESS | _WOW64) as k:
        while True:
            try:
                child = winreg.EnumKey(k, 0)
            except OSError:
                break
            _delete_tree(hive, subkey + "\\" + child)
    winreg.DeleteKeyEx(hive, subkey, _WOW64, 0)


def _encode_kontakt_path(path):
    """
    Kontakt stores recent paths as '@v001' + drive letter + 'dNNN<dir>' per folder
    (NNN = name length). Returns that prefix for a folder, lowercased for comparison.
    """
    drive, rest = os.path.splitdrive(os.path.normpath(path))
    encoded = "@v001" + drive.rstrip(":")
    for part in [p for p in rest.split(os.sep) if p]:
        encoded += f"d{len(part):03d}{part}"
    return encoded.lower()


def _points_into(encoded_value, prefix):
    v = encoded_value.lower()
    return v == prefix or (v.startswith(prefix) and v[len(prefix)] in "df")


def _clean_recent_files(lib_path):
    """Drop Kontakt's recent-file entries (and last missing-samples folder) inside lib_path."""
    prefix = _encode_kontakt_path(lib_path)
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, KONTAKT_KEY, 0, winreg.KEY_ALL_ACCESS)
    except OSError:
        return 0
    with k:
        def get(name, default=None):
            try:
                return winreg.QueryValueEx(k, name)[0]
            except OSError:
                return default

        count = get("recentFileCount", 0)
        entries = [(get(f"recentFileNameNr{i}", ""), get(f"recentLuaScriptNr{i}", "@")) for i in range(count)]
        kept = [e for e in entries if not _points_into(e[0], prefix)]
        removed = len(entries) - len(kept)
        if removed:
            for i, (file_name, lua) in enumerate(kept):
                winreg.SetValueEx(k, f"recentFileNameNr{i}", 0, winreg.REG_SZ, file_name)
                winreg.SetValueEx(k, f"recentLuaScriptNr{i}", 0, winreg.REG_SZ, lua)
            for i in range(len(kept), count):
                for name in (f"recentFileNameNr{i}", f"recentLuaScriptNr{i}"):
                    try:
                        winreg.DeleteValue(k, name)
                    except OSError:
                        pass
            winreg.SetValueEx(k, "recentFileCount", 0, winreg.REG_DWORD, len(kept))

        missing = get("lastMissingSamplesFolder")
        if missing and _points_into(missing, prefix):
            winreg.DeleteValue(k, "lastMissingSamplesFolder")
    return removed


def remove_remnants(lib_path, product_id, upid, extra_names=(), protected_names=()):
    """
    Remove the library's system registration. Returns (ok, details).
    ok is False if the HKLM key could not be removed — the caller must then leave the db
    alone, or Kontakt will simply re-register the library on its next launch.
    """
    details = []
    remnants = find_remnants(lib_path, product_id, upid, extra_names, protected_names)

    # ── HKLM product key: the one Kontakt re-registers from ──
    for name in remnants["reg_keys"]:
        subkey = NI_KEY + "\\" + name
        if not _key_exists(winreg.HKEY_LOCAL_MACHINE, subkey):
            continue
        try:
            _delete_tree(winreg.HKEY_LOCAL_MACHINE, subkey)
            details.append(f"✓ Removed registry key HKLM\\{subkey}")
        except PermissionError:
            details.append(f"✗ Could not remove HKLM\\{subkey} — KLib must run as administrator")
            return False, details
        except OSError as e:
            details.append(f"✗ Could not remove HKLM\\{subkey}: {e}")
            return False, details

    # ── HKCU per-user product key ──
    for name in remnants["reg_keys"]:
        subkey = NI_KEY + "\\" + name
        if not _key_exists(winreg.HKEY_CURRENT_USER, subkey):
            continue
        try:
            _delete_tree(winreg.HKEY_CURRENT_USER, subkey)
            details.append(f"✓ Removed registry key HKCU\\{subkey}")
        except OSError as e:
            details.append(f"✗ Could not remove HKCU\\{subkey}: {e}")

    # ── Service Center product file + installed_products json ──
    for f in remnants["xml_files"] + remnants["json_files"]:
        try:
            os.remove(f)
            details.append(f"✓ Deleted {f}")
        except OSError as e:
            details.append(f"✗ Could not delete {f}: {e}")

    # ── Library artwork + .meta in NI Resources ──
    for d in remnants["image_dirs"]:
        try:
            shutil.rmtree(d)
            details.append(f"✓ Deleted artwork folder {d}")
        except OSError as e:
            details.append(f"✗ Could not delete {d}: {e}")

    # ── Kontakt recent-files list ──
    try:
        n = _clean_recent_files(lib_path)
        if n:
            details.append(f"✓ Removed {n} recent-file entr{'y' if n == 1 else 'ies'} from Kontakt")
    except OSError as e:
        details.append(f"✗ Could not clean Kontakt recent files: {e}")

    return True, details
