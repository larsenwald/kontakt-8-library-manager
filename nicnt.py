"""
nicnt.py — reads Native Instruments .nicnt library containers

Layout (little-endian), as found in vendor and generated .nicnt files:
  0x000  MTD header (256 bytes, starts '/\\ NI FC MTD  /\\'), then the ProductHints XML, zero-padded
  ...    MTD header #2 — +0x110: file count, +0x118: total data size
  ...    TOC header; 0x260 after it, one 640-byte entry per file: +0x00 u64 data offset,
         +0x08 u64 index, +0x20 UTF-16 name ('|' as path separator)
  ...    TOC header #2; file data starts 0x260 after it and runs to the end of the file
Generated files may carry placeholder bytes in the count/size fields, so those are not trusted.
build_nicnt writes the vendor layout exactly (vendor files rebuild byte-for-byte from their parts).
"""

import glob
import os
import struct
import uuid
import xml.etree.ElementTree as ET
from xml.sax import saxutils

MTD_MAGIC = b"/\\ NI FC MTD  /\\"
TOC_MAGIC = b"/\\ NI FC TOC  /\\"
XML_OFFSET = 0x100
TOC_ENTRY_SIZE = 640
TOC_NAME_OFFSET = 0x20
SECTION_BODY = 0x260  # TOC header to its first entry, and TOC header #2 to the file data
VERSION_OFFSET = 0x42  # UTF-16 container version in MTD #1
SECTION_INDEX_OFFSET = 0x84
SECTION_SIZE_OFFSET = 0x90
METADATA_SIZE = 512000  # ProductHints payload size in vendor files
FILLER_F0 = 0xF0F0F0F0F0F0F0F0
FILLER_F1 = 0xF1F1F1F1F1F1F1F1


class NicntError(ValueError):
    pass


def _product_xml(d):
    end = d.find(b"\x00", XML_OFFSET)
    xml = d[XML_OFFSET:end if end >= 0 else len(d)].strip()
    if not xml.startswith(b"<?xml") or b"</ProductHints>" not in xml:
        raise NicntError("no ProductHints XML found")
    return xml


def _product_fields(xml):
    try:
        product = ET.fromstring(xml).find("Product")
    except ET.ParseError as e:
        raise NicntError(f"ProductHints XML is invalid: {e}")
    if product is None:
        raise NicntError("ProductHints has no <Product>")

    def text(tag):
        return (product.findtext(tag) or "").strip()

    specific = product.find("ProductSpecific")
    visibility = (specific.findtext("Visibility") or "").strip() if specific is not None else ""
    return {
        "name": text("Name"),
        "reg_key": text("RegKey") or text("Name"),
        "company": text("Company"),
        "snpid": text("SNPID"),
        "upid": text("UPID"),
        "hu": (specific.findtext("HU") or "").strip() if specific is not None else "",
        "jdx": (specific.findtext("JDX") or "").strip() if specific is not None else "",
        "visibility": int(visibility) if visibility.isdigit() else 3,
    }


def _files(d):
    """[(name, offset, size)] of the embedded files. Best effort: [] if the TOC is unreadable."""
    first = d.find(TOC_MAGIC)
    second = d.find(TOC_MAGIC, first + 1) if first >= 0 else -1
    if second < 0:
        return []
    data_start = second + SECTION_BODY
    entries = []
    for e in range(first + SECTION_BODY, second, TOC_ENTRY_SIZE):
        offset, index = struct.unpack_from("<QQ", d, e)
        raw = d[e + TOC_NAME_OFFSET:e + TOC_ENTRY_SIZE]
        name = raw.decode("utf-16le", "ignore").split("\x00", 1)[0]
        if not name or index != len(entries) + 1:
            break
        entries.append((name, data_start + offset))
    files = []
    for i, (name, start) in enumerate(entries):
        end = entries[i + 1][1] if i + 1 < len(entries) else len(d)
        if not (data_start <= start <= end <= len(d)):
            return []
        files.append((name, start, end - start))
    return files


def find_nicnt(folder):
    """Path of the folder's .nicnt, None if there isn't one. Raises if there are several."""
    matches = glob.glob(os.path.join(glob.escape(folder), "*.nicnt"))  # folder names often contain [brackets]
    if len(matches) > 1:
        raise NicntError(f"more than one .nicnt in {folder}: " + ", ".join(os.path.basename(m) for m in matches))
    return matches[0] if matches else None


def artwork_names(files):
    """Library names under .PAResources|image|<name>| — the NI Resources\\image folders this .nicnt fills."""
    return {parts[2] for parts in (n.split("|") for n, _, _ in files)
            if len(parts) == 4 and parts[0] == ".PAResources" and parts[1] == "image"}


def read_nicnt(path):
    """
    Returns { product: {...}, xml: bytes, files: [(name, offset, size)] }.
    Raises NicntError if the file isn't a readable .nicnt.
    """
    with open(path, "rb") as f:
        d = f.read()
    if not d.startswith(MTD_MAGIC):
        raise NicntError("not a .nicnt file (missing NI FC MTD header)")
    xml = _product_xml(d)
    return {"product": _product_fields(xml), "xml": xml, "files": _files(d)}


