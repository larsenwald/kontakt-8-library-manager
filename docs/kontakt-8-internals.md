# Kontakt 8 internals — a reverse-engineered reference

How Native Instruments Kontakt 8 (Windows) registers, indexes, names and decorates sample
libraries, worked out while building KLib. Everything here was determined by observing Kontakt's
own files, registry entries, database and behaviour on a live install, and by testing changes
against Kontakt. It is unofficial; Native Instruments publishes none of it.

Facts are **verified** (tested against Kontakt, or byte-for-byte checks across many files) unless
marked *(inferred)* or listed under [Open questions](#open-questions).

Contents

1. [Where things live](#1-where-things-live)
2. [Library lifecycle: how Kontakt adds, re-adds and hides libraries](#2-library-lifecycle)
3. [komplete.db3 — the library database](#3-kompletedb3--the-library-database)
4. [The .nicnt container format](#4-the-nicnt-container-format)
5. [Patch headers (.nki / .nkm / .nksn) and the browser folder](#5-patch-headers-and-the-browser-folder)
6. [Browser artwork, header colour and banner text](#6-browser-artwork-header-colour-and-banner-text)
7. [SNPIDs and product identity](#7-snpids-and-product-identity)
8. [Removing a library completely](#8-removing-a-library-completely)
9. [Detecting that Kontakt is running](#9-detecting-that-kontakt-is-running)
10. [Open questions](#open-questions)

---

## 1. Where things live

| Location | What it is |
|---|---|
| `%LOCALAPPDATA%\Native Instruments\Kontakt 8\komplete.db3` | SQLite library database (WAL journal mode). See [§3](#3-kompletedb3--the-library-database). |
| `%LOCALAPPDATA%\Native Instruments\Kontakt 8\LibrariesCache\K{SNPID}{unix-seconds}.cache` | Per-library cache, e.g. `K4871790819497.cache` = SNPID `487`, created at Unix time `1790819497`. Not every library has one. |
| `%LOCALAPPDATA%\Native Instruments\Kontakt 8\lock.lck` | 0 bytes and **never modified** while Kontakt runs — useless as a "Kontakt is open" signal ([§9](#9-detecting-that-kontakt-is-running)). |
| `HKLM\SOFTWARE\Native Instruments\<RegKey>` | The library's registration. **Kontakt re-adds any library that has this key** ([§2](#2-library-lifecycle)). |
| `HKCU\SOFTWARE\Native Instruments\<RegKey>` | Per-user state: `UserRemoved` (DWORD 0), `UserListIndex` (DWORD 0). |
| `HKCU\SOFTWARE\Native Instruments\Kontakt 8` | Settings, including the recent-files list (`recentFileNameNr{i}`, `recentLuaScriptNr{i}`, `recentFileCount`) and `lastMissingSamplesFolder`. |
| `%COMMONPROGRAMFILES%\Native Instruments\Service Center\<RegKey>.xml` | The library's `ProductHints` XML (a copy of the one inside its `.nicnt`). |
| `%COMMONPROGRAMFILES%\Native Instruments\Service Center\NativeAccess.xml` | Native Access's product catalogue: ~856 NI products with `UPID`, `SNPID`, `RegKey`, `Name`. |
| `%PUBLIC%\Documents\Native Instruments\installed_products\<RegKey>.json` | `{"ContentDir": "<library folder>", "ContentVersion": "1.0.0"}` |
| `%PUBLIC%\Documents\NI Resources\image\<browser folder name>\` | Library artwork Kontakt's browser shows ([§6](#6-browser-artwork-header-colour-and-banner-text)). |
| `%PUBLIC%\Documents\NI Resources\_DatabaseResources\{color,shortname}\*.db` | Shared lookup tables: header colours and short banner names, ~2,300 products. |
| `%PUBLIC%\Documents\NI Resources\database\pal\categories.db` | Shared category database. |
| `%COMMONPROGRAMFILES%\Native Instruments\Kontakt 8\PAResources\image\<product>\` | Artwork for 166 NI products, installed with Kontakt itself (not per-library). |

`<RegKey>` comes from the `RegKey` (or `Name`) element of the library's `ProductHints` XML.

### Registry values written when a library is added

| Key | Value | Type | Notes |
|---|---|---|---|
| HKLM `…\<RegKey>` | `ContentDir` | REG_SZ | Library folder |
| | `ContentVersion` | REG_SZ | `1.0.0` |
| | `Visibility` | REG_DWORD | `3` (from `ProductSpecific/Visibility` in the XML) |
| | `HU`, `JDX` | REG_SZ | Only for vendor libraries — authorization values issued by NI, copied from the XML. Cannot be generated. |
| HKCU `…\<RegKey>` | `UserRemoved`, `UserListIndex` | REG_DWORD | `0`, `0` |

### Recent-file path encoding

Kontakt stores recent paths as `@v001` + drive letter + one `dNNN<folder>` per folder level
(`NNN` = the folder name's length, zero-padded) + `F…` for the file:

```
C:\Users\me\Samples\Strings\Violin.nki  →  @v001Cd005Usersd002med007Samplesd007StringsF…Violin.nki
```

So "is this recent file inside library X" is a prefix test on the encoded folder path.

---

## 2. Library lifecycle

- **On every launch Kontakt reads the `HKLM\SOFTWARE\Native Instruments\*` product keys** and
  creates a `k_content_path` row for any library it doesn't already have. Deleting a library from
  the database alone therefore does nothing — Kontakt recreates it next launch. The registration
  has to go first ([§8](#8-removing-a-library-completely)).
- If the folder in `ContentDir` no longer exists, the recreated row gets **`state = 4`** and the
  library is hidden in Kontakt's browser (but still in the database).
- Normal libraries have `state = 1`. `state = 2` appears on "Native Instruments User Content" and
  on a freshly added library — meaning unknown ([open questions](#open-questions)).
- Libraries have `monitored = 0`: **Kontakt scans a library's patches only when it is added.**
  Editing patch files afterwards is not picked up — the database must be updated to match
  ([§5](#5-patch-headers-and-the-browser-folder)).
- A library whose `ProductHints` has no `UPID` gets a **new random `upid` in the database on every
  launch**. Including a stable UPID in the `.nicnt` fixes this.
- Adding a library (as Kontakt Button and KLib do) = writing the registry keys, the Service Center
  XML and the `installed_products` JSON. Kontakt then indexes the library on its next start.

---

## 3. komplete.db3 — the library database

SQLite, WAL journal mode. Columns use a custom collation **`KOMPLETE`** that Kontakt registers at
runtime; any other client must register a collation with that name (a plain comparison works) or
queries touching those indexes fail. Timestamps (`created_at`, `updated_at`, `scanned_at`,
`mod_date`) are **nanoseconds since the Unix epoch**.

### Core tables

| Table | Purpose / key columns |
|---|---|
| `k_content_path` | One row per library: `id`, `path`, `content_type` (1 = NI, 2/3 = third-party/user), `alias` (display name), `state`, `scanned_at`, `product_id` (= SNPID), `upid`, `visible`, `monitored`, `product_version_s`, timestamps. |
| `k_sound_info` | One row per patch/sample: `name`, `author`, `vendor`, `device_type_flags`, `bank_chain_id`, `content_path_id`, `file_name` (**absolute path**), `file_ext`, `uuid`, `file_size`, `mod_date`, `favorite_id`, … |
| `k_bank_chain` | The browser folder hierarchy: `bcvendor`, `entry1` (top-level folder), `entry2`, `entry3`; `UNIQUE(bcvendor, entry1, entry2, entry3)`. |
| `k_sound_info_category` / `k_category` | Category tags. |
| `k_sound_info_mode` / `k_mode` | "Character" / mode tags. |
| `p_sound_info_{Instrument,Combined,Tool,Leap,Loop,Oneshot}_{1,2}` | Denormalised browser tables, filled by triggers. |
| `f_sound_info` | FTS5 search index (external content). |

### Browser tables (`p_sound_info_*`)

Maintained entirely by triggers on `k_sound_info` and `k_bank_chain`. A patch lands in a browser
table only if its library is `visible` and its bank chain's `entry1` is non-empty:

| Table | Condition |
|---|---|
| `Instrument` | `device_type_flags & 1` and `file_ext IN ('nki','nksn')` |
| `Combined` | `file_ext = 'nkm'` (multis) |
| `Tool` | `file_ext = 'nkt'` |
| `Leap` | `file_ext = 'nkl'` |
| `Loop` | `device_type_flags & 8`, audio file (`wav/aif/aiff/rx2`) |
| `Oneshot` | `device_type_flags & 4`, audio file |
| suffix `_1` / `_2` | library `content_type` 1 (NI) / 2–3 (third-party, user) |

### Full-text search and its traps

`f_sound_info` is an FTS5 **external-content** table (`content='v_sound_info'`). The view maps
`brand` = `bcvendor` (or "Native Instruments"), `product` = `entry1`, `bank` = `entry2`,
`subbank` = `entry3`, `type` = categories, `character` = modes. Triggers keep it in sync:
`BEFORE DELETE` on `k_sound_info` issues an FTS `'delete'` with the row's current values.

Two traps, both of which surface as **`database disk image is malformed`** (FTS5's
`SQLITE_CORRUPT_VTAB`, not real file corruption — `PRAGMA integrity_check` still says `ok`):

1. **Never delete from `f_sound_info` directly.** The triggers already do; a manual delete removes
   the entries first, then the trigger's `'delete'` targets entries that no longer exist.
2. **`update_f_sound_info` is an `AFTER UPDATE` trigger** whose `'delete'` step reads the row back
   from `v_sound_info` — i.e. the *new* values. Changing a patch's `bank_chain_id` therefore
   deletes tokens that were never indexed. Kontakt never hits this (it re-indexes by delete +
   insert); anything that updates in place must rebuild the index afterwards:

```sql
INSERT INTO f_sound_info(f_sound_info) VALUES('rebuild');
-- strict consistency check (raises "malformed" if the index and content disagree):
INSERT INTO f_sound_info(f_sound_info, rank) VALUES('integrity-check', 1);
```

Also: run deletes in **one transaction**. Statement-by-statement autocommit leaves half-deleted
libraries when something fails part-way.

### Renaming a library's browser folder in place

1. Rename the bank in each patch file ([§5](#5-patch-headers-and-the-browser-folder)).
2. Get-or-create `k_bank_chain(bcvendor, <new name>, entry2, entry3)` per existing chain (keeps
   sub-folders), repoint each patch's `bank_chain_id`, and refresh `file_size` / `mod_date`
   (ns) from the edited files.
3. Delete bank chains nothing references.
4. Rebuild `f_sound_info`.

The triggers update the browser tables; Kontakt shows the new folder on next launch — no
remove/re-add needed. Verified in Kontakt.

---

## 4. The .nicnt container format

A `.nicnt` sits in the library folder and describes the product. It is an NI "FC" container:
a chain of 256-byte section headers, little-endian throughout.

```
offset 0x000   MTD header #1 ("/\ NI FC MTD  /\")
               +0x42  UTF-16 container version ("1.0", "1.2.0", …)
               +0x84  u32 section index (1)
               +0x90  u32 metadata size, +0x94 u32 metadata size (512000 in vendor files)
       0x100   ProductHints XML, zero-padded to the metadata size
               MTD header #2   +0x84 u32 = 2, +0x90 u32 = 1
                 then u64 1 · u64 0xF0F0F0F0F0F0F0F0 · u64 file count · u64 total data size
               TOC header #1 ("/\ NI FC TOC  /\") + zeros to +0x260
                 then one 640-byte entry per file:
                   +0x00 u64 offset into the data area · +0x08 u64 index (1-based)
                   +0x20 UTF-16 name, '|' as path separator
                 then u64 total data size · u64 0xF1F1F1F1F1F1F1F1 · u64 0 · u64 0
               TOC header #2 + zeros to +0x260
               file data (concatenated), to end of file
```

Validation: every vendor `.nicnt` examined (7, from different vendors and NI) **rebuilds
byte-for-byte** from its XML and embedded files using this layout. Files made by third-party
generators may carry placeholder bytes (`?`) in the count/size fields and an unaligned second
section; Kontakt accepts them, so readers shouldn't trust those fields.

### Embedded files seen

| TOC name | Contents |
|---|---|
| `.LibBrowser.png` | Library banner (905×99 or 587×98) — *(inferred)* the Kontakt 5/6-era library tab. |
| `.db.cache` | `<soundinfos version="110"><all/></soundinfos>` |
| `.PAResources\|image\|<Name>\|*.png/.webp/.meta` | Library artwork → `NI Resources\image\<name>` ([§6](#6-browser-artwork-header-colour-and-banner-text)) |
| `.PAResources\|_DatabaseResources\|{color,shortname}\|*.db/.meta` | A *snapshot of the whole shared table* (hundreds to ~2,300 products), not just this library's rows. |
| `.PAResources\|database\|PAL\|categories.db`, `PAL.meta` | Shared category database. |

A minimal `.nicnt` that Kontakt accepts needs only the XML and a `.db.cache` entry. **Never ship a
partial `color.db` / `shortname.db`**: these are installed wholesale and would erase every other
product's colours and short names.

### ProductHints XML

```xml
<ProductHints spec="1.0.16">
  <Product version="1">
    <UPID>…uuid…</UPID>          <!-- stable product id; omit it and Kontakt invents one per launch -->
    <Name>…</Name>
    <Type>Content</Type>
    <NKSEnabled>true</NKSEnabled>                       <!-- vendor files -->
    <Relevance><Application minVersion="…" nativeContent="true">kontakt</Application>…</Relevance>
    <PoweredBy>Kontakt</PoweredBy>
    <Visibility>…</Visibility>
    <Company>…</Company>
    <AuthSystem>RAS3</AuthSystem>
    <SNPID>Q46</SNPID>
    <RegKey>…</RegKey>           <!-- name used for registry key, Service Center xml, json -->
    <Icon>kontakt</Icon>
    <ProductSpecific>
      <HU>…</HU><JDX>…</JDX>      <!-- vendor authorization; absent for custom libraries -->
      <Visibility type="Number">3</Visibility>
    </ProductSpecific>
  </Product>
</ProductHints>
```

---

## 5. Patch headers and the browser folder

**The folder a patch appears under in Kontakt's browser comes from the patch file itself, not
from the `.nicnt`.** Each `.nki` (and `.nkm` / `.nksn`) stores a bank list in its header; Kontakt
copies it into `k_bank_chain` when it scans the library. Patches whose bank is the default
**`"Kontakt"`** all land in Kontakt's shared "Kontakt" folder. Proven by:
- a library with a third-party-generated `.nicnt` but properly named patches getting its own
  folder, and libraries with vendor `.nicnt`s doing the same;
- renaming the bank in patch files and watching the library move to its own folder.

### Header layout

The header is an NI chunk tree:

- The file starts with a **u64 = the file's total size**.
- `hsin` chunks: `u64 size` (counted from the size field), `u32 version`, magic `hsin`, …, a
  16-byte GUID, then data and child chunks.
- `DSIN` items: `u64 size`, magic `DSIN`, `u32 id`, `u32 version`, … — chained.
- The **soundinfo** item is `DSIN` id **108**. It contains an inner `DSIN` id 1; 32 bytes after
  that inner magic the fields are:

```
5 strings      name, author, vendor, (2 more)       string = u32 length + UTF-16LE
8 × u32
bank list      u32 count (1–3), then that many strings   ← the browser folder path
tag list       u32 count, then strings                    (e.g. "KontaktInstrument", "Brass")
(Kontakt 6+)   attribute pairs: \@color, \@devicetypeflags, \@soundtype, \@tempo, \@verl, …
```

Examples: Freyja (Kontakt 5 format) `1 × "Kontakt"`; Cinematic Studio Brass
`1 × "Cinematic Studio Brass"`; LA Scoring Strings 3 `3 × "LASS Full 3 Library", "Instruments",
"- Splits"`.

### Renaming the bank

Replace the bank list's first string (keep sub-levels), then grow the `u64` size fields of every
chunk that encloses it by the same number of bytes — in practice four: the file-size field, two
`hsin` chunks and the `DSIN` 108 item. **There is no checksum.** Verified on 1,183 of 1,190 patch
files across 12 libraries (all formats parse; renaming and restoring is byte-identical); the 7
failures were download-damaged files. Kontakt loads and plays edited `.nki` files normally.
(`.nkm` / `.nksn` share the layout but only `.nki` was tested in Kontakt — multis and snapshots
don't decide a library's folder.)

Because Kontakt only scans on add, an installed library also needs its database updated
([§3](#renaming-a-librarys-browser-folder-in-place)).

---

## 6. Browser artwork, header colour and banner text

Kontakt looks all of this up by the library's **browser folder name** (its patches' bank name,
`k_bank_chain.entry1`), **case-insensitively** — not by the registry name or `.nicnt` name. A
mismatch hides everything: LA Scoring Strings 3 ships artwork in `la scoring strings 3`, but its
browser folder is "LASS Full 3 Library", so Kontakt shows none of it.

### Images — `%PUBLIC%\Documents\NI Resources\image\<name>\`

| Where in Kontakt | File | Size | Notes |
|---|---|---|---|
| Enlarged view tile | `NKS2_software_tile.webp` | 420×316 | Cropped to the **middle ~420×201** (≈58 px cut top and bottom). |
| ↳ fallback | `MST_artwork.png` | 134×66 in NI products, but any size works (Ethera ships 400×300 and looks sharper) | Used when there's no software tile. |
| Condensed view banner | `NKS2_hardware_banner.webp` | 1280×212 | Shown whole. Without it Kontakt shows text (`VB_shortname`, below). |
| Library header (above the patch list) | `VB_logo.png` | 227×47, **transparent** | Drawn at 2× on a bar 52 logo-px tall, **8 px from the left, 2 px from the top**; the bar spans the browser panel. Without a logo: library name in Kontakt's default style. |

Other files in NI's standard set — `MST_logo.png` 240×196, `MST_plugin.png` (varies),
`OSO_logo.png` 417×65, `VB_artwork.png` 96×47, `NKS2_hardware_banner_1280x152.webp` — do not
appear in Kontakt 8's browser *(inferred: Komplete Kontrol / Maschine)*. A `.meta` file is not
required (Ethera has none and displays). Loose files are enough; vendor libraries get them
extracted from the `.nicnt`'s `.PAResources|image|…` entries, and some installers copy them
directly.

### Header colour and banner text — shared databases

`%PUBLIC%\Documents\NI Resources\_DatabaseResources\color\color.db` and
`…\shortname\shortname.db`, both:

```sql
CREATE TABLE color (Location text NOT NULL COLLATE NOCASE, Name text NOT NULL COLLATE NOCASE,
                    Value text NOT NULL, seqidx integer DEFAULT 0)
-- shortname: same columns
```

`Location` = browser folder name.

| Name | Effect |
|---|---|
| `VB_bgcolor` (hex, e.g. `df8066`) | Library header background — **only shown behind a `VB_logo`**; without a logo the header stays Kontakt's default grey. |
| `MST_bgcolor`, `NKS2_control_color` | Other products' UIs *(inferred)*. |
| `VB_shortname` | Text in the condensed-view banner when there's no banner image ("CS Brass", "Freyja"). |
| `MST_shortname`, `MKII_shortname`, `MIKRO_shortname` | Hardware / other UIs *(inferred)*. |

All verified by adding rows for a library and watching Kontakt. These files are **replaced
wholesale** when a vendor library carrying a newer snapshot is installed, so custom rows must be
re-applied afterwards (KLib keeps its own record and does this on startup).

---

## 7. SNPIDs and product identity

- **SNPID**: 3 characters, `0-9A-Z` (some vendor ones are lower-case hex like `30a`), stored in
  `ProductHints/SNPID` and in `k_content_path.product_id`; also used in `LibrariesCache` names.
- Must be unique across every NI product (`NativeAccess.xml`, ~856 SNPIDs), every installed
  product (`Service Center\*.xml`) and the database.
- The `RegKey` / name must not collide with a real product's either — it names the registry key,
  so a collision would overwrite that product's registration.
- **UPID**: a UUID per product. Vendor files carry one; custom libraries can use any stable UUID
  (KLib derives one from name + SNPID with `uuid5`) and Kontakt keeps it.

---

## 8. Removing a library completely

Order matters: registration first, database last — otherwise Kontakt re-adds it.

1. `HKLM\SOFTWARE\Native Instruments\<RegKey>` (needs admin; delete recursively).
2. `HKCU\SOFTWARE\Native Instruments\<RegKey>`.
3. `Service Center\<RegKey>.xml` (needs admin).
4. `installed_products\<RegKey>.json`.
5. Recent-file entries inside the library folder (rewrite and renumber
   `recentFileNameNr*` / `recentLuaScriptNr*`, update `recentFileCount`) and
   `lastMissingSamplesFolder` if it points inside.
6. `NI Resources\image\<name>\` — matched by the `.nicnt`'s `.PAResources|image|<Name>` entries,
   the registry name, the database alias and the library's own bank names; never one another
   installed library still uses.
7. `LibrariesCache\K{SNPID}*.cache`.
8. Database rows, in one transaction: category/mode junction rows, `k_sound_info` (triggers clean
   the browser tables and FTS), bank chains nothing else uses, `k_content_path`.
9. Optionally the sample folder.

Matching libraries to their Service Center XML by `UPID` alone isn't reliable (see the random-UPID
behaviour); match by SNPID and `ContentDir` too. Leave `Kontakt 8\PAResources\image\<product>`
alone — that's NI's shipped artwork, not per-install data. Re-adding later works normally:
everything above is recreated.

---

## 9. Detecting that Kontakt is running

`lock.lck` stays 0 bytes with a September timestamp across many Kontakt sessions, so its size says
nothing. What works:

- the `Kontakt 8.exe` process (standalone), **and**
- whether any process has `komplete.db3` open: `CreateFileW` with share mode `0` fails with
  `ERROR_SHARING_VIOLATION` (32) while it's open — this also catches Kontakt running as a plugin
  inside a DAW, where no `Kontakt 8.exe` exists.

---

## Open questions

- `k_content_path.state = 2` — seen on User Content and on freshly added libraries; `1` is normal,
  `4` is missing.
- What `MST_*`, `OSO_logo`, `VB_artwork` and the 1280×152 banner are used for outside Kontakt 8's
  browser.
- Whether `.nkm` / `.nksn` files with renamed banks behave in Kontakt (they parse identically).
- The exact meaning of the 8 `u32` fields in the soundinfo block and of `Visibility` values
  (`1`, `3`, `7`).
