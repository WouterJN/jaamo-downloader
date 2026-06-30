"""
Jaamo Photo Downloader
Logs into the Jaamo parent portal, lists children, and lets a parent
browse, select, and download photos with EXIF metadata injected on save.
Works with any daycare using the Jaamo platform.
"""

import datetime
import hashlib
import io
import json
import os
import platform
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import piexif
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageTk

# ── Constants ─────────────────────────────────────────────────────────────────

__version__      = "1.2.0"

BASE_URL         = "https://sksg.jaamo.nl"
LOGIN_URL        = f"{BASE_URL}/login/ouder/sksg"
ACCOUNTS_URL     = f"{BASE_URL}/ouders/accounts"

# GPS coordinates of the SKSG school — written into EXIF of every photo.
SCHOOL_LAT       =  53.2263183712867
SCHOOL_LON       =   6.581978296365286

THUMB_SIZE       = (180, 180)
DIARY_THUMB_SIZE = (120, 120)
COLS             = 4                                      # photos per row in gallery
CREDENTIALS_FILE  = os.path.expanduser("~/.jaamo_credentials.json")
SETTINGS_FILE     = os.path.expanduser("~/.jaamo_settings.json")

_CACHE_ROOT       = os.path.expanduser("~/.jaamo_cache")
THUMB_CACHE_DIR   = os.path.join(_CACHE_ROOT, "thumbs")
DTHUMB_CACHE_DIR  = os.path.join(_CACHE_ROOT, "diary_thumbs")
STORY_CACHE_DIR   = os.path.join(_CACHE_ROOT, "stories")

# Dutch month abbreviations as they appear on the Jaamo photos page.
_NL_MONTHS = {
    "jan": 1, "feb": 2, "mrt": 3, "apr": 4,  "mei": 5,  "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12,
}
_EN_MONTHS = {
    1: "january", 2: "february",  3: "march",    4: "april",
    5: "may",     6: "june",      7: "july",      8: "august",
    9: "september", 10: "october", 11: "november", 12: "december",
}

# ── Colour palette ────────────────────────────────────────────────────────────

BG       = "#F0F4F8"   # window background
CARD     = "#FFFFFF"   # cards, toolbar, photo cells
PRIMARY  = "#2B6CB0"   # primary buttons, selected cell border
PRIMARY2 = "#2C5282"   # primary button hover
SUCCESS  = "#276749"
SUCCESS2 = "#22543D"
MUTED    = "#718096"   # secondary text, hints
BORDER   = "#CBD5E0"   # unselected cell border, separator line
ERROR    = "#C53030"
TEXT     = "#1A202C"
DATE_BG  = "#EBF4FF"   # date group header background
DATE_FG  = "#2B6CB0"   # date group header text

# ── EXIF helpers ──────────────────────────────────────────────────────────────

def _parse_nl_date(date_str):
    """Parse '29 jun 2026' → datetime.date, or None on failure."""
    try:
        parts = date_str.strip().split()
        return datetime.date(int(parts[2]), _NL_MONTHS[parts[1].lower()], int(parts[0]))
    except Exception:
        return None


def _decimal_to_dms(value):
    """Convert decimal degrees to piexif rational triples (deg, min, sec×1_000_000)."""
    d = int(abs(value))
    m = int((abs(value) - d) * 60)
    # Store seconds as an integer numerator over 1_000_000 to keep 6 decimal places.
    s = round((abs(value) - d - m / 60) * 3600 * 1_000_000)
    return [(d, 1), (m, 1), (s, 1_000_000)]


def _inject_exif_metadata(jpeg_bytes, date, caption=None, lat=SCHOOL_LAT, lon=SCHOOL_LON):
    """Return jpeg_bytes with date, optional caption, and GPS coordinates in EXIF.

    lat/lon default to the module-level school constants but can be overridden
    at call time with user-configured values.

    Uses PIL img.save() instead of piexif.insert() because newer piexif versions
    require a file path argument for insert(), which breaks in-memory usage.
    """
    dt_str = date.strftime("%Y:%m:%d 00:00:00").encode()

    try:
        exif_dict = piexif.load(jpeg_bytes)
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}

    # Write date to all three standard date fields.
    exif_dict.setdefault("Exif", {})[piexif.ExifIFD.DateTimeOriginal]  = dt_str
    exif_dict.setdefault("Exif", {})[piexif.ExifIFD.DateTimeDigitized] = dt_str
    exif_dict.setdefault("0th",  {})[piexif.ImageIFD.DateTime]         = dt_str

    if caption:
        # ImageDescription is latin-1 (EXIF spec); UserComment carries full Unicode.
        exif_dict.setdefault("0th",  {})[piexif.ImageIFD.ImageDescription] = \
            caption.encode("latin-1", errors="replace")
        exif_dict.setdefault("Exif", {})[piexif.ExifIFD.UserComment] = \
            b"UNICODE\x00" + caption.encode("utf-16-le")

    gps = exif_dict.setdefault("GPS", {})
    gps[piexif.GPSIFD.GPSLatitudeRef]  = b"N" if lat >= 0 else b"S"
    gps[piexif.GPSIFD.GPSLatitude]     = _decimal_to_dms(lat)
    gps[piexif.GPSIFD.GPSLongitudeRef] = b"E" if lon >= 0 else b"W"
    gps[piexif.GPSIFD.GPSLongitude]    = _decimal_to_dms(lon)

    try:
        img = Image.open(io.BytesIO(jpeg_bytes))
        out = io.BytesIO()
        img.save(out, format="JPEG", exif=piexif.dump(exif_dict), quality=95)
        return out.getvalue()
    except Exception:
        return jpeg_bytes   # fall back to original if re-encoding fails


def _apply_metadata(jpeg_bytes, date_str, caption=None, lat=SCHOOL_LAT, lon=SCHOOL_LON):
    """Parse date_str and inject EXIF; returns original bytes if date is unparseable."""
    date = _parse_nl_date(date_str) if date_str else None
    return _inject_exif_metadata(jpeg_bytes, date, caption, lat, lon) if date else jpeg_bytes


def _set_file_time(path, date_str):
    """Set file mtime to noon on the photo date so Finder/Explorer sorts correctly."""
    date = _parse_nl_date(date_str) if date_str else None
    if date:
        ts = time.mktime(datetime.datetime(date.year, date.month, date.day, 12, 0).timetuple())
        os.utime(path, (ts, ts))

# ── File helpers ──────────────────────────────────────────────────────────────

def _unique_path(folder, name):
    """Return folder/name, appending _1, _2, … if the file already exists."""
    dest = os.path.join(folder, name)
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(dest)
    i = 1
    while os.path.exists(dest):
        dest = f"{base}_{i}{ext}"
        i += 1
    return dest


