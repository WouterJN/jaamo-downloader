# Jaamo Photo Downloader — Technical Reference

## What this project does

A Python desktop app (Tkinter GUI) that logs into the Jaamo parent portal for SKSG daycare
(`https://sksg.jaamo.nl`) and lets a parent browse, select, and download all photos of their
child. Photos are grouped by date. Downloaded files have EXIF metadata (date, caption, GPS)
injected automatically.

## Run

```bash
cd /Users/wouter/Claude/jaamo-downloader
python3 app.py
```

## Tests

```bash
cd /Users/wouter/Claude/jaamo-downloader
python3 -m unittest tests -v
```

87 tests across 10 test classes. No Tkinter display required — `JaamoApp` is never
instantiated; tests only cover module-level helpers and replicated parsing logic.

| Class | Cases | What is tested |
|---|---|---|
| `TestParseNlDate` | 11 | All 12 month abbreviations, whitespace, `None`, empty string, bad input |
| `TestDecimalToDms` | 9 | Degree/minute values for school coords, roundtrip accuracy, zero, whole degree |
| `TestBuildFilename` | 13 | Query stripping, hex hash, duplicate extension, date/name prefix, all 12 months, real S3 URL, fallback |
| `TestUniquePath` | 5 | No collision, `_1` suffix, `_2`/`_3` increments, extension preserved, result doesn't pre-exist |
| `TestSetFileTime` | 4 | Correct date, noon time, invalid date leaves mtime, `None` leaves mtime |
| `TestInjectExifMetadata` | 11 | All three date fields, GPS N/E refs, caption in `ImageDescription` + `UserComment`, Unicode emoji, no caption, bad JPEG fallback |
| `TestApplyMetadata` | 5 | Valid date, invalid date, `None` date, caption passthrough, empty caption |
| `TestGalleryHtmlParsing` | 14 | Group count, dates, photo counts, URL/thumb/caption extraction, missing `data-src` skipped, empty gallery |
| `TestAccountsHtmlParsing` | 10 | Two children, deduplication, non-child links ignored, fallback name, empty page |
| `TestCredentialFile` | 5 | Save/load roundtrip, chmod 600, missing file graceful, clear removes file, clear on missing doesn't raise |

### Hex-hash stripping note

`_build_filename` strips leading hex prefixes matching `^[0-9a-f]+_`
(e.g. `50335e52be_IMG_0558.jpeg` → `IMG_0558.jpeg`).
The prefix `full_image_17bb0f_` is **not** stripped because `u`, `l`, `g` are not
hex digits — this is intentional and verified by `test_real_s3_url_full_pipeline`.

---

## Stack

| Layer | Technology |
|---|---|
| GUI | Python `tkinter` + `ttk` (theme: `clam`) |
| HTTP | `requests.Session` (cookie-based auth) |
| HTML parsing | `beautifulsoup4` |
| Image display | `Pillow` (`PIL.ImageTk`) |
| EXIF writing | `piexif` |
| Concurrency | `threading` (daemon threads) + `root.after()` for thread-safe UI updates |

Install: `pip install requests beautifulsoup4 Pillow piexif`

---

## Authentication

The site is a **Ruby on Rails** app (not Laravel). The login flow is:

1. `GET https://sksg.jaamo.nl/login/ouder/sksg`
   — Returns HTML with `<input name="authenticity_token">` (Rails CSRF token).

2. `POST https://sksg.jaamo.nl/login/ouder/sksg` with:
   ```
   authenticity_token = <value from step 1>
   user[email]        = <email>
   user[password]     = <password>
   user[remember_me]  = 1
   commit             = Inloggen
   ```

3. Success: final URL does **not** contain `/login`.
   Failure: final URL still contains `/login`.

`requests.Session()` stores the session cookie automatically.

### Saved credentials

Credentials are saved to `~/.jaamo_credentials.json` (chmod 600) when the
"Inloggegevens onthouden" checkbox is ticked. Cleared when unticked.

---

## Photos page

**URL:** `https://sksg.jaamo.nl/ouders/children/{child_id}/photos`  
Child ID is discovered at runtime from `/ouders/accounts` — it is not hardcoded.

- All photos are on **one page** — no pagination needed.

### HTML structure

Photos and date headers are **siblings** inside `<div class="image_gallery">`:

