# KLib — Kontakt 8 Library Manager

A Windows desktop app for managing third-party sample libraries in Native Instruments **Kontakt 8**:
add them, give them proper names and artwork in Kontakt's browser, and remove them without a
trace. Built in Python ([pywebview](https://pywebview.flowrl.com/) UI, SQLite via
[apsw](https://rogerbinns.github.io/apsw/), [Pillow](https://python-pillow.org/) for images).

Kontakt keeps its library state spread across an undocumented SQLite database, the Windows
registry, NI's product files and its own patch formats. KLib works directly with all of them —
everything it does is based on reverse engineering those formats, written up in
**[docs/kontakt-8-internals.md](docs/kontakt-8-internals.md)**.

## Features

- **Add libraries** — reads the library's `.nicnt`, or **generates one** for libraries that come
  without it (name, optional company, a 3-character SNPID checked against Native Access's product
  catalogue and installed products). Registers it so Kontakt indexes it on its next launch.
- **Fix the browser folder** — patches whose embedded library name is the default "Kontakt" all
  pile into Kontakt's shared "Kontakt" folder. KLib rewrites the name inside each `.nki` (backing
  up the originals) so the library gets its own folder — when adding, or for installed libraries
  with no remove/re-add.
- **Artwork** — set the enlarged-view tile, condensed-view banner, header logo, header colour and
  banner text, with an interactive cropper framed exactly the way Kontakt displays each image.
- **Remove completely** — database rows, registry keys, product files, caches, artwork and
  recent-file entries, so nothing is left behind and the library can be reinstalled cleanly.
  Deleting the sample files is opt-in, with a clear warning.
- **Safe by default** — refuses to touch anything while Kontakt (standalone or inside a DAW) has
  its database open, with a *Try again* dialog; deletes are transactional; edited files are
  backed up.

## Reverse engineering Kontakt 8

Highlights from [the full write-up](docs/kontakt-8-internals.md):

- **Database (`komplete.db3`)** — mapped the schema: libraries, patches, the bank-chain folder
  hierarchy, trigger-maintained browser tables and an FTS5 search index. Diagnosed a
  `database disk image is malformed` error as an out-of-sync FTS5 external-content index, not
  corruption — including a flaw in Kontakt's own `AFTER UPDATE` trigger that desyncs the index
  whenever a patch's folder changes in place.
- **`.nicnt` container format** — decoded NI's "FC" container (section headers, table of
  contents, embedded files) well enough that KLib's writer **reproduces every vendor `.nicnt`
  byte-for-byte** from its parts.
- **Patch headers (`.nki`)** — located the library name inside the patch's chunk tree and worked out
  which size fields must change when it's rewritten (no checksum). Verified against 1,183 patch
  files; Kontakt loads the edited patches normally.
- **Registration** — found that Kontakt silently re-registers any library that still has an
  `HKLM` product key, which is why "deleted" libraries kept coming back, and mapped everything a
  complete removal has to touch.
- **Browser artwork** — determined which image files, sizes and crops Kontakt uses for each view,
  that it looks them up by browser folder name, and where header colours and short banner names
  live — confirmed with labelled test images and screenshots.

## Running it

Requires Windows, Python 3.12+ and Kontakt 8.

```
pip install -r requirements.txt
python main.py
```

KLib asks for administrator rights at startup (UAC): adding and removing a library's
registration writes to `HKEY_LOCAL_MACHINE`. To launch it without a console window (e.g. from a
desktop shortcut), use `pythonw.exe main.py`.

Build a standalone executable with PyInstaller:

```
pip install pyinstaller
pyinstaller klib.spec
```

## Project layout

| File | Role |
|---|---|
| `main.py` | Window, API exposed to the UI, admin elevation |
| `kontakt_manager.html` | The UI |
| `db.py` | `komplete.db3`: reading libraries, transactional delete, bank renames, Kontakt-running check |
| `register.py` | Adding a library: registry, Service Center, installed_products, SNPID / name checks |
| `cleanup.py` | Removing a library's registration and leftovers |
| `nicnt.py` | `.nicnt` reader and writer |
| `patches.py` | Reading / rewriting the library name inside patch files |
| `artwork.py` | Browser artwork, header colour and banner text |

## Disclaimer

KLib is an independent project, not affiliated with or endorsed by Native Instruments. Kontakt,
Native Access and Komplete are trademarks of Native Instruments GmbH. The file formats were
documented by observing a local installation for interoperability; KLib can't create the
authorization data vendor libraries carry. Back up `komplete.db3` before experimenting.
