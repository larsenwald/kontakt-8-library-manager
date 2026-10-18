"""
artwork.py — library pictures, header colour and banner text in Kontakt 8's browser

Kontakt looks everything up by the library's browser folder name (its patches' bank name),
case-insensitively:
  NI Resources\\image\\<name>\\
    NKS2_software_tile.webp  420x316  enlarged-view tile (only the middle ~420x201 shows);
                                      without it Kontakt falls back to MST_artwork.png (any size)
    NKS2_hardware_banner.webp 1280x212 condensed-view banner
    VB_logo.png               227x47   library header logo, transparent
  NI Resources\\_DatabaseResources\\color\\color.db          VB_bgcolor: header colour (behind a logo only)
  NI Resources\\_DatabaseResources\\shortname\\shortname.db  VB_shortname: banner text when there's no banner
The two databases are shared snapshots that vendor installs can replace wholesale, so KLib keeps
its own record of colour / text changes and re-applies them (reapply_overrides).
"""

import base64
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time

from PIL import Image

from cleanup import NI_RESOURCES_IMAGE_DIR

DB_RESOURCES_DIR = os.path.join(os.path.dirname(NI_RESOURCES_IMAGE_DIR), "_DatabaseResources")
KLIB_DIR = os.path.join(os.environ.get("LOCALAPPDATA", ""), "KLib")
OVERRIDES_FILE = os.path.join(KLIB_DIR, "artwork-overrides.json")
BACKUP_ROOT = os.path.join(KLIB_DIR, "artwork-backups")
STAGING_DIR = os.path.join(tempfile.gettempdir(), "klib-artwork")

SLOTS = {
    "tile": {"file": "NKS2_software_tile.webp", "size": (420, 316), "fit": "cover",
             "fallbacks": ("MST_artwork.png",)},
    "banner": {"file": "NKS2_hardware_banner.webp", "size": (1280, 212), "fit": "cover", "fallbacks": ()},
    "logo": {"file": "VB_logo.png", "size": (227, 47), "fit": "contain", "fallbacks": ()},
}
TILE_VISIBLE = (420, 201)  # the band of the tile Kontakt's enlarged view actually shows
COLOR_KEYS = ("VB_bgcolor", "MST_bgcolor")
SHORTNAME_KEYS = ("VB_shortname", "MST_shortname", "MKII_shortname", "MIKRO_shortname")


class ArtworkError(ValueError):
    pass


# ── Locations ──

def image_folder(name):
    """The library's NI Resources\\image folder (existing one if any, matched case-insensitively)."""
    if os.path.isdir(NI_RESOURCES_IMAGE_DIR):
        for entry in os.listdir(NI_RESOURCES_IMAGE_DIR):
            if entry.lower() == name.lower():
                return os.path.join(NI_RESOURCES_IMAGE_DIR, entry)
    return os.path.join(NI_RESOURCES_IMAGE_DIR, name.lower())


def _db(kind):
    return os.path.join(DB_RESOURCES_DIR, kind, kind + ".db")


# ── Images ──