```html
<div class="image_gallery d-flex flex-wrap mx-2">

  <!-- Date header -->
  <div class="col-12 font_semi_bold text-start">29 jun 2026</div>

  <!-- Photo entry -->
  <div class="image_canvas col-3 pe-2 pb-2">
    <a href="[full_url]">
      <div class="image_container"
           data-src="[full_url]"
           data-sub-html="#caption-1048739">
        <img class="img-fluid"
             data-original="[full_url]"
             src="/empty-rectangle.jpg"/>
      </div>
      <div id="caption-1048739" style="display:none">
        <p>Caption text here</p>
      </div>
    </a>
  </div>

</div>
```

### Parsing logic

Walk `image_gallery` children in order:
- `div.col-12.font_semi_bold` → new date group
- `div.image_canvas` → photo entry for current date group
  - Full URL: `div.image_container[data-src]`
  - Thumbnail URL: `img[data-original]` (same URL as full in the photos page — no `thumb_` prefix here)
  - Caption: find `div[id=caption-NNN]` where NNN comes from `data-sub-html="#caption-NNN"` on the container

### S3 URL signing

All S3 URLs are pre-signed with **600-second (10-minute) expiry** (`X-Amz-Expires=600`).
Download promptly after page fetch. Expired URLs return HTTP 403.
To recover: re-fetch the photos page to get fresh signed URLs (call `_load_photos()`).

---

## EXIF metadata injection

The server strips all EXIF on upload. The app re-injects metadata on every save using `piexif` + PIL:

### Tags written

| Tag | IFD | Value |
|---|---|---|
| `DateTime` | `0th` | Date from page header, time `00:00:00` |
| `DateTimeOriginal` | `Exif` | Same |
| `DateTimeDigitized` | `Exif` | Same |
| `ImageDescription` | `0th` | Caption (latin-1 encoded) — if present |
| `UserComment` | `Exif` | `b"UNICODE\x00" + caption.encode("utf-16-le")` — if present |
| `GPSLatitudeRef` | `GPS` | `b"N"` |
| `GPSLatitude` | `GPS` | DMS rationals for `SCHOOL_LAT` |
| `GPSLongitudeRef` | `GPS` | `b"E"` |
| `GPSLongitude` | `GPS` | DMS rationals for `SCHOOL_LON` |

### GPS school location

`SCHOOL_LAT` and `SCHOOL_LON` are decimal-degree constants defined at the top of `app.py`.
All photos get this location regardless of caption or date.

### DMS conversion

```python
def _decimal_to_dms(value):
    d = int(abs(value))
    m = int((abs(value) - d) * 60)
    s = round((abs(value) - d - m / 60) * 3600 * 1_000_000)
    return [(d, 1), (m, 1), (s, 1_000_000)]
```

### Why `piexif.insert` is not used

`piexif.insert(exif_bytes, jpeg_bytes)` requires a file path in newer piexif versions.
Instead, use PIL's `img.save(out, format="JPEG", exif=piexif.dump(exif_dict), quality=95)`.

### File timestamps

`os.utime(path, (ts, ts))` sets mtime to noon on the photo date so Finder sorts correctly.

---

## App architecture (`app.py`)

### File organisation

```
imports (stdlib alphabetical, then third-party)
constants      BASE_URL, ACCOUNTS_URL, SCHOOL_LAT/LON, THUMB_SIZE, …
palette        BG, CARD, PRIMARY, …
helpers        _parse_nl_date, _decimal_to_dms, _inject_exif_metadata,
               _apply_metadata, _set_file_time, _unique_path, _build_filename
_apply_styles()
JaamoApp class
__main__ block
```

### Flow

```
JaamoApp.__init__
  └─ _setup_login_frame()

[user clicks Inloggen]
  └─ _on_login()
       └─ thread: _do_login()          # GET login → CSRF → POST creds
            └─ root.after: _login_ok()
                 └─ show spinner
                 └─ thread: _fetch_children()   # GET /ouders/accounts, parse child links
                      └─ root.after: _on_children_loaded(children)
                           ├─ (1 child)  → _start_gallery(id, name)
                           └─ (N children) → _show_child_selector(children)
                                └─ [click] → _start_gallery(id, name)
                                     ├─ _setup_gallery_frame()
                                     └─ _load_photos()
                                          └─ thread: _fetch_photos()
                                               └─ root.after: _build_gallery(groups, total)
                                                    └─ for each group: create header + grid frame
                                                         └─ N threads: _load_thumb()
                                                              └─ root.after: _place_thumb()
```

