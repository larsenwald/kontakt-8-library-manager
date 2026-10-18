"""
patches.py — reads and changes the library (bank) name stored in Kontakt patch files

Kontakt files each patch under the bank name in its own header, not under the .nicnt, so a
library whose patches say "Kontakt" lands in Kontakt's shared "Kontakt" folder.

Header layout, as found in .nki files from Kontakt 5–7:
  NI chunk tree: 'hsin' chunks and 'DSIN' items, each preceded by a u64 size counted from itself.
  The soundinfo item (DSIN id 108) holds an inner DSIN id 1; 32 bytes after its magic:
    5 strings (name, author, vendor, …) · 8 u32 · bank list · tag list · (Kontakt 6+: attributes)
  Strings are u32 length + UTF-16LE. Lists are u32 count + strings; attributes are key/value pairs.
Renaming rewrites the bank list's first entry and grows the enclosing size fields to match.
"""

import os
import shutil
import struct
import time

SOUNDINFO_ID = 108
PATCH_EXTS = (".nki", ".nkm", ".nksn")
EDITABLE_EXTS = (".nki",)  # verified in Kontakt; .nkm/.nksn are scanned but not edited yet
DEFAULT_BANK = "Kontakt"
MAX_BANK_LEVELS = 3
BACKUP_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", ""), "KLib", "patch-backups")


class PatchError(ValueError):
    pass


# ── Parsing ──

def _containers(d):
    """(base, size, kind) of every hsin chunk ('hsin') and DSIN item (its id)."""
    out = []
    for magic, back in ((b"hsin", 12), (b"DSIN", 8)):
        i = 0
        while (i := d.find(magic, i)) >= 0:
            base = i - back
            if base >= 0:
                size = struct.unpack_from("<Q", d, base)[0]
                if 0 < size and base + size <= len(d):
                    kind = struct.unpack_from("<I", d, i + 4)[0] if magic == b"DSIN" else "hsin"
                    out.append((base, size, kind))
            i += 4
    return sorted(out)


def _u32(d, o, end):
    if o + 4 > end:
        raise PatchError("soundinfo ends early")
    return struct.unpack_from("<I", d, o)[0], o + 4


def _string(d, o, end):
    n, o = _u32(d, o, end)
    if o + 2 * n > end:
        raise PatchError("string runs past soundinfo")
    try:
        s = d[o:o + 2 * n].decode("utf-16le")
    except UnicodeDecodeError:
        raise PatchError("string is not UTF-16")
    if not s.isprintable():
        raise PatchError("string is not printable")
    return s, o + 2 * n


def _list(d, o, end, limit):
    count, o = _u32(d, o, end)
    if count > limit:
        raise PatchError(f"list of {count} is implausible")
    items = []
    for _ in range(count):
        s, o = _string(d, o, end)
        items.append(s)
    return items, o


def _soundinfo(d):
    """Parse the soundinfo block → dict with bank list and the offsets needed to rewrite it."""
    blocks = [c for c in _containers(d) if c[2] == SOUNDINFO_ID]
    for base, size, _ in sorted(blocks, key=lambda c: -c[1]):
        end = base + size
        inner = d.find(b"DSIN\x01\x00\x00\x00", base, end)
        if inner < 0:
            continue
        try:
            o = inner + 32
            fields = []
            for _ in range(5):
                s, o = _string(d, o, end)
                fields.append(s)
            o += 8 * 4
            bank_at = o
            banks, o = _list(d, o, end, MAX_BANK_LEVELS)
            bank_end = o
            tags, o = _list(d, o, end, 256)  # attribute pairs follow in Kontakt 6+; not needed here
        except PatchError:
            continue
        return {"name": fields[0], "author": fields[1], "banks": banks, "tags": tags,
                "bank_at": bank_at, "bank_end": bank_end}
    raise PatchError("no readable soundinfo block")


def read_banks(path):
    """Bank list of a patch file, e.g. ['Kontakt'] or ['LASS Full 3 Library', 'Instruments', '- Splits']."""
    with open(path, "rb") as f:
        return _soundinfo(f.read())["banks"]