def _fit(img, size, mode):
    """cover: fill size and centre-crop · contain: fit inside size on a transparent canvas."""
    w, h = size
    if mode == "cover":
        scale = max(w / img.width, h / img.height)
        resized = img.resize((max(w, round(img.width * scale)), max(h, round(img.height * scale))), Image.LANCZOS)
        left, top = (resized.width - w) // 2, (resized.height - h) // 2
        return resized.crop((left, top, left + w, top + h))
    img = img.convert("RGBA")
    scale = min(w / img.width, h / img.height)
    resized = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    canvas.alpha_composite(resized, ((w - resized.width) // 2, (h - resized.height) // 2))
    return canvas


def _data_url(img, max_width):
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _preview(slot, img):
    """How the slot looks in Kontakt: the tile's visible band, the whole banner, the logo as-is."""
    if slot == "tile":
        img = _fit(img.convert("RGB"), TILE_VISIBLE, "cover")
        return _data_url(img, 420)
    if slot == "banner":
        return _data_url(img.convert("RGB"), 640)
    return _data_url(img.convert("RGBA"), 454)


SOURCE_PREVIEW_MAX = 1600  # longest side of the picture the crop editor works on


def _open_source(source_path):
    try:
        src = Image.open(source_path)
        src.load()
    except (OSError, ValueError) as e:
        raise ArtworkError(f"could not read the image: {e}")
    return src.convert("RGBA")


def source_info(source_path):
    """A picked image's size plus a lightweight preview for the crop editor."""
    src = _open_source(source_path)
    preview = src
    if max(src.size) > SOURCE_PREVIEW_MAX:
        scale = SOURCE_PREVIEW_MAX / max(src.size)
        preview = src.resize((round(src.width * scale), round(src.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    preview.save(buf, "WEBP", quality=85)
    return {"width": src.width, "height": src.height, "edge_color": _edge_color(src),
            "preview": "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode()}


def _crop_to(src, crop, size):
    """
    Render crop = {x, y, w, h} (source pixels, may extend past the image) onto a canvas of `size`.
    Only the part that overlaps the image is resampled, from full resolution; the rest is transparent.
    """
    w, h = size
    cx, cy, cw, ch = (float(crop[k]) for k in ("x", "y", "w", "h"))
    if cw <= 0 or ch <= 0:
        raise ArtworkError("crop area is empty")
    sx, sy = w / cw, h / ch
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    left, top = max(0.0, cx), max(0.0, cy)
    right, bottom = min(float(src.width), cx + cw), min(float(src.height), cy + ch)
    if right <= left or bottom <= top:
        return canvas
    dest = (round((left - cx) * sx), round((top - cy) * sy), round((right - cx) * sx), round((bottom - cy) * sy))
    if dest[2] <= dest[0] or dest[3] <= dest[1]:
        return canvas
    part = src.resize((dest[2] - dest[0], dest[3] - dest[1]), Image.LANCZOS, box=(left, top, right, bottom))
    canvas.alpha_composite(part, (dest[0], dest[1]))
    return canvas


def _edge_color(img):
    """Average colour of the image's outer border — a background that usually blends in."""
    rgb = img.convert("RGB")
    small = rgb.resize((max(1, min(64, rgb.width)), max(1, min(64, rgb.height))), Image.BILINEAR)
    w, h = small.size
    border = [small.getpixel((x, y)) for x in range(w) for y in (0, h - 1)] + \
             [small.getpixel((x, y)) for y in range(h) for x in (0, w - 1)]
    return "#" + "".join(f"{round(sum(p[i] for p in border) / len(border)):02x}" for i in range(3))


def _parse_color(value):
    value = (value or "").strip().lstrip("#").lower()
    if len(value) != 6 or any(ch not in "0123456789abcdef" for ch in value):
        raise ArtworkError("background must be a hex colour like #1a1a1a")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def prepare_image(slot, source_path, crop=None, background=None):
    """
    Resize / crop a picked image for a slot into a staging file. crop = {x, y, w, h} in source pixels,
    and may reach past the image. Tile and banner are solid images: anything the image doesn't cover
    (including the tile's unseen top/bottom strips) is filled with `background` (default: the image's
    edge colour). The logo keeps those areas transparent so the header colour shows through.
    Without a crop, tile/banner are centre-cropped to fill and the logo is fitted whole.
    Returns {path, preview, note}.
    """
    spec = SLOTS[slot]
    size = spec["size"]
    src = _open_source(source_path)
    if crop:
        fitted = _crop_to(src, crop, size)
        source_pixels = float(crop["w"])
    else:
        fitted = _fit(src, size, spec["fit"])
        source_pixels = min(src.width, src.height * size[0] / size[1]) if spec["fit"] == "cover" else size[0]
    if spec["fit"] == "cover":
        fill = _parse_color(background or _edge_color(src))
        solid = Image.new("RGBA", size, fill + (255,))
        solid.alpha_composite(fitted.convert("RGBA"))
        fitted = solid.convert("RGB")
    os.makedirs(STAGING_DIR, exist_ok=True)
    out = os.path.join(STAGING_DIR, f"{slot}-{time.time_ns()}{os.path.splitext(spec['file'])[1]}")
    if out.endswith(".webp"):
        fitted.save(out, "WEBP", quality=95, method=6)
    else:
        fitted.save(out, "PNG")
    note = ""
    if spec["fit"] == "cover" and source_pixels < size[0]:
        note = f"This crop is enlarged {size[0] / source_pixels:.1f}x, so it may look soft in Kontakt."
    return {"path": out, "preview": _preview(slot, fitted), "note": note}


# ── Colour + banner text ──

def _read_value(kind, location, key):
    path = _db(kind)
    if not os.path.isfile(path):
        return None
    with sqlite3.connect(path) as c:
        row = c.execute(f"SELECT Value FROM {kind} WHERE Location = ? AND Name = ? ORDER BY seqidx LIMIT 1",
                        (location, key)).fetchone()
    return row[0] if row else None


def _write_values(kind, location, keys, value):
    """Set (or clear, if value is None) the given keys for location in a shared resource db."""
    path = _db(kind)
    if not os.path.isfile(path):
        if value is None:
            return
        raise ArtworkError(f"{kind}.db not found in NI Resources")
    with sqlite3.connect(path) as c:
        for key in keys:
            c.execute(f"DELETE FROM {kind} WHERE Location = ? AND Name = ?", (location, key))
            if value is not None:
                c.execute(f"INSERT INTO {kind}(Location, Name, Value, seqidx) VALUES(?, ?, ?, 0)",
                          (location, key, value))


def _load_overrides():
    try:
        with open(OVERRIDES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_overrides(data):
    os.makedirs(KLIB_DIR, exist_ok=True)
    tmp = OVERRIDES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, OVERRIDES_FILE)


def _remember(name, field, value):
    data = _load_overrides()
    entry = data.setdefault(name.lower(), {"name": name})
    entry["name"] = name
    if value is None:
        entry.pop(field, None)
    else:
        entry[field] = value
    if set(entry) == {"name"}:
        data.pop(name.lower())
    _save_overrides(data)


def reapply_overrides():
    """Put KLib's colour / banner-text changes back if a vendor install replaced the shared dbs."""
    fixed = 0
    for entry in _load_overrides().values():
        name = entry["name"]
        if "color" in entry and _read_value("color", name, COLOR_KEYS[0]) != entry["color"]:
            _write_values("color", name, COLOR_KEYS, entry["color"])
            fixed += 1
        if "shortname" in entry and _read_value("shortname", name, SHORTNAME_KEYS[0]) != entry["shortname"]:
            _write_values("shortname", name, SHORTNAME_KEYS, entry["shortname"])
            fixed += 1
    return fixed


# ── Library-level ──

def read_artwork(name):
    """Current artwork for a browser folder name, with previews of how Kontakt shows it."""
    folder = image_folder(name)
    result = {"name": name, "folder": folder}
    for slot, spec in SLOTS.items():
        found = None
        for file_name in (spec["file"],) + spec["fallbacks"]:
            path = os.path.join(folder, file_name)
            if os.path.isfile(path):
                found = path
                break
        entry = {"file": os.path.basename(found) if found else None, "preview": None}
        if found:
            try:
                with Image.open(found) as img:
                    img.load()
                    entry["preview"] = _preview(slot, img)
            except OSError:
                pass
        result[slot] = entry
    color = _read_value("color", name, COLOR_KEYS[0])
    result["color"] = ("#" + color.lstrip("#")) if color else None
    result["shortname"] = _read_value("shortname", name, SHORTNAME_KEYS[0])
    return result


def apply_artwork(name, changes):
    """
    changes: { tile|banner|logo: staged path to set, or "" to remove (key absent = keep),
               color: "#rrggbb" or "" to clear, shortname: text or "" to clear }
    Replaced files are backed up first. Returns a list of detail strings.
    """
    details = []
    folder = image_folder(name)
    backup_dir = os.path.join(BACKUP_ROOT, name.lower(), time.strftime("%Y%m%d-%H%M%S"))

    def backup(path):
        os.makedirs(backup_dir, exist_ok=True)
        shutil.copy2(path, os.path.join(backup_dir, os.path.basename(path)))

    for slot, spec in SLOTS.items():
        if slot not in changes:
            continue
        staged = changes[slot]
        # Removing a tile also removes its fallback, or Kontakt would keep showing MST_artwork
        targets = [spec["file"]] + (list(spec["fallbacks"]) if not staged else [])
        for file_name in targets:
            path = os.path.join(folder, file_name)
            if os.path.isfile(path):
                backup(path)
                os.remove(path)
        if staged:
            if not os.path.isfile(staged) or not os.path.normpath(staged).startswith(os.path.normpath(STAGING_DIR)):
                raise ArtworkError(f"prepared {slot} image is missing — pick it again")
            os.makedirs(folder, exist_ok=True)
            shutil.copyfile(staged, os.path.join(folder, spec["file"]))
            details.append(f"✓ {slot.capitalize()} image set ({spec['file']})")
        else:
            details.append(f"✓ {slot.capitalize()} image removed")
    if os.path.isdir(folder) and not os.listdir(folder):
        os.rmdir(folder)

    if "color" in changes:
        color = (changes["color"] or "").strip().lstrip("#").lower() or None
        if color and (len(color) != 6 or any(ch not in "0123456789abcdef" for ch in color)):
            raise ArtworkError("header colour must be a hex colour like #e8199b")
        _write_values("color", name, COLOR_KEYS, color)
        _remember(name, "color", color)
        details.append(f"✓ Header colour {'set to #' + color if color else 'cleared'}")
    if "shortname" in changes:
        text = (changes["shortname"] or "").strip() or None
        _write_values("shortname", name, SHORTNAME_KEYS, text)
        _remember(name, "shortname", text)
        details.append(f"✓ Banner text {'set to ' + repr(text) if text else 'cleared'}")
    return details


def rename_artwork(old, new):
    """Follow a library rename: move its image folder and colour / banner-text rows to the new name."""
    if not old or not new or old.lower() == new.lower():
        return []
    details = []
    src, dst = image_folder(old), image_folder(new)
    if os.path.isdir(src) and not os.path.exists(dst):
        shutil.move(src, os.path.join(NI_RESOURCES_IMAGE_DIR, new.lower()))
        details.append(f"✓ Moved artwork to '{new}'")
    for kind in ("color", "shortname"):
        path = _db(kind)
        if os.path.isfile(path):
            with sqlite3.connect(path) as c:
                if not c.execute(f"SELECT 1 FROM {kind} WHERE Location = ?", (new,)).fetchone():
                    n = c.execute(f"UPDATE {kind} SET Location = ? WHERE Location = ?", (new, old)).rowcount
                    if n:
                        details.append(f"✓ Moved {kind} settings to '{new}'")
    data = _load_overrides()
    if old.lower() in data and new.lower() not in data:
        entry = data.pop(old.lower())
        entry["name"] = new
        data[new.lower()] = entry
        _save_overrides(data)
    return details


def forget(names):
    """On library delete: drop KLib's own colour / banner-text changes for these names (vendor rows stay)."""
    data = _load_overrides()
    for name in names:
        entry = data.pop((name or "").lower(), None)
        if not entry:
            continue
        if "color" in entry:
            _write_values("color", entry["name"], COLOR_KEYS, None)
        if "shortname" in entry:
            _write_values("shortname", entry["name"], SHORTNAME_KEYS, None)
    _save_overrides(data)