def _build_filename(url, fallback_idx, date_str=None, first_name=None):
    """Build SKSG_{name}_{dd}-{month}-{yyyy}_{original}.ext from a S3 photo URL.

    Strips:
    - Query string (pre-signed S3 params)
    - Duplicate extension  e.g. "photo.jpeg.jpeg" → "photo.jpeg"
    - Leading hex hash     e.g. "50335e52be_IMG_0558.jpeg" → "IMG_0558.jpeg"
    """
    raw = url.split("/")[-1].split("?")[0] or f"foto_{fallback_idx}.jpg"

    stem, ext = os.path.splitext(raw)
    stem2, ext2 = os.path.splitext(stem)
    if ext and ext == ext2:
        raw = stem2 + ext                              # drop duplicate extension

    raw = re.sub(r"^[0-9a-f]+_", "", raw)            # drop leading hex hash

    date_prefix = ""
    if date_str:
        date = _parse_nl_date(date_str)
        if date:
            date_prefix = f"{date.day:02d}-{_EN_MONTHS[date.month]}-{date.year}_"

    name_prefix = f"{first_name}_" if first_name else ""
    return f"SKSG_{name_prefix}{date_prefix}{raw}"

# ── ttk styles ────────────────────────────────────────────────────────────────

def _thumb_cache_path(url, cache_dir):
    """Return the file path where a thumbnail for `url` is (or would be) cached.

    The query string is stripped so the same image maps to the same cache file
    even when the S3 pre-signed URL is refreshed.
    """
    stable = url.split("?")[0]
    key    = hashlib.md5(stable.encode()).hexdigest()
    return os.path.join(cache_dir, key + ".jpg")


def _story_cache_path(child_id, story_id):
    """Return the file path for a cached story entry."""
    return os.path.join(STORY_CACHE_DIR, str(child_id), f"{story_id}.json")


def _load_story_cache(path):
    """Load a cached story dict from *path*; return None if absent or corrupt."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_story_cache(path, entry):
    """Persist a parsed story dict to *path* as JSON, creating parent dirs."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False)


def _apply_styles():
    """Configure the clam theme with app-specific styles and a thin modern scrollbar.

    clam is used instead of the native Aqua theme because Aqua ignores background/
    foreground on tk.Button widgets, making consistent cross-platform colouring impossible.
    """
    s = ttk.Style()
    s.theme_use("clam")

    # Frames
    s.configure("TFrame",         background=BG)
    s.configure("Card.TFrame",    background=CARD)
    s.configure("DateBar.TFrame", background=DATE_BG)

    # Labels
    s.configure("TLabel",          background=BG,      foreground=TEXT,    font=("Helvetica", 11))
    s.configure("Card.TLabel",     background=CARD,    foreground=TEXT,    font=("Helvetica", 11))
    s.configure("Title.TLabel",    background=CARD,    foreground=TEXT,    font=("Helvetica", 22, "bold"))
    s.configure("Sub.TLabel",      background=CARD,    foreground=MUTED,   font=("Helvetica", 11))
    s.configure("Err.TLabel",      background=CARD,    foreground=ERROR,   font=("Helvetica", 10))
    s.configure("Bar.TLabel",      background=CARD,    foreground=MUTED,   font=("Helvetica", 10))
    s.configure("BarTitle.TLabel", background=CARD,    foreground=TEXT,    font=("Helvetica", 13, "bold"))
    s.configure("Date.TLabel",     background=DATE_BG, foreground=DATE_FG, font=("Helvetica", 11, "bold"))

    # Entry
    s.configure("TEntry", fieldbackground=CARD, foreground=TEXT,
                font=("Helvetica", 11), bordercolor=BORDER, padding=6)
    s.map("TEntry", bordercolor=[("focus", PRIMARY)])

    # Checkbox
    s.configure("TCheckbutton", background=CARD, foreground=TEXT, font=("Helvetica", 10))

    # Buttons — loop to avoid repeating configure/map for each variant
    for name, bg, hover in [
        ("Primary.TButton", PRIMARY,   PRIMARY2),
        ("Success.TButton", SUCCESS,   SUCCESS2),
        ("Warn.TButton",    "#B7791F", "#975A16"),
        ("Date.TButton",    PRIMARY,   PRIMARY2),
        ("Ghost.TButton",   BG,        BORDER),
    ]:
        s.configure(name, background=bg, foreground=CARD,
                    font=("Helvetica", 10, "bold"), borderwidth=0,
                    focusthickness=0, padding=(10, 5))
        s.map(name,
              background=[("active", hover), ("disabled", BORDER)],
              foreground=[("disabled", MUTED)])
    # Ghost button uses muted text, not white
    s.configure("Ghost.TButton", foreground=MUTED)
    s.map("Ghost.TButton", foreground=[("active", TEXT)])

    # Diary progress bar — thin strip, fills left-to-right as stories load
    s.configure("Diary.Horizontal.TProgressbar",
                background=PRIMARY, troughcolor=BORDER,
                borderwidth=0, thickness=3)

    # Thin modern scrollbar — override the layout to remove arrow buttons entirely
    s.layout("Thin.Vertical.TScrollbar", [
        ("Vertical.TScrollbar.trough", {"children": [
            ("Vertical.TScrollbar.thumb", {"expand": "1", "sticky": "nswe"})
        ], "sticky": "ns"})
    ])
    s.configure("Thin.Vertical.TScrollbar",
                background="#B0B8C8", troughcolor=BG,
                borderwidth=0, relief="flat", width=6)
    s.map("Thin.Vertical.TScrollbar",
          background=[("active", PRIMARY), ("pressed", PRIMARY2)])


# ── Application ───────────────────────────────────────────────────────────────