def read_version(path):
    """Container version string from the first MTD header (e.g. '1.0', '1.2.0')."""
    with open(path, "rb") as f:
        head = f.read(XML_OFFSET)
    return head[VERSION_OFFSET:VERSION_OFFSET + 0x40].decode("utf-16le", "ignore").split("\x00", 1)[0]


# ── Writing ──

def _section_header(magic, index=0, sizes=(), version=""):
    h = bytearray(XML_OFFSET)
    h[:len(magic)] = magic
    if version:
        v = version.encode("utf-16le")
        h[VERSION_OFFSET:VERSION_OFFSET + len(v)] = v
    if index:
        struct.pack_into("<I", h, SECTION_INDEX_OFFSET, index)
    for i, size in enumerate(sizes):
        struct.pack_into("<I", h, SECTION_SIZE_OFFSET + 4 * i, size)
    return bytes(h)


def build_nicnt(xml, files=(), version="1.0"):
    """
    Assemble a .nicnt from ProductHints XML (bytes) and [(toc_name, data)], in the vendor layout:
    MTD #1 + XML padded to METADATA_SIZE, MTD #2 + file count/size, TOC #1 + entries + trailer,
    TOC #2 + file data.
    """
    if len(xml) > METADATA_SIZE:
        raise NicntError("ProductHints XML is too large")
    files = list(files)
    total = sum(len(data) for _, data in files)

    out = bytearray()
    out += _section_header(MTD_MAGIC, 1, (METADATA_SIZE, METADATA_SIZE), version)
    out += xml + bytes(METADATA_SIZE - len(xml))

    out += _section_header(MTD_MAGIC, 2, (1,))
    out += struct.pack("<QQQQ", 1, FILLER_F0, len(files), total)

    out += TOC_MAGIC + bytes(SECTION_BODY - len(TOC_MAGIC))
    offset = 0
    for index, (name, data) in enumerate(files, 1):
        encoded = name.encode("utf-16le")
        if len(encoded) + 2 > TOC_ENTRY_SIZE - TOC_NAME_OFFSET:
            raise NicntError(f"file name too long: {name}")
        entry = bytearray(TOC_ENTRY_SIZE)
        struct.pack_into("<QQ", entry, 0, offset, index)
        entry[TOC_NAME_OFFSET:TOC_NAME_OFFSET + len(encoded)] = encoded
        out += entry
        offset += len(data)
    out += struct.pack("<QQQQ", total, FILLER_F1, 0, 0)

    out += TOC_MAGIC + bytes(SECTION_BODY - len(TOC_MAGIC))
    for _, data in files:
        out += data
    return bytes(out)


DB_CACHE = (b'<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'
            b'<soundinfos version="110">\n\n  <all/>\n\n</soundinfos>\n')
UPID_NAMESPACE = uuid.UUID("6b1d0c5e-3f0a-4f6e-9a39-2d4b5c6e7f80")  # KLib's own namespace


def product_xml(name, company, snpid, upid):
    """ProductHints for a library without vendor authorization (no HU/JDX, no Relevance)."""
    esc = lambda s: saxutils.escape(s or "")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'
        '<ProductHints spec="1.0.16">\n\n'
        '  <Product version="1">\n'
        f'    <UPID>{upid}</UPID>\n'
        f'    <Name>{esc(name)}</Name>\n'
        '    <Type>Content</Type>\n'
        '    <PoweredBy>Kontakt</PoweredBy>\n'
        '    <Visibility>1</Visibility>\n'
        f'    <Company>{esc(company)}</Company>\n'
        '    <AuthSystem>RAS3</AuthSystem>\n'
        f'    <SNPID>{esc(snpid)}</SNPID>\n'
        f'    <RegKey>{esc(name)}</RegKey>\n'
        '    <Icon>kontakt</Icon>\n'
        '    <ProductSpecific>\n'
        '      <Visibility type="Number">3</Visibility>\n'
        '    </ProductSpecific>\n'
        '  </Product>\n\n'
        '</ProductHints>\n'
    ).encode("utf-8")


def generate_nicnt(folder, name, company, snpid):
    """Write <folder>/<name>.nicnt for a library that came without one. Returns its path."""
    if find_nicnt(folder):
        raise NicntError("this folder already has a .nicnt")
    upid = str(uuid.uuid5(UPID_NAMESPACE, f"{snpid.upper()}|{name}"))  # stable across regenerations
    data = build_nicnt(product_xml(name, company, snpid.upper(), upid), [(".db.cache", DB_CACHE)])
    path = os.path.join(folder, name + ".nicnt")
    with open(path, "xb") as f:
        f.write(data)
    return path


def extract(path, name):
    """Bytes of one embedded file, by its TOC name."""
    with open(path, "rb") as f:
        d = f.read()
    for file_name, offset, size in _files(d):
        if file_name == name:
            return d[offset:offset + size]
    raise KeyError(name)
