"""
nicnt.py — reads Native Instruments .nicnt library containers

Layout (little-endian), as found in vendor and generated .nicnt files:
  0x000  MTD header (256 bytes, starts '/\\ NI FC MTD  /\\'), then the ProductHints XML, zero-padded
  ...    MTD header #2 — +0x110: file count, +0x118: total data size
  ...    TOC header; 0x260 after it, one 640-byte entry per file: +0x00 u64 data offset,
         +0x08 u64 index, +0x20 UTF-16 name ('|' as path separator)
  ...    TOC header #2; file data starts 0x260 after it and runs to the end of the file
Generated files may carry placeholder bytes in the count/size fields, so those are not trusted.
"""

import struct
import xml.etree.ElementTree as ET

MTD_MAGIC = b"/\\ NI FC MTD  /\\"
TOC_MAGIC = b"/\\ NI FC TOC  /\\"
XML_OFFSET = 0x100
TOC_ENTRY_SIZE = 640
TOC_NAME_OFFSET = 0x20
SECTION_BODY = 0x260  # TOC header to its first entry, and TOC header #2 to the file data


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


def extract(path, name):
    """Bytes of one embedded file, by its TOC name."""
    with open(path, "rb") as f:
        d = f.read()
    for file_name, offset, size in _files(d):
        if file_name == name:
            return d[offset:offset + size]
    raise KeyError(name)