class JaamoApp:
    """Main application class. One instance per process."""

    def __init__(self, root):
        self.root = root
        self.root.title(f"Jaamo Photo Downloader v{__version__}")
        self.root.minsize(750, 520)
        self.root.configure(bg=BG)
        _apply_styles()

        for _d in (THUMB_CACHE_DIR, DTHUMB_CACHE_DIR, STORY_CACHE_DIR):
            os.makedirs(_d, exist_ok=True)

        # Persistent HTTP session keeps the auth cookie across all requests.
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})

        # Gallery state
        self._photo_refs   = []   # kept alive to prevent Tkinter GC of ImageTk images
        self._groups       = []   # [{"date": str, "entries": [...], "frame": Frame, "sel_btn": Button}]
        self._selected     = set()  # full URLs currently selected for download
        self._cells        = {}   # full_url → cell tk.Frame (for border highlight)
        self._loaded_count = 0
        self._total_count  = 0

        # Child / navigation state
        self._children   = []   # full list from accounts page — reused by "← Terug" button
        self._child_name = ""
        self._photos_url = ""   # set by _start_gallery(); URL for the selected child

        # GPS coordinates written into EXIF of every downloaded photo.
        # Loaded from SETTINGS_FILE; fall back to the module-level school defaults.
        settings         = self._load_settings()
        self._gps_lat    = settings.get("gps_lat", SCHOOL_LAT)
        self._gps_lon    = settings.get("gps_lon", SCHOOL_LON)

        self._setup_login_frame()

    # ── Settings ──────────────────────────────────────────────────────────────

    def _load_settings(self):
        try:
            with open(SETTINGS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_settings(self):
        with open(SETTINGS_FILE, "w") as f:
            json.dump({"gps_lat": self._gps_lat, "gps_lon": self._gps_lon}, f)

    # ── Credentials ───────────────────────────────────────────────────────────

    def _load_credentials(self):
        try:
            with open(CREDENTIALS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_credentials(self, email, password):
        with open(CREDENTIALS_FILE, "w") as f:
            json.dump({"email": email, "password": password}, f)
        os.chmod(CREDENTIALS_FILE, 0o600)

    def _clear_credentials(self):
        try:
            os.remove(CREDENTIALS_FILE)
        except FileNotFoundError:
            pass

    # ── Login screen ──────────────────────────────────────────────────────────

    def _setup_login_frame(self):
        outer = ttk.Frame(self.root)
        outer.pack(expand=True, fill="both")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        card = ttk.Frame(outer, style="Card.TFrame", padding=40)
        card.grid(row=0, column=0)

        ttk.Label(card, text="Jaamo",           style="Title.TLabel").grid(row=0, column=0, columnspan=2, pady=(0, 4))
        ttk.Label(card, text="Photo Downloader", style="Sub.TLabel"  ).grid(row=1, column=0, columnspan=2, pady=(0, 28))

        ttk.Label(card, text="E-mail",     style="Card.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.email_var = tk.StringVar()
        email_entry = ttk.Entry(card, textvariable=self.email_var, width=34)
        email_entry.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 16))

        ttk.Label(card, text="Wachtwoord", style="Card.TLabel").grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.pw_var = tk.StringVar()
        ttk.Entry(card, textvariable=self.pw_var, show="•", width=34).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(0, 12))

        self.remember_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(card, text="Inloggegevens onthouden",
                        variable=self.remember_var).grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 20))

        self.login_btn = ttk.Button(card, text="Inloggen",
                                    style="Primary.TButton", command=self._on_login)
        self.login_btn.grid(row=7, column=0, columnspan=2, sticky="ew")

        self.status_var = tk.StringVar()
        ttk.Label(card, textvariable=self.status_var, style="Err.TLabel").grid(
            row=8, column=0, columnspan=2, pady=(10, 0))

        ttk.Button(card, text="GPS Location", style="Ghost.TButton",
                   command=self._show_location_dialog).grid(
            row=9, column=0, columnspan=2, pady=(12, 0))

        # Pre-fill from saved credentials if available
        creds = self._load_credentials()
        if creds.get("email"):    self.email_var.set(creds["email"])
        if creds.get("password"): self.pw_var.set(creds["password"])

        email_entry.focus_set()
        self.root.bind("<Return>", lambda _: self._on_login())
        self._login_outer = outer

    def _on_login(self):
        email    = self.email_var.get().strip()
        password = self.pw_var.get()
        if not email or not password:
            self.status_var.set("Vul e-mail en wachtwoord in.")
            return
        self.login_btn.config(state="disabled", text="Bezig…")
        self.status_var.set("")
        threading.Thread(target=self._do_login, args=(email, password), daemon=True).start()

    def _do_login(self, email, password):
        """
        Rails CSRF login: GET the login page to obtain the authenticity_token,
        then POST credentials. Success = redirect away from /login.
        """
        try:
            r     = self.session.get(LOGIN_URL, timeout=15)
            r.raise_for_status()
            soup  = BeautifulSoup(r.text, "html.parser")
            token = soup.find("input", {"name": "authenticity_token"})
            if not token:
                raise ValueError("Geen CSRF-token gevonden.")

            r2 = self.session.post(LOGIN_URL, data={
                "authenticity_token": token["value"],
                "user[email]":        email,
                "user[password]":     password,
                "user[remember_me]":  "1",
                "commit":             "Inloggen",
            }, timeout=15)
            r2.raise_for_status()

            if "/login" in r2.url:
                # Still on login page — credentials were rejected
                soup2 = BeautifulSoup(r2.text, "html.parser")
                err   = soup2.find(class_=lambda c: c and "error" in c.lower())
                msg   = err.get_text(strip=True) if err else "Inloggen mislukt. Controleer uw gegevens."
                self.root.after(0, lambda: self._login_failed(msg))
            else:
                if self.remember_var.get():
                    self._save_credentials(email, password)
                else:
                    self._clear_credentials()
                self.root.after(0, self._login_ok)

        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda: self._login_failed(msg))

    def _login_failed(self, msg):
        self.status_var.set(msg)
        self.login_btn.config(state="normal", text="Inloggen")

    def _login_ok(self):
        self.root.unbind("<Return>")
        self._login_outer.destroy()
        self._show_spinner("Kinderen laden…")
        threading.Thread(target=self._fetch_children, daemon=True).start()

    # ── Shared loading spinner ────────────────────────────────────────────────

    def _show_spinner(self, msg):
        """Replace current content with a centred status label. Stored as self._spinner."""
        self._spinner = ttk.Frame(self.root)
        self._spinner.pack(expand=True, fill="both")
        self._spinner.columnconfigure(0, weight=1)
        self._spinner.rowconfigure(0, weight=1)
        ttk.Label(self._spinner, text=msg, style="Sub.TLabel").grid(row=0, column=0)

    # ── Child selector ────────────────────────────────────────────────────────

    def _fetch_children(self):
        """
        Parse /ouders/accounts for <a href="/ouders/children/NNN"> links.
        Fetches each child's profile thumbnail as a PIL image (converted to
        ImageTk on the main thread in _show_child_selector).
        """
        try:
            r    = self.session.get(ACCOUNTS_URL, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            children = []
            seen     = set()
            for a in soup.find_all("a", href=re.compile(r"^/ouders/children/\d+$")):
                child_id = a["href"].split("/")[-1]
                if child_id in seen:
                    continue
                seen.add(child_id)

                img_tag = a.find("img", class_="thumbnail")
                name    = img_tag.get("alt", f"Kind {child_id}") if img_tag else f"Kind {child_id}"

                pil_img = None
                if img_tag and img_tag.get("src"):
                    src = img_tag["src"]
                    if not src.startswith("http"):
                        src = BASE_URL + src
                    try:
                        ri      = self.session.get(src, timeout=10)
                        pil_img = Image.open(io.BytesIO(ri.content))
                        pil_img.thumbnail((80, 80), Image.LANCZOS)
                    except Exception:
                        pass

                children.append({"id": child_id, "name": name, "pil_img": pil_img})

            self.root.after(0, lambda: self._on_children_loaded(children))

        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda: self._on_children_error(msg))

    def _on_children_loaded(self, children):
        self._spinner.destroy()
        self._children = children
        if len(children) == 0:
            self._on_children_error("Geen kinderen gevonden in het account.")
        else:
            self._show_child_selector(children)

    def _on_children_error(self, msg):
        try:
            self._spinner.destroy()
        except Exception:
            pass
        err_frame = ttk.Frame(self.root)
        err_frame.pack(expand=True, fill="both")
        err_frame.columnconfigure(0, weight=1)
        err_frame.rowconfigure(0, weight=1)
        card = ttk.Frame(err_frame, style="Card.TFrame", padding=40)
        card.grid(row=0, column=0)
        ttk.Label(card, text="Fout bij laden",   style="Title.TLabel").pack(pady=(0, 8))
        ttk.Label(card, text=msg,                style="Err.TLabel"  ).pack(pady=(0, 20))
        ttk.Button(card, text="Opnieuw inloggen", style="Primary.TButton",
                   command=lambda: (err_frame.destroy(), self._setup_login_frame())
                   ).pack(fill="x")

    def _show_child_selector(self, children):
        outer = ttk.Frame(self.root)
        outer.pack(expand=True, fill="both")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        ncols = len(children)
        card  = ttk.Frame(outer, style="Card.TFrame", padding=40)
        card.grid(row=0, column=0)

        ttk.Label(card, text="Jaamo",         style="Title.TLabel").grid(row=0, column=0, columnspan=ncols, pady=(0, 4))
        ttk.Label(card, text="Kies een kind", style="Sub.TLabel"  ).grid(row=1, column=0, columnspan=ncols, pady=(0, 28))

        for col, child in enumerate(children):
            child_card = ttk.Frame(card, style="Card.TFrame", padding=12)
            child_card.grid(row=2, column=col, padx=20)

            if child.get("pil_img"):
                # ImageTk.PhotoImage must be created on the main thread
                photo = ImageTk.PhotoImage(child["pil_img"])
                self._photo_refs.append(photo)
                tk.Label(child_card, image=photo, bg=CARD).pack(pady=(0, 8))

            ttk.Label(child_card, text=child["name"], style="Card.TLabel").pack(pady=(0, 10))

            btn_row = ttk.Frame(child_card, style="Card.TFrame")
            btn_row.pack(fill="x")
            ttk.Button(btn_row, text="Foto's", style="Primary.TButton",
                       command=lambda c=child, o=outer: (o.destroy(), self._start_gallery(c["id"], c["name"]))
                       ).pack(side="left", fill="x", expand=True, padx=(0, 4))
            ttk.Button(btn_row, text="Dagboek", style="Ghost.TButton",
                       command=lambda c=child, o=outer: (o.destroy(), self._start_diary_mode(c["id"], c["name"]))
                       ).pack(side="left", fill="x", expand=True)

    def _start_gallery(self, child_id, child_name):
        self._child_id   = child_id
        self._photos_url = f"{BASE_URL}/ouders/children/{child_id}/photos"
        self._child_name = child_name
        self._setup_gallery_frame()
        self._load_photos()

    # ── Gallery frame ─────────────────────────────────────────────────────────

    def _setup_gallery_frame(self):
        """
        Build the gallery screen. Everything is packed inside self._gallery_frame
        so _back_to_child_selector() can tear it all down with a single destroy().
        """
        self._gallery_frame = ttk.Frame(self.root)
        self._gallery_frame.pack(fill="both", expand=True)

        # ── Toolbar ───────────────────────────────────────────────────────────
        bar = ttk.Frame(self._gallery_frame, style="Card.TFrame", padding=(16, 10))
        bar.pack(fill="x")

        ttk.Button(bar, text="← Terug", style="Ghost.TButton",
                   command=self._back_to_child_selector).pack(side="left", padx=(0, 12))

        ttk.Label(bar, text=f"Foto's — {self._child_name}", style="BarTitle.TLabel").pack(side="left")

        # Right side, packed right-to-left
        self.dl_sel_btn = ttk.Button(bar, text="Download selectie",
                                     style="Warn.TButton",
                                     command=self._download_selected, state="disabled")
        self.dl_sel_btn.pack(side="right", padx=(6, 0))

        self.sel_all_btn = ttk.Button(bar, text="Selecteer alles",
                                      style="Ghost.TButton",
                                      command=self._select_all, state="disabled")
        self.sel_all_btn.pack(side="right", padx=(6, 0))

        ttk.Button(bar, text="Vernieuwen", style="Primary.TButton",
                   command=self._load_photos).pack(side="right", padx=(6, 0))

        self.progress_var  = tk.StringVar(value="Foto's laden…")
        self.selection_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.selection_var, style="Bar.TLabel").pack(side="right", padx=(12, 0))
        ttk.Label(bar, textvariable=self.progress_var,  style="Bar.TLabel").pack(side="right", padx=12)

        # ── Separator ─────────────────────────────────────────────────────────
        tk.Frame(self._gallery_frame, height=1, bg=BORDER).pack(fill="x")

        # ── Scrollable photo area ─────────────────────────────────────────────
        container = ttk.Frame(self._gallery_frame)
        container.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        scrollbar   = ttk.Scrollbar(container, orient="vertical",
                                    style="Thin.Vertical.TScrollbar",
                                    command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y", padx=(0, 2))
        self.canvas.pack(side="left", fill="both", expand=True)

        self.scroll_frame = tk.Frame(self.canvas, bg=BG)
        self._canvas_win  = self.canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")

        # Keep scroll region and canvas window width in sync with resizing
        self.scroll_frame.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(
            self._canvas_win, width=e.width))

        # ── Mouse wheel ───────────────────────────────────────────────────────
        # macOS reports delta as small integers (3–6); Windows/Linux use multiples of 120.
        if platform.system() == "Darwin":
            self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(-e.delta, "units"))
        else:
            self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))
            self.canvas.bind_all("<Button-4>",   lambda e: self.canvas.yview_scroll(-1, "units"))  # Linux scroll up
            self.canvas.bind_all("<Button-5>",   lambda e: self.canvas.yview_scroll(1,  "units"))  # Linux scroll down

    # Bounding box of the Netherlands (mainland + islands)
    _NL_LAT_MIN, _NL_LAT_MAX = 50.75, 53.55
    _NL_LON_MIN, _NL_LON_MAX =  3.35,  7.25

    def _show_location_dialog(self):
        """Modal dialog to view and update the GPS coordinates saved into each photo's EXIF."""
        dlg = tk.Toplevel(self.root)
        dlg.title("GPS Location")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        frame = ttk.Frame(dlg, style="Card.TFrame", padding=28)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="GPS Location", style="BarTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(frame, text="Wordt opgeslagen in EXIF van elke gedownloade foto.",
                  style="Sub.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 20))

        ttk.Label(frame, text="Breedtegraad (lat)", style="Card.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(0, 4))
        lat_var = tk.StringVar(value=str(self._gps_lat))
        ttk.Entry(frame, textvariable=lat_var, width=32).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(0, 12))

        ttk.Label(frame, text="Lengtegraad (lon)", style="Card.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 4))
        lon_var = tk.StringVar(value=str(self._gps_lon))
        ttk.Entry(frame, textvariable=lon_var, width=32).grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=(0, 16))

        err_var = tk.StringVar()
        ttk.Label(frame, textvariable=err_var, style="Err.TLabel").grid(
            row=6, column=0, columnspan=2, pady=(0, 12))

        def _save():
            try:
                lat = float(lat_var.get().strip().replace(",", "."))
                lon = float(lon_var.get().strip().replace(",", "."))
            except ValueError:
                err_var.set("Vul geldige coördinaten in (bijv. 52.3676, 4.9041).")
                return
            if not (self._NL_LAT_MIN <= lat <= self._NL_LAT_MAX):
                err_var.set(f"Breedtegraad moet tussen {self._NL_LAT_MIN} en {self._NL_LAT_MAX} liggen (Nederland).")
                return
            if not (self._NL_LON_MIN <= lon <= self._NL_LON_MAX):
                err_var.set(f"Lengtegraad moet tussen {self._NL_LON_MIN} en {self._NL_LON_MAX} liggen (Nederland).")
                return
            self._gps_lat = lat
            self._gps_lon = lon
            self._save_settings()
            dlg.destroy()

        btn_frame = ttk.Frame(frame, style="Card.TFrame")
        btn_frame.grid(row=7, column=0, columnspan=2, sticky="ew")
        ttk.Button(btn_frame, text="Annuleren", style="Ghost.TButton",
                   command=dlg.destroy).pack(side="left")
        ttk.Button(btn_frame, text="Opslaan", style="Primary.TButton",
                   command=_save).pack(side="right")

        dlg.bind("<Return>", lambda _: _save())
        dlg.bind("<Escape>", lambda _: dlg.destroy())

        # Centre the dialog over the main window
        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width()  - dlg.winfo_width())  // 2
        y = self.root.winfo_y() + (self.root.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")

    def _back_to_child_selector(self):
        self._gallery_frame.destroy()
        self._show_child_selector(self._children)

    # ── Diary frame ───────────────────────────────────────────────────────────

    def _start_diary_mode(self, child_id, child_name):
        self._child_id   = child_id
        self._child_name = child_name
        self._setup_diary_frame()

    def _setup_diary_frame(self):
        self._diary_frame   = ttk.Frame(self.root)
        self._diary_frame.pack(fill="both", expand=True)
        self._diary_entries = []
        self._diary_total   = 0

        # ── Toolbar ───────────────────────────────────────────────────────────
        bar = ttk.Frame(self._diary_frame, style="Card.TFrame", padding=(16, 10))
        bar.pack(fill="x")
        ttk.Button(bar, text="← Terug", style="Ghost.TButton",
                   command=self._back_from_diary).pack(side="left", padx=(0, 12))
        ttk.Label(bar, text=f"Dagboek — {self._child_name}",
                  style="BarTitle.TLabel").pack(side="left")
        self._diary_dl_btn = ttk.Button(bar, text="Download dagboek",
                                        style="Primary.TButton",
                                        command=self._trigger_diary_download,
                                        state="disabled")
        self._diary_dl_btn.pack(side="right", padx=(6, 0))
        self._diary_progress_var = tk.StringVar(value="Laden…")
        ttk.Label(bar, textvariable=self._diary_progress_var,
                  style="Bar.TLabel").pack(side="right", padx=12)

        # Thin progress bar — fills as stories load, disappears when done
        self._diary_pbar_var = tk.DoubleVar(value=0)
        self._diary_pbar = ttk.Progressbar(
            self._diary_frame, mode="determinate",
            variable=self._diary_pbar_var,
            style="Diary.Horizontal.TProgressbar")
        self._diary_pbar.pack(fill="x")

        tk.Frame(self._diary_frame, height=1, bg=BORDER).pack(fill="x")

        # ── Scrollable body ───────────────────────────────────────────────────
        container = ttk.Frame(self._diary_frame)
        container.pack(fill="both", expand=True)

        self._diary_canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical",
                                  style="Thin.Vertical.TScrollbar",
                                  command=self._diary_canvas.yview)
        self._diary_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y", padx=(0, 2))
        self._diary_canvas.pack(side="left", fill="both", expand=True)

        self._diary_scroll = ttk.Frame(self._diary_canvas)
        self._diary_win    = self._diary_canvas.create_window(
            (0, 0), window=self._diary_scroll, anchor="nw")

        self._diary_scroll.bind("<Configure>", lambda e: self._diary_canvas.configure(
            scrollregion=self._diary_canvas.bbox("all")))
        self._diary_canvas.bind("<Configure>", lambda e: self._diary_canvas.itemconfig(
            self._diary_win, width=e.width))

        if platform.system() == "Darwin":
            self._diary_canvas.bind_all(
                "<MouseWheel>", lambda e: self._diary_canvas.yview_scroll(-e.delta, "units"))
        else:
            self._diary_canvas.bind_all(
                "<MouseWheel>", lambda e: self._diary_canvas.yview_scroll(int(-e.delta / 120), "units"))
            self._diary_canvas.bind_all(
                "<Button-4>", lambda e: self._diary_canvas.yview_scroll(-1, "units"))
            self._diary_canvas.bind_all(
                "<Button-5>", lambda e: self._diary_canvas.yview_scroll(1,  "units"))

        threading.Thread(target=self._fetch_diary_entries, daemon=True).start()

    def _back_from_diary(self):
        self._diary_frame.destroy()
        self._show_child_selector(self._children)

    def _trigger_diary_download(self):
        first_name = self._child_name.split()[0]
        path = filedialog.asksaveasfilename(
            title="Dagboek opslaan als…",
            defaultextension=".html",
            filetypes=[("HTML bestand", "*.html"), ("Alle bestanden", "*.*")],
            initialfile=f"{first_name}_dagboek.html",
        )
        if not path:
            return
        html = self._build_diary_html(self._diary_entries)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        messagebox.showinfo("Dagboek opgeslagen",
                            f"{len(self._diary_entries)} berichten opgeslagen in:\n{path}")

    # ── Photo loading ─────────────────────────────────────────────────────────

    def _load_photos(self):
        """Reset gallery state and kick off a fresh fetch."""
        self._photo_refs.clear()
        self._groups.clear()
        self._selected.clear()
        self._cells.clear()
        for w in self.scroll_frame.winfo_children():
            w.destroy()
        self.dl_sel_btn.config(state="disabled", text="Download selectie")
        self.sel_all_btn.config(state="disabled")
        self.selection_var.set("")
        self.progress_var.set("Foto's laden…")
        threading.Thread(target=self._fetch_photos, daemon=True).start()

    def _fetch_photos(self):
        """
        Fetch the photos page and parse date groups + photo entries.

        HTML structure inside <div class="image_gallery">:
          <div class="col-12 font_semi_bold">29 jun 2026</div>   ← date header
          <div class="image_canvas …">…</div>                    ← photo entry
          …

        The two element types are siblings, so we walk them in order and track
        the current date group.
        """
        try:
            r    = self.session.get(self._photos_url, timeout=20)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")

            gallery = soup.find("div", class_="image_gallery")
            if not gallery:
                self.root.after(0, lambda: self.progress_var.set("Geen fotogalerij gevonden."))
                return

            groups          = []
            current_date    = "Onbekende datum"
            current_entries = []

            for node in gallery.children:
                # BeautifulSoup yields NavigableString nodes (whitespace) alongside Tag
                # nodes — skip anything without .get() (i.e. non-Tag nodes).
                if not hasattr(node, "get"):
                    continue

                classes = node.get("class", [])

                if "col-12" in classes and "font_semi_bold" in classes:
                    # Date header — flush the previous group and start a new one
                    if current_entries:
                        groups.append({"date": current_date, "entries": current_entries})
                    current_date    = node.get_text(strip=True)
                    current_entries = []

                elif "image_canvas" in classes:
                    div  = node.find("div", class_="image_container")
                    if not div:
                        continue
                    full = div.get("data-src", "")
                    if not full or not full.startswith("http"):
                        continue

                    img_tag = div.find("img", attrs={"data-original": True})
                    thumb   = img_tag["data-original"] if img_tag else full

                    # Caption lives in a sibling div whose id is referenced by data-sub-html
                    caption = ""
                    sub_id  = div.get("data-sub-html", "").lstrip("#")
                    if sub_id:
                        cap_div = node.find("div", {"id": sub_id})
                        if cap_div:
                            caption = cap_div.get_text(separator="\n", strip=True)

                    current_entries.append({"thumb": thumb, "full": full, "caption": caption})

            if current_entries:
                groups.append({"date": current_date, "entries": current_entries})

            total        = sum(len(g["entries"]) for g in groups)
            self._groups = groups
            self.root.after(0, lambda: self._build_gallery(groups, total))

        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda: self.progress_var.set(f"Fout: {msg}"))

    def _build_gallery(self, groups, total):
        """Render date group headers and photo grids; spawn one thread per thumbnail."""
        if not groups:
            self.progress_var.set("Geen foto's gevonden.")
            return

        self._loaded_count = 0
        self._total_count  = total
        self.progress_var.set(f"0 / {total} geladen")

        for group in groups:
            # ── Date group header ─────────────────────────────────────────────
            header = ttk.Frame(self.scroll_frame, style="DateBar.TFrame", padding=(12, 8))
            header.pack(fill="x", padx=12, pady=(14, 4))

            ttk.Label(header,
                      text=f"{group['date']}  —  {len(group['entries'])} foto's",
                      style="Date.TLabel").pack(side="left")

            entries  = group["entries"]
            date_str = group["date"]

            ttk.Button(header, text="Download dag", style="Date.TButton",
                       command=lambda e=entries, d=date_str: self._download_date(e, d)
                       ).pack(side="right", padx=(6, 0))

            sel_btn = ttk.Button(header, text="Selecteer dag", style="Ghost.TButton",
                                 command=lambda g=group: self._toggle_select_day(g))
            sel_btn.pack(side="right")
            group["sel_btn"] = sel_btn

            # ── Photo grid ────────────────────────────────────────────────────
            grid = tk.Frame(self.scroll_frame, bg=BG)
            grid.pack(fill="x", padx=12, pady=(0, 4))
            group["frame"] = grid

            for idx, entry in enumerate(entries):
                threading.Thread(
                    target=self._load_thumb,
                    args=(entry["thumb"], entry["full"], group, idx),
                    daemon=True,
                ).start()

        self.sel_all_btn.config(state="normal" if total > 0 else "disabled")

    def _load_thumb(self, thumb_url, full_url, group, slot):
        """Download (or load from cache) one thumbnail; schedule UI placement."""
        try:
            cache = _thumb_cache_path(thumb_url, THUMB_CACHE_DIR)
            if os.path.exists(cache):
                data = open(cache, "rb").read()
            else:
                r    = self.session.get(thumb_url, timeout=20)
                r.raise_for_status()
                data = r.content
                open(cache, "wb").write(data)
            img = Image.open(io.BytesIO(data))
            img.thumbnail(THUMB_SIZE, Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self.root.after(0, lambda p=photo, u=full_url, g=group, s=slot:
                            self._place_thumb(p, u, g, s))
        except Exception:
            self.root.after(0, self._thumb_loaded)   # still count failed loads

    def _place_thumb(self, photo, url, group, slot):
        """Place one thumbnail cell in the grid and wire up click-to-select."""
        # Keep a reference — Tkinter drops PhotoImage objects that go out of scope.
        self._photo_refs.append(photo)

        row, col = divmod(slot, COLS)
        cell = tk.Frame(group["frame"], bg=CARD, padx=4, pady=4,
                        highlightbackground=BORDER, highlightthickness=1)
        cell.grid(row=row, column=col, padx=6, pady=6, sticky="nw")
        self._cells[url] = cell

        # One handler bound to every widget inside the cell
        toggle = lambda e, u=url, g=group: self._toggle_select(u, g)

        lbl = tk.Label(cell, image=photo, cursor="hand2", bg=CARD)
        lbl.pack()
        lbl.bind("<Button-1>", toggle)
        cell.bind("<Button-1>", toggle)

        hint = tk.Label(cell, text="Klik om te selecteren",
                        fg=MUTED, bg=CARD, cursor="hand2", font=("Helvetica", 8))
        hint.pack(pady=(3, 0))
        hint.bind("<Button-1>", toggle)

        caption = group["entries"][slot].get("caption", "")
        if caption:
            line    = caption.split("\n")[0]
            text    = (line[:40] + "…") if len(line) > 40 else line
            cap_lbl = tk.Label(cell, text=text, fg=MUTED, bg=CARD,
                               font=("Helvetica", 8), wraplength=THUMB_SIZE[0])
            cap_lbl.pack(pady=(1, 0))
            cap_lbl.bind("<Button-1>", toggle)

        self._thumb_loaded()

    def _thumb_loaded(self):
        self._loaded_count += 1
        self.progress_var.set(f"{self._loaded_count} / {self._total_count} geladen")

    # ── Selection ─────────────────────────────────────────────────────────────

    def _set_cell_selected(self, url, selected):
        """Update the highlight border of a single photo cell."""
        cell = self._cells.get(url)
        if cell:
            if selected:
                cell.config(highlightbackground=PRIMARY, highlightthickness=3)
            else:
                cell.config(highlightbackground=BORDER,  highlightthickness=1)

    def _toggle_select(self, url, group):
        selected = url not in self._selected
        if selected:
            self._selected.add(url)
        else:
            self._selected.discard(url)
        self._set_cell_selected(url, selected)
        self._update_selection_bar()
        self._update_day_btn(group)

    def _toggle_select_day(self, group):
        """Select all photos in a group, or deselect them if already all selected."""
        urls     = {e["full"] for e in group["entries"]}
        selected = not urls.issubset(self._selected)
        if selected:
            self._selected |= urls
        else:
            self._selected -= urls
        for url in urls:
            self._set_cell_selected(url, selected)
        self._update_selection_bar()
        self._update_day_btn(group)

    def _select_all(self):
        for group in self._groups:
            for entry in group["entries"]:
                self._selected.add(entry["full"])
                self._set_cell_selected(entry["full"], True)
            self._update_day_btn(group)
        self._update_selection_bar()

    def _update_day_btn(self, group):
        """Flip the 'Selecteer dag' button label based on current selection state."""
        btn = group.get("sel_btn")
        if btn:
            urls    = {e["full"] for e in group["entries"]}
            all_sel = bool(urls) and urls.issubset(self._selected)
            btn.config(text="Deselecteer dag" if all_sel else "Selecteer dag")

    def _update_selection_bar(self):
        n = len(self._selected)
        if n == 0:
            self.selection_var.set("")
            self.dl_sel_btn.config(state="disabled", text="Download selectie")
        else:
            self.selection_var.set(f"{n} geselecteerd  |")
            self.dl_sel_btn.config(state="normal", text=f"Download selectie ({n})")

    # ── Download ──────────────────────────────────────────────────────────────

    def _download_selected(self):
        """Ask for a folder and download all selected photos preserving date order."""
        pairs = [
            (entry, group["date"])
            for group in self._groups
            for entry in group["entries"]
            if entry["full"] in self._selected
        ]
        folder = filedialog.askdirectory(title=f"Map voor {len(pairs)} geselecteerde foto's")
        if not folder:
            return
        self.dl_sel_btn.config(state="disabled", text="Bezig…")
        threading.Thread(target=self._save_photos, args=(pairs, folder), daemon=True).start()

    def _download_date(self, entries, date_label):
        """Ask for a folder and download all photos for a single date."""
        folder = filedialog.askdirectory(title=f"Map voor foto's van {date_label}")
        if not folder:
            return
        self.progress_var.set(f"Downloaden: {date_label}…")
        pairs = [(e, date_label) for e in entries]
        threading.Thread(target=self._save_photos, args=(pairs, folder), daemon=True).start()

    def _save_photos(self, pairs, folder):
        """
        Worker thread: download each (entry, date_str) pair, inject EXIF, and write to disk.
        S3 pre-signed URLs expire after 10 minutes — if 403 errors occur, the user
        should click Vernieuwen to re-fetch fresh URLs before downloading.
        """
        total      = len(pairs)
        errors     = 0
        first_name = self._child_name.split()[0] if self._child_name else None

        for i, (entry, date_str) in enumerate(pairs):
            url = entry["full"]
            try:
                r = self.session.get(url, timeout=30)
                r.raise_for_status()
                dest = _unique_path(folder, _build_filename(url, i, date_str, first_name))
                data = _apply_metadata(r.content, date_str, entry.get("caption"),
                                      self._gps_lat, self._gps_lon)
                with open(dest, "wb") as f:
                    f.write(data)
                _set_file_time(dest, date_str)
                self.root.after(0, lambda n=i + 1: self.progress_var.set(f"{n} / {total} opgeslagen"))
            except Exception:
                errors += 1

        def _done():
            msg = f"{total - errors} foto's opgeslagen in:\n{folder}"
            if errors:
                msg += f"\n({errors} mislukt)"
            messagebox.showinfo("Klaar", msg)
            self.progress_var.set(f"{self._total_count} foto's geladen")
            self._update_selection_bar()

        self.root.after(0, _done)

    # ── Diary ─────────────────────────────────────────────────────────────────

    def _fetch_diary_entries(self):
        """Worker thread: fetch every story page and hand results to the main thread."""
        try:
            stories_url = f"{BASE_URL}/ouders/children/{self._child_id}/stories"
            soup = BeautifulSoup(self.session.get(stories_url, timeout=15).text, "html.parser")

            seen, story_ids = set(), []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if f"/children/{self._child_id}/stories/" in href:
                    sid = href.rstrip("/").rsplit("/", 1)[-1]
                    if sid.isdigit() and sid not in seen:
                        seen.add(sid)
                        story_ids.append(sid)

            total = len(story_ids)
            self._diary_total = total

            for i, sid in enumerate(story_ids):
                cache_path = _story_cache_path(self._child_id, sid)
                cached = _load_story_cache(cache_path)
                if cached is not None:
                    self.root.after(0, lambda e=cached, n=i + 1, t=total:
                                    self._add_diary_card(e, n, t))
                    continue

                self.root.after(0, lambda n=i + 1, t=total:
                                self._diary_progress_var.set(f"Laden: {n}/{t}…"))
                try:
                    soup2 = BeautifulSoup(
                        self.session.get(
                            f"{BASE_URL}/ouders/children/{self._child_id}/stories/{sid}",
                            timeout=15).text,
                        "html.parser")

                    h1   = soup2.find("h1")
                    date = h1.get_text(" ", strip=True) if h1 else ""

                    time_div = soup2.find(
                        "div", class_=lambda c: c and "font_semi_bold" in c and "text_dark_grey" in c)
                    time_str = time_div.get_text(strip=True) if time_div else ""

                    text_div = soup2.find(
                        "div", class_=lambda c: c and "font_small" in c and "text_dark_grey" in c)
                    paras: list[str] = []
                    if text_div:
                        seen_p: set[str] = set()
                        for p in text_div.find_all("p"):
                            t = p.get_text(strip=True)
                            if t and t not in seen_p:
                                seen_p.add(t)
                                paras.append(t)

                    images: list[dict] = []
                    for img_div in soup2.find_all("div", class_="image_container"):
                        full = img_div.get("data-src", "")
                        if not full or not full.startswith("http"):
                            continue
                        img_tag   = img_div.find("img", attrs={"data-original": True})
                        thumb     = img_tag["data-original"] if img_tag else full
                        if thumb and thumb.startswith("http"):
                            images.append({"thumb": thumb, "full": full})

                    if date or paras or images:
                        entry = {"date": date, "time": time_str,
                                 "paras": paras, "images": images}
                        _save_story_cache(cache_path, entry)
                        self.root.after(0, lambda e=entry, n=i + 1, t=total:
                                        self._add_diary_card(e, n, t))
                except Exception:
                    pass

            self.root.after(0, self._on_diary_loaded)

        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda: self._diary_progress_var.set(f"Fout: {msg}"))

    def _add_diary_card(self, entry, n, total):
        """Main thread: append one entry and render its card immediately."""
        self._diary_entries.append(entry)
        self._diary_pbar_var.set(n / total * 100 if total else 0)

        card = ttk.Frame(self._diary_scroll, style="Card.TFrame", padding=(16, 12))
        card.pack(fill="x", padx=16, pady=(8, 0))

        # Date + time on one row
        hdr = ttk.Frame(card, style="Card.TFrame")
        hdr.pack(fill="x", pady=(0, 6))
        ttk.Label(hdr, text=entry["date"],
                  style="BarTitle.TLabel").pack(side="left")
        if entry["time"]:
            ttk.Label(hdr, text=entry["time"],
                      style="Bar.TLabel").pack(side="right")

        # Separator line under header
        tk.Frame(card, height=1, bg=BORDER).pack(fill="x", pady=(0, 8))

        # Image thumbnails
        if entry.get("images"):
            img_frame = tk.Frame(card, bg=CARD)
            img_frame.pack(anchor="w", pady=(0, 8))
            for img_entry in entry["images"]:
                cell = tk.Frame(img_frame,
                                width=DIARY_THUMB_SIZE[0],
                                height=DIARY_THUMB_SIZE[1],
                                bg=BORDER)
                cell.pack_propagate(False)
                cell.pack(side="left", padx=(0, 6))
                lbl = tk.Label(cell, bg=BORDER)
                lbl.pack(fill="both", expand=True)
                threading.Thread(
                    target=self._load_diary_thumb,
                    args=(img_entry["thumb"], lbl),
                    daemon=True,
                ).start()

        # Text paragraphs
        for para in entry["paras"]:
            ttk.Label(card, text=para, wraplength=680,
                      justify="left", style="Card.TLabel").pack(anchor="w", pady=(0, 4))

    def _on_diary_loaded(self):
        """Main thread: called once all stories are fetched — finalise the UI."""
        self._diary_progress_var.set(f"{len(self._diary_entries)} berichten")
        self._diary_dl_btn.config(state="normal")
        self._diary_pbar.pack_forget()
        # Bottom padding so last card isn't flush against the edge
        tk.Frame(self._diary_scroll, height=16, bg=BG).pack()

    def _load_diary_thumb(self, url, label):
        """Worker thread: fetch (or load from cache) one diary thumbnail."""
        try:
            cache = _thumb_cache_path(url, DTHUMB_CACHE_DIR)
            if os.path.exists(cache):
                data = open(cache, "rb").read()
            else:
                r    = self.session.get(url, timeout=15)
                r.raise_for_status()
                data = r.content
                open(cache, "wb").write(data)
            pil_img = Image.open(io.BytesIO(data))
            pil_img.thumbnail(DIARY_THUMB_SIZE, Image.LANCZOS)
            def _place(pil=pil_img):
                photo = ImageTk.PhotoImage(pil)
                self._photo_refs.append(photo)
                label.config(image=photo, bg=CARD)
            self.root.after(0, _place)
        except Exception:
            pass

    def _build_diary_html(self, entries):
        """Build a self-contained HTML file from parsed diary entries."""
        first_name = self._child_name.split()[0]
        today      = datetime.date.today().strftime("%d-%m-%Y")

        cards = []
        for e in entries:
            time_html = (f'<div class="time">{e["time"]}</div>') if e["time"] else ""
            paras_html = "".join(f"<p>{p}</p>" for p in e["paras"])
            cards.append(
                f'<div class="entry">'
                f'<div class="date">{e["date"]}</div>'
                f'{time_html}'
                f'<div class="text">{paras_html}</div>'
                f'</div>'
            )

        return f"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8"/>
  <title>Dagboek van {first_name}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #F0F4F8; color: #1A202C;
      max-width: 760px; margin: 0 auto; padding: 32px 16px;
    }}
    h1   {{ color: #2B6CB0; font-size: 1.8rem; margin-bottom: 4px; }}
    .sub {{ color: #718096; font-size: .88rem; margin-bottom: 36px; }}
    .entry {{
      background: #fff; border: 1px solid #CBD5E0; border-radius: 10px;
      padding: 18px 22px; margin-bottom: 14px;
    }}
    .date {{ font-weight: 700; color: #2B6CB0; font-size: 1rem; margin-bottom: 2px; }}
    .time {{ color: #718096; font-size: .82rem; margin-bottom: 10px; }}
    .text p {{ line-height: 1.65; margin-bottom: 8px; }}
    .text p:last-child {{ margin-bottom: 0; }}
  </style>
</head>
<body>
  <h1>Dagboek van {first_name}</h1>
  <p class="sub">Gedownload op {today} &middot; {len(entries)} berichten</p>
  {"".join(cards)}
</body>
</html>"""


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    root.resizable(True, True)
    JaamoApp(root)
    root.mainloop()