def rename_bank(d, new_name):
    """Return d with the top-level bank name replaced (sub-levels kept). Verifies the result."""
    if not new_name or not new_name.isprintable():
        raise PatchError("invalid library name")
    info = _soundinfo(d)
    banks = [new_name] + info["banks"][1:]
    encoded = struct.pack("<I", len(banks)) + b"".join(
        struct.pack("<I", len(b)) + b.encode("utf-16le") for b in banks)
    at, old_end = info["bank_at"], info["bank_end"]
    delta = len(encoded) - (old_end - at)
    before = _containers(d)
    out = bytearray(d[:at] + encoded + d[old_end:])
    for base, size, _ in before:
        if base <= at < base + size:
            struct.pack_into("<Q", out, base, size + delta)

    expected = sorted((base + (delta if base > at else 0),
                       size + (delta if base <= at < base + size else 0), kind)
                      for base, size, kind in before)
    if _containers(bytes(out)) != expected:
        raise PatchError("chunk layout changed unexpectedly")
    check = _soundinfo(bytes(out))
    if check["banks"] != banks or check["name"] != info["name"] or check["tags"] != info["tags"]:
        raise PatchError("rewritten soundinfo does not read back correctly")
    return bytes(out)


# ── Library-level ──

def scan_library(folder):
    """
    Every patch file under folder. Returns list of
      { path, rel, ext, banks (list|None), editable (bool), error (str|None) }
    """
    results = []
    for root, _, files in os.walk(folder):
        for fn in sorted(files):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in PATCH_EXTS:
                continue
            path = os.path.join(root, fn)
            entry = {"path": path, "rel": os.path.relpath(path, folder), "ext": ext,
                     "banks": None, "editable": ext in EDITABLE_EXTS, "error": None}
            try:
                entry["banks"] = read_banks(path)
            except (PatchError, OSError) as e:
                entry["error"] = str(e)
                entry["editable"] = False
            results.append(entry)
    return results


def summarize(scan):
    """{ top-level bank name: count } over readable patches, plus counts of skipped files."""
    names = {}
    for e in scan:
        if e["banks"] is not None:
            top = e["banks"][0] if e["banks"] else ""
            names[top] = names.get(top, 0) + 1
    return {
        "names": names,
        "total": len(scan),
        "unreadable": sum(1 for e in scan if e["error"]),
        "not_editable": sum(1 for e in scan if not e["error"] and not e["editable"]),
    }


def set_library_name(folder, new_name, label=None):
    """
    Rename the top-level bank of every editable patch under folder that doesn't already carry
    new_name. Originals are copied to BACKUP_ROOT/<label>/<timestamp>/ first.
    Returns { changed: [rel], skipped: [rel], failed: [(rel, error)], backup_dir }.
    """
    scan = scan_library(folder)
    todo = [e for e in scan if e["editable"] and e["banks"] and e["banks"][0] != new_name]
    result = {"changed": [], "skipped": [e["rel"] for e in scan if not e["editable"]],
              "failed": [], "backup_dir": None}
    if not todo:
        return result

    backup_dir = os.path.join(BACKUP_ROOT, label or os.path.basename(folder), time.strftime("%Y%m%d-%H%M%S"))
    result["backup_dir"] = backup_dir
    for e in todo:
        try:
            with open(e["path"], "rb") as f:
                data = f.read()
            new_data = rename_bank(data, new_name)
            backup = os.path.join(backup_dir, e["rel"])
            os.makedirs(os.path.dirname(backup), exist_ok=True)
            shutil.copy2(e["path"], backup)
            tmp = e["path"] + ".klib-tmp"
            with open(tmp, "wb") as f:
                f.write(new_data)
            os.replace(tmp, e["path"])
            result["changed"].append(e["rel"])
        except (PatchError, OSError) as err:
            result["failed"].append((e["rel"], str(err)))
    return result