### Key data structures

```python
# One group per date
self._groups = [
    {
        "date":    "29 jun 2026",
        "entries": [
            {"thumb": "https://s3.../...", "full": "https://s3.../...", "caption": ""},
            ...
        ],
        "frame":   <tk.Frame>,      # thumbnail grid widget
        "sel_btn": <ttk.Button>,    # "Selecteer dag" button reference
    },
    ...
]

self._selected     = set()  # set of selected full URLs
self._cells        = {}     # full_url → cell tk.Frame (for border updates)
self._photo_refs   = []     # ImageTk.PhotoImage refs — must be kept alive to prevent GC
self._loaded_count = 0      # thumbnails loaded so far (progress display)
self._total_count  = 0      # total thumbnails expected
self._children     = []     # full child list from accounts page — kept for "back" navigation
self._photos_url   = ""     # set by _start_gallery; URL for the selected child's photos page
self._child_name   = ""     # full name of the selected child
```

### Selection helpers

`_set_cell_selected(url, selected)` — single place that updates a cell's highlight border.
`_toggle_select`, `_toggle_select_day`, and `_select_all` all delegate to it.

### Download flow

Both "Download selectie" and "Download dag" build a `pairs` list of `(entry, date_str)`
and call the same `_save_photos(pairs, folder)` worker thread:

```python
def _save_photos(self, pairs, folder):
    # downloads, injects EXIF, writes file, sets mtime — for each pair
```

### Thread safety rule

Never call Tkinter widget methods from worker threads.
Always schedule via `self.root.after(0, callable)`.

**Python 3.13 gotcha:** exception variables are deleted after the `except` block.
Closures in lambdas scheduled via `root.after` will fail if they reference `e` directly:

```python
# WRONG
except Exception as e:
    self.root.after(0, lambda: self._fail(str(e)))  # e is gone

# CORRECT
except Exception as e:
    msg = str(e)
    self.root.after(0, lambda: self._fail(msg))
```

---

## UI layout

### Login screen
Centered card on a `BG`-coloured frame. Email + password fields, "remember me" checkbox.
Pre-fills from `~/.jaamo_credentials.json` on startup. Enter key triggers login.

### Gallery toolbar (top bar)
Left: "← Terug" ghost button (only when 2+ children), then title "Foto's — {child name}".
Right (right to left): "Download selectie (N)", "Selecteer alles", "Vernieuwen".
Progress label + selection count label shown between buttons and title.

### Gallery body
Scrollable `tk.Canvas` containing `scroll_frame`. Each date group has:
1. A `DateBar.TFrame` header with date + count label, "Selecteer dag" ghost button, "Download dag" blue button.
2. A `tk.Frame` grid with `COLS=4` columns of photo cells.

Each photo cell (`tk.Frame` with highlight border):
- `tk.Label` with thumbnail image
- "Klik om te selecteren" hint
- Optional caption (first line, max 40 chars)

### Selection

- Click any cell (image, hint, or caption label) → `_toggle_select(url, group)`
- Toggles URL in `self._selected`, changes cell border (`BORDER`/1px → `PRIMARY`/3px)
- "Selecteer dag" button → `_toggle_select_day(group)` — selects/deselects all in group
- Day button label flips between "Selecteer dag" / "Deselecteer dag"
- Toolbar shows count and enables "Download selectie (N)" button

### Scrollbar

Thin 6px modern scrollbar using a custom ttk layout that removes arrow buttons:

```python
style.layout("Thin.Vertical.TScrollbar", [
    ("Vertical.TScrollbar.trough", {"children": [
        ("Vertical.TScrollbar.thumb", {"expand": "1", "sticky": "nswe"})
    ], "sticky": "ns"})
])
```

### Mouse scroll

macOS `event.delta` is small (3–6), not multiples of 120 like Windows.

```python
if platform.system() == "Darwin":
    canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-e.delta, "units"))
else:
    canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
    canvas.bind_all("<Button-4>",   lambda e: canvas.yview_scroll(-1, "units"))  # Linux
    canvas.bind_all("<Button-5>",   lambda e: canvas.yview_scroll(1,  "units"))  # Linux
```

---

## Download modes

| Button | Behaviour |
|---|---|
| Click photo | `_toggle_select` — select/deselect for batch download |
| Selecteer alles | `_select_all` — selects every photo, enables "Download selectie" |
| Selecteer dag | `_toggle_select_day` — selects/deselects all photos for that date |
| Download selectie (N) | `_download_selected` → `_save_photos` — saves N selected photos to one flat folder |
| Download dag | `_download_date` → `_save_photos` — saves all photos for one date to a chosen folder |

All downloads go through `_save_photos(pairs, folder)`, which calls
`_apply_metadata(jpeg_bytes, date_str, caption)` then `_set_file_time` for each file.

### Filename format

`SKSG_{first_name}_{dd}-{month}-{yyyy}_{original}.jpeg`
e.g. `SKSG_{child}_29-june-2026_IMG_0558.jpeg`

- First name = `self._child_name.split()[0]`
- Date from the date group header, converted via `_parse_nl_date` + `_EN_MONTHS`
- Original filename with duplicate extension and leading hex hash stripped
- Built by `_build_filename(url, fallback_idx, date_str, first_name)`

---

## Colour palette

| Name | Hex | Use |
|---|---|---|
| `BG` | `#F0F4F8` | Window background |
| `CARD` | `#FFFFFF` | Login card, toolbar, photo cells |
| `PRIMARY` | `#2B6CB0` | Buttons, selected cell border |
| `SUCCESS` | `#276749` | Reserved / future use |
| `WARN` | `#B7791F` | "Download selectie" button |
| `DATE_BG` | `#EBF4FF` | Date header bar background |
| `DATE_FG` | `#2B6CB0` | Date header text |
| `MUTED` | `#718096` | Secondary text, hint labels |
| `BORDER` | `#CBD5E0` | Unselected cell border, separator |

---

## Child selector

After login the app fetches `GET /ouders/accounts` and looks for all
`<a href="/ouders/children/NNN">` links. Each link contains:

```html
<a class="text-decoration-none" href="/ouders/children/{id}">
  <img alt="{Child Name}" class="thumbnail img-fluid" src="https://s3..."/>
  ...{Child Name}...
</a>
```

- Child ID comes from the `href`.
- Child name comes from `img[alt]`.
- Profile thumbnail is fetched in the background thread and passed as a PIL Image;
  `ImageTk.PhotoImage` is created on the main thread in `_show_child_selector`.

If there is exactly one child the selector screen is skipped.
`self._photos_url` and `self._child_name` are set by `_start_gallery(child_id, name)`.
`self._children` is always stored so the back button can re-show the selector without a new network request.

### Back button

A "← Terug" ghost button appears in the gallery toolbar **only when there are 2+ children**.
It calls `_back_to_child_selector()`, which destroys `self._gallery_frame` and calls
`_show_child_selector(self._children)`. The entire gallery (toolbar, separator, canvas) lives
inside `self._gallery_frame` so one `.destroy()` tears it all down cleanly.

---

## Known limitations

- **10-minute URL expiry**: S3 signed URLs expire 600 s after page load. If download starts
  after that, URLs return 403. Fix: click "Vernieuwen" to re-fetch fresh URLs.
- **JS-free scraping**: works because Jaamo renders HTML server-side. If they migrate to a
  JS SPA, Selenium/Playwright would be needed.
- **Caption encoding**: `ImageDescription` is latin-1 (EXIF spec). Characters outside latin-1
  are replaced with `?`. Full Unicode is in `UserComment` (utf-16-le).

---

## Credentials reference

| Field | Value |
|---|---|
| Login URL | `https://sksg.jaamo.nl/login/ouder/sksg` |
| Accounts URL | `https://sksg.jaamo.nl/ouders/accounts` |
| Photos URL | `https://sksg.jaamo.nl/ouders/children/{id}/photos` (dynamic) |
| Organisation slug | `sksg` |
| Child ID | Discovered at runtime from accounts page |
| CSRF field | `authenticity_token` |
| Email field | `user[email]` |
| Password field | `user[password]` |
| Saved credentials | `~/.jaamo_credentials.json` (chmod 600) |
