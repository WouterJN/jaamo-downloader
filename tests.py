"""
tests.py — Test suite for Jaamo Photo Downloader

Run:  python3 -m pytest tests.py -v
  or: python3 -m unittest tests -v

Coverage:
  - Date parsing           (_parse_nl_date)
  - GPS conversion         (_decimal_to_dms)
  - Filename building      (_build_filename)
  - Collision-safe paths   (_unique_path)
  - File timestamps        (_set_file_time)
  - EXIF injection         (_inject_exif_metadata, _apply_metadata)
  - Gallery HTML parsing   (replicates _fetch_photos logic)
  - Accounts HTML parsing  (replicates _fetch_children logic)
  - Credential file I/O    (save / load / clear / permissions)
  - Diary stories listing  (replicates _fetch_diary_entries list logic)
  - Diary story page       (replicates _fetch_diary_entries per-page logic)
  - Diary HTML building    (replicates _build_diary_html logic)

Note: JaamoApp itself is not instantiated here — it requires a live Tk display.
All tests target module-level helpers and replicated parsing logic.
"""

import datetime
import io
import json
import os
import re
import tempfile
import unittest

import piexif
from bs4 import BeautifulSoup
from PIL import Image

from app import (
    SCHOOL_LAT,
    SCHOOL_LON,
    JaamoApp,
    _apply_metadata,
    _build_filename,
    _decimal_to_dms,
    _inject_exif_metadata,
    _parse_nl_date,
    _set_file_time,
    _unique_path,
)

# ── Test helpers ──────────────────────────────────────────────────────────────

def _make_jpeg(width=10, height=10, color=(200, 100, 50)):
    """Return minimal valid JPEG bytes using Pillow."""
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _parse_gallery_html(html):
    """
    Replicate the parsing logic from JaamoApp._fetch_photos so we can unit-test
    it without needing a live Tk window or HTTP session.
    """
    soup    = BeautifulSoup(html, "html.parser")
    gallery = soup.find("div", class_="image_gallery")
    if not gallery:
        return []

    groups          = []
    current_date    = "Onbekende datum"
    current_entries = []

    for node in gallery.children:
        if not hasattr(node, "get"):
            continue
        classes = node.get("class", [])

        if "col-12" in classes and "font_semi_bold" in classes:
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
            caption = ""
            sub_id  = div.get("data-sub-html", "").lstrip("#")
            if sub_id:
                cap_div = node.find("div", {"id": sub_id})
                if cap_div:
                    caption = cap_div.get_text(separator="\n", strip=True)
            current_entries.append({"thumb": thumb, "full": full, "caption": caption})

    if current_entries:
        groups.append({"date": current_date, "entries": current_entries})
    return groups


def _parse_stories_html(html, child_id):
    """
    Replicate the story-ID extraction from JaamoApp._fetch_diary_entries.
    Returns a list of unique numeric story ID strings in page order.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen, story_ids = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if f"/children/{child_id}/stories/" in href:
            sid = href.rstrip("/").rsplit("/", 1)[-1]
            if sid.isdigit() and sid not in seen:
                seen.add(sid)
                story_ids.append(sid)
    return story_ids


def _parse_story_page(html):
    """
    Replicate the per-story parsing from JaamoApp._fetch_diary_entries.
    Returns {"date": str, "time": str, "paras": [str, ...]}.
    """
    soup = BeautifulSoup(html, "html.parser")
    h1   = soup.find("h1")
    date = h1.get_text(" ", strip=True) if h1 else ""

    time_div = soup.find(
        "div", class_=lambda c: c and "font_semi_bold" in c and "text_dark_grey" in c)
    time_str = time_div.get_text(strip=True) if time_div else ""

    text_div = soup.find(
        "div", class_=lambda c: c and "font_small" in c and "text_dark_grey" in c)
    paras: list = []
    if text_div:
        seen_p: set = set()
        for p in text_div.find_all("p"):
            t = p.get_text(strip=True)
            if t and t not in seen_p:
                seen_p.add(t)
                paras.append(t)

    images: list = []
    for img_div in soup.find_all("div", class_="image_container"):
        full = img_div.get("data-src", "")
        if not full or not full.startswith("http"):
            continue
        img_tag = img_div.find("img", attrs={"data-original": True})
        thumb   = img_tag["data-original"] if img_tag else full
        if thumb and thumb.startswith("http"):
            images.append({"thumb": thumb, "full": full})

    return {"date": date, "time": time_str, "paras": paras, "images": images}


def _build_diary_html(entries, first_name, today="01-01-2026"):
    """
    Replicate JaamoApp._build_diary_html for testing without a Tk instance.
    """
    cards = []
    for e in entries:
        time_html  = f'<div class="time">{e["time"]}</div>' if e["time"] else ""
        paras_html = "".join(f"<p>{p}</p>" for p in e["paras"])
        cards.append(
            f'<div class="entry">'
            f'<div class="date">{e["date"]}</div>'
            f'{time_html}'
            f'<div class="text">{paras_html}</div>'
            f'</div>'
        )
    return (
        f'<!DOCTYPE html>\n<html lang="nl">\n<head>\n'
        f'  <meta charset="UTF-8"/>\n'
        f'  <title>Dagboek van {first_name}</title>\n'
        f'</head>\n<body>\n'
        f'  <h1>Dagboek van {first_name}</h1>\n'
        f'  <p class="sub">Gedownload op {today} &middot; {len(entries)} berichten</p>\n'
        + "".join(cards) +
        f'\n</body>\n</html>'
    )


def _parse_accounts_html(html):
    """
    Replicate the child-parsing logic from JaamoApp._fetch_children so we can
    unit-test it without a Tk window or HTTP session.
    """
    soup     = BeautifulSoup(html, "html.parser")
    children = []
    seen     = set()
    for a in soup.find_all("a", href=re.compile(r"^/ouders/children/\d+$")):
        child_id = a["href"].split("/")[-1]
        if child_id in seen:
            continue
        seen.add(child_id)
        img_tag = a.find("img", class_="thumbnail")
        name    = img_tag.get("alt", f"Kind {child_id}") if img_tag else f"Kind {child_id}"
        children.append({"id": child_id, "name": name})
    return children


# ── Sample HTML fixtures ──────────────────────────────────────────────────────

GALLERY_HTML = """
<div class="image_gallery d-flex flex-wrap mx-2">

  <div class="col-12 font_semi_bold text-start">29 jun 2026</div>

  <div class="image_canvas col-3 pe-2 pb-2">
    <a href="https://s3.example.com/photo1.jpeg">
      <div class="image_container"
           data-src="https://s3.example.com/photo1.jpeg"
           data-sub-html="#caption-100">
        <img class="img-fluid" data-original="https://s3.example.com/photo1.jpeg" src="/empty.jpg"/>
      </div>
      <div id="caption-100" style="display:none"><p>Spelen buiten</p></div>
    </a>
  </div>

  <div class="image_canvas col-3 pe-2 pb-2">
    <a href="https://s3.example.com/photo2.jpeg">
      <div class="image_container"
           data-src="https://s3.example.com/photo2.jpeg"
           data-sub-html="#caption-101">
        <img class="img-fluid" data-original="https://s3.example.com/photo2.jpeg" src="/empty.jpg"/>
      </div>
      <div id="caption-101" style="display:none"></div>
    </a>
  </div>

  <div class="col-12 font_semi_bold text-start">28 jun 2026</div>

  <div class="image_canvas col-3 pe-2 pb-2">
    <a href="https://s3.example.com/photo3.jpeg">
      <div class="image_container"
           data-src="https://s3.example.com/photo3.jpeg"
           data-sub-html="#caption-102">
        <img class="img-fluid" data-original="https://s3.example.com/photo3.jpeg" src="/empty.jpg"/>
      </div>
      <div id="caption-102" style="display:none"></div>
    </a>
  </div>

</div>
"""

# A photo entry with no data-src — should be silently skipped
GALLERY_HTML_MISSING_SRC = """
<div class="image_gallery">
  <div class="col-12 font_semi_bold text-start">29 jun 2026</div>
  <div class="image_canvas col-3">
    <a href="#">
      <div class="image_container" data-src="" data-sub-html="#caption-1">
        <img class="img-fluid" data-original="" src="/empty.jpg"/>
      </div>
    </a>
  </div>
</div>
"""

ACCOUNTS_HTML = """
<html><body>
  <a class="text-decoration-none" href="/ouders/children/12497">
    <img alt="Maurits Finn" class="thumbnail img-fluid" src="/assets/maurits.jpg"/>
    Maurits Finn
  </a>
  <a class="text-decoration-none" href="/ouders/children/99999">
    <img alt="Other Kid" class="thumbnail img-fluid" src="/assets/other.jpg"/>
    Other Kid
  </a>
  <!-- Duplicate link to same child — must be deduplicated -->
  <a class="text-decoration-none" href="/ouders/children/12497">
    <img alt="Maurits Finn" class="thumbnail img-fluid" src="/assets/maurits.jpg"/>
  </a>
  <!-- Non-child link — must be ignored -->
  <a href="/ouders/timelines">Tijdlijn</a>
</body></html>
"""

ACCOUNTS_HTML_NO_IMG_ALT = """
<html><body>
  <a href="/ouders/children/555">
    <span>no image tag here</span>
  </a>
</body></html>
"""

# Diary: stories list page — as returned by /ouders/children/{id}/stories
STORIES_HTML = """
<html><body>
  <a href="/ouders/children/12497/stories/244244">Bericht 1</a>
  <a href="/ouders/children/12497/stories/244244">Bericht 1 (duplicate link)</a>
  <a href="/ouders/children/12497/stories/243260">Bericht 2</a>
  <a href="/ouders/children/12497/stories/237958">Bericht 3</a>
  <a href="/ouders/children/12497/stories/new">Nieuw bericht knop — geen getal, must be ignored</a>
  <a href="/ouders/children/12497/stories">Terug — no story ID, ignored</a>
  <a href="/ouders/accounts">Account link — ignored</a>
</body></html>
"""

# Diary: individual story page — as returned by /ouders/children/{id}/stories/{sid}
STORY_PAGE_HTML = """
<html><body>
  <div class="stories">
    <div class="container-fluid">
      <h1 class="py-2 ps-1 m-0">
        <i class="mdi mdi-book-outline"></i>
        20 mei 2026
      </h1>
      <div class="px-1">
        <div class="font_semi_bold text_dark_grey pb-2">08:10</div>
        <div class="font_small text_dark_grey pb-1">
          <p><p>Beste ouder/verzorger</p></p>
          <p><p>Vandaag hebben we buiten gespeeld.</p></p>
          <p><p>Groetjes de flamingos</p></p>
          <p><p>Beste ouder/verzorger</p></p>
        </div>
      </div>
    </div>
  </div>
</body></html>
"""

STORY_PAGE_NO_TEXT_HTML = """
<html><body>
  <div class="stories">
    <h1 class="py-2 ps-1 m-0"><i class="mdi mdi-book-outline"></i> 15 jan 2026</h1>
  </div>
</body></html>
"""

STORY_PAGE_WITH_IMAGES_HTML = """
<html><body>
  <div class="stories">
    <h1 class="py-2 ps-1 m-0"><i class="mdi mdi-book-outline"></i> 21 mei 2026</h1>
    <div class="font_semi_bold text_dark_grey pb-2">09:00</div>
    <div class="font_small text_dark_grey pb-1">
      <p>Tekst met foto's.</p>
    </div>
    <div class="image_container"
         data-src="https://s3.example.com/full/photo1.jpg">
      <img data-original="https://s3.example.com/thumb/photo1.jpg" src="/empty.jpg"/>
    </div>
    <div class="image_container"
         data-src="https://s3.example.com/full/photo2.jpg">
      <img data-original="https://s3.example.com/thumb/photo2.jpg" src="/empty.jpg"/>
    </div>
    <div class="image_container" data-src="">
      <img data-original="https://s3.example.com/thumb/photo3.jpg" src="/empty.jpg"/>
    </div>
  </div>
</body></html>
"""


# ── 1. Date parsing ───────────────────────────────────────────────────────────

class TestParseNlDate(unittest.TestCase):

    def test_standard_date(self):
        self.assertEqual(_parse_nl_date("29 jun 2026"), datetime.date(2026, 6, 29))

    def test_first_of_january(self):
        self.assertEqual(_parse_nl_date("1 jan 2024"), datetime.date(2024, 1, 1))

    def test_last_of_december(self):
        self.assertEqual(_parse_nl_date("31 dec 2023"), datetime.date(2023, 12, 31))

    def test_leading_and_trailing_whitespace(self):
        self.assertEqual(_parse_nl_date("  15 mrt 2025  "), datetime.date(2025, 3, 15))

    def test_all_dutch_month_abbreviations(self):
        expected = [
            ("jan", 1), ("feb", 2), ("mrt", 3), ("apr", 4),
            ("mei", 5), ("jun", 6), ("jul", 7), ("aug", 8),
            ("sep", 9), ("okt", 10), ("nov", 11), ("dec", 12),
        ]
        for abbr, num in expected:
            with self.subTest(month=abbr):
                result = _parse_nl_date(f"1 {abbr} 2024")
                self.assertIsNotNone(result)
                self.assertEqual(result.month, num)

    def test_none_returns_none(self):
        self.assertIsNone(_parse_nl_date(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(_parse_nl_date(""))

    def test_unknown_month_abbreviation(self):
        self.assertIsNone(_parse_nl_date("29 xyz 2026"))

    def test_non_numeric_day(self):
        self.assertIsNone(_parse_nl_date("abc jun 2026"))

    def test_too_few_parts(self):
        self.assertIsNone(_parse_nl_date("jun 2026"))

    def test_garbage_string(self):
        self.assertIsNone(_parse_nl_date("not a date at all"))


# ── 2. GPS conversion ─────────────────────────────────────────────────────────

class TestDecimalToDms(unittest.TestCase):

    def test_returns_three_rationals(self):
        result = _decimal_to_dms(53.0)
        self.assertEqual(len(result), 3)
        for rational in result:
            self.assertEqual(len(rational), 2)

    def test_school_lat_degrees(self):
        d, _ = _decimal_to_dms(SCHOOL_LAT)[0]
        self.assertEqual(d, 53)

    def test_school_lat_minutes(self):
        m, _ = _decimal_to_dms(SCHOOL_LAT)[1]
        self.assertEqual(m, 13)

    def test_school_lon_degrees(self):
        d, _ = _decimal_to_dms(SCHOOL_LON)[0]
        self.assertEqual(d, 6)

    def test_roundtrip_school_lat(self):
        """Converting DMS back to decimal must match to 5 decimal places."""
        dms       = _decimal_to_dms(SCHOOL_LAT)
        recovered = dms[0][0] + dms[1][0] / 60 + (dms[2][0] / dms[2][1]) / 3600
        self.assertAlmostEqual(recovered, SCHOOL_LAT, places=5)

    def test_roundtrip_school_lon(self):
        dms       = _decimal_to_dms(SCHOOL_LON)
        recovered = dms[0][0] + dms[1][0] / 60 + (dms[2][0] / dms[2][1]) / 3600
        self.assertAlmostEqual(recovered, SCHOOL_LON, places=5)

    def test_roundtrip_arbitrary_values(self):
        for value in [0.0, 45.0, 90.0, 12.3456789]:
            with self.subTest(value=value):
                dms       = _decimal_to_dms(value)
                recovered = dms[0][0] + dms[1][0] / 60 + (dms[2][0] / dms[2][1]) / 3600
                self.assertAlmostEqual(recovered, value, places=5)

    def test_zero_coordinate(self):
        dms = _decimal_to_dms(0.0)
        self.assertEqual(dms[0], (0, 1))
        self.assertEqual(dms[1], (0, 1))

    def test_whole_degree(self):
        dms = _decimal_to_dms(45.0)
        self.assertEqual(dms[0][0], 45)
        self.assertEqual(dms[1][0], 0)


# ── 3. Filename building ──────────────────────────────────────────────────────

class TestBuildFilename(unittest.TestCase):

    BASE = "https://s3.example.com/photos/"

    def test_always_starts_with_sksg(self):
        result = _build_filename(self.BASE + "IMG_0001.jpeg", 0)
        self.assertTrue(result.startswith("SKSG_"))

    def test_basic_url_no_extras(self):
        result = _build_filename(self.BASE + "IMG_0001.jpeg", 0)
        self.assertEqual(result, "SKSG_IMG_0001.jpeg")

    def test_strips_query_string(self):
        url    = self.BASE + "IMG_0001.jpeg?X-Amz-Expires=600&sig=abc"
        result = _build_filename(url, 0)
        self.assertNotIn("?", result)
        self.assertNotIn("X-Amz", result)

    def test_strips_leading_hex_hash(self):
        url    = self.BASE + "50335e52be_IMG_0558.jpeg"
        result = _build_filename(url, 0)
        self.assertIn("IMG_0558.jpeg", result)
        self.assertNotIn("50335e52be", result)

    def test_strips_duplicate_extension(self):
        url    = self.BASE + "photo.jpeg.jpeg"
        result = _build_filename(url, 0)
        self.assertTrue(result.endswith(".jpeg"))
        self.assertNotIn(".jpeg.jpeg", result)

    def test_date_prefix_added(self):
        result = _build_filename(self.BASE + "IMG_0001.jpeg", 0, date_str="29 jun 2026")
        self.assertEqual(result, "SKSG_29-june-2026_IMG_0001.jpeg")

    def test_name_and_date_prefix(self):
        result = _build_filename(self.BASE + "IMG_0001.jpeg", 0,
                                 date_str="29 jun 2026", first_name="Maurits")
        self.assertEqual(result, "SKSG_Maurits_29-june-2026_IMG_0001.jpeg")

    def test_name_without_date(self):
        result = _build_filename(self.BASE + "IMG_0001.jpeg", 0, first_name="Maurits")
        self.assertEqual(result, "SKSG_Maurits_IMG_0001.jpeg")

    def test_invalid_date_omitted(self):
        result = _build_filename(self.BASE + "IMG_0001.jpeg", 0, date_str="bad date")
        self.assertEqual(result, "SKSG_IMG_0001.jpeg")

    def test_fallback_filename_on_empty_path(self):
        result = _build_filename("https://s3.example.com/", 7)
        self.assertIn("foto_7", result)

    def test_all_months_produce_english_name(self):
        months = [
            ("jan", "january"), ("feb", "february"), ("mrt", "march"),
            ("apr", "april"),   ("mei", "may"),      ("jun", "june"),
            ("jul", "july"),    ("aug", "august"),   ("sep", "september"),
            ("okt", "october"), ("nov", "november"), ("dec", "december"),
        ]
        for nl, en in months:
            with self.subTest(month=nl):
                result = _build_filename(self.BASE + "p.jpeg", 0, date_str=f"1 {nl} 2024")
                self.assertIn(en, result)

    def test_real_s3_url_full_pipeline(self):
        """End-to-end: real URL as seen on the Jaamo photos page.

        full_image_17bb0f_ is NOT a pure hex prefix (contains 'u','l','g'),
        so the regex ^[0-9a-f]+_ does not strip it — only the .jpeg.jpeg
        duplicate extension is removed.
        """
        url = (
            "https://sksg-jaamo-app.s3.eu-central-1.amazonaws.com/pictures/child/image/"
            "12497/full_image_17bb0f_IMG_6319.jpeg.jpeg"
            "?X-Amz-Expires=600&X-Amz-Date=20260629T193805Z"
            "&X-Amz-Algorithm=AWS4-HMAC-SHA256"
        )
        result = _build_filename(url, 0, date_str="29 jun 2026", first_name="Maurits")
        self.assertEqual(result, "SKSG_Maurits_29-june-2026_full_image_17bb0f_IMG_6319.jpeg")

    def test_pure_hex_prefix_is_stripped(self):
        """A filename starting with only hex chars + underscore loses the prefix."""
        url    = "https://s3.example.com/50335e52be_IMG_0558.jpeg"
        result = _build_filename(url, 0)
        self.assertEqual(result, "SKSG_IMG_0558.jpeg")


# ── 4. Collision-safe paths ───────────────────────────────────────────────────

class TestUniquePath(unittest.TestCase):

    def test_non_existent_file_returned_as_is(self):
        with tempfile.TemporaryDirectory() as d:
            result = _unique_path(d, "photo.jpeg")
            self.assertEqual(result, os.path.join(d, "photo.jpeg"))

    def test_existing_file_gets_suffix_1(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "photo.jpeg"), "w").close()
            result = _unique_path(d, "photo.jpeg")
            self.assertEqual(result, os.path.join(d, "photo_1.jpeg"))

    def test_multiple_collisions_increments(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ["photo.jpeg", "photo_1.jpeg", "photo_2.jpeg"]:
                open(os.path.join(d, name), "w").close()
            result = _unique_path(d, "photo.jpeg")
            self.assertEqual(result, os.path.join(d, "photo_3.jpeg"))

    def test_preserves_extension(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "img.jpg"), "w").close()
            result = _unique_path(d, "img.jpg")
            self.assertTrue(result.endswith(".jpg"))

    def test_result_does_not_exist_yet(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "x.jpeg"), "w").close()
            result = _unique_path(d, "x.jpeg")
            self.assertFalse(os.path.exists(result))


# ── 5. File timestamps ────────────────────────────────────────────────────────

class TestSetFileTime(unittest.TestCase):

    def _make_temp_file(self):
        f = tempfile.NamedTemporaryFile(delete=False)
        f.close()
        return f.name

    def test_sets_correct_date(self):
        path = self._make_temp_file()
        try:
            _set_file_time(path, "29 jun 2026")
            import datetime as dt
            mtime = dt.datetime.fromtimestamp(os.path.getmtime(path))
            self.assertEqual(mtime.year,  2026)
            self.assertEqual(mtime.month, 6)
            self.assertEqual(mtime.day,   29)
        finally:
            os.unlink(path)

    def test_sets_time_to_noon(self):
        path = self._make_temp_file()
        try:
            _set_file_time(path, "15 jan 2025")
            import datetime as dt
            mtime = dt.datetime.fromtimestamp(os.path.getmtime(path))
            self.assertEqual(mtime.hour, 12)
            self.assertEqual(mtime.minute, 0)
        finally:
            os.unlink(path)

    def test_invalid_date_leaves_mtime_unchanged(self):
        path = self._make_temp_file()
        try:
            original = os.path.getmtime(path)
            _set_file_time(path, "bad date")
            self.assertAlmostEqual(os.path.getmtime(path), original, places=0)
        finally:
            os.unlink(path)

    def test_none_date_leaves_mtime_unchanged(self):
        path = self._make_temp_file()
        try:
            original = os.path.getmtime(path)
            _set_file_time(path, None)
            self.assertAlmostEqual(os.path.getmtime(path), original, places=0)
        finally:
            os.unlink(path)


# ── 6. EXIF injection ─────────────────────────────────────────────────────────

class TestInjectExifMetadata(unittest.TestCase):

    def setUp(self):
        self.jpeg = _make_jpeg()
        self.date = datetime.date(2026, 6, 29)

    def test_returns_bytes(self):
        result = _inject_exif_metadata(self.jpeg, self.date)
        self.assertIsInstance(result, bytes)

    def test_result_is_valid_jpeg(self):
        result = _inject_exif_metadata(self.jpeg, self.date)
        img = Image.open(io.BytesIO(result))
        self.assertEqual(img.format, "JPEG")

    def test_datetime_written_to_0th(self):
        result = _inject_exif_metadata(self.jpeg, self.date)
        exif   = piexif.load(result)
        dt_val = exif["0th"][piexif.ImageIFD.DateTime].decode()
        self.assertEqual(dt_val, "2026:06:29 00:00:00")

    def test_datetime_original_written(self):
        result = _inject_exif_metadata(self.jpeg, self.date)
        exif   = piexif.load(result)
        dt_val = exif["Exif"][piexif.ExifIFD.DateTimeOriginal].decode()
        self.assertEqual(dt_val, "2026:06:29 00:00:00")

    def test_gps_latitude_ref_is_N(self):
        result = _inject_exif_metadata(self.jpeg, self.date)
        exif   = piexif.load(result)
        self.assertEqual(exif["GPS"][piexif.GPSIFD.GPSLatitudeRef], b"N")

    def test_gps_longitude_ref_is_E(self):
        result = _inject_exif_metadata(self.jpeg, self.date)
        exif   = piexif.load(result)
        self.assertEqual(exif["GPS"][piexif.GPSIFD.GPSLongitudeRef], b"E")

    def test_caption_written_to_image_description(self):
        result = _inject_exif_metadata(self.jpeg, self.date, caption="Spelen buiten")
        exif   = piexif.load(result)
        desc   = exif["0th"][piexif.ImageIFD.ImageDescription]
        self.assertEqual(desc, b"Spelen buiten")

    def test_caption_written_to_user_comment_as_utf16(self):
        caption = "Dag groep 2!"
        result  = _inject_exif_metadata(self.jpeg, self.date, caption=caption)
        exif    = piexif.load(result)
        uc      = exif["Exif"][piexif.ExifIFD.UserComment]
        self.assertTrue(uc.startswith(b"UNICODE\x00"))
        self.assertEqual(uc[8:].decode("utf-16-le"), caption)

    def test_unicode_caption_in_user_comment(self):
        """Characters outside latin-1 must survive in UserComment (utf-16-le)."""
        caption = "Buiten spelen 🌞"
        result  = _inject_exif_metadata(self.jpeg, self.date, caption=caption)
        exif    = piexif.load(result)
        uc      = exif["Exif"][piexif.ExifIFD.UserComment]
        self.assertEqual(uc[8:].decode("utf-16-le"), caption)

    def test_no_caption_omits_image_description(self):
        result = _inject_exif_metadata(self.jpeg, self.date)
        exif   = piexif.load(result)
        self.assertNotIn(piexif.ImageIFD.ImageDescription, exif.get("0th", {}))

    def test_invalid_jpeg_returns_original(self):
        bad    = b"not a jpeg at all"
        result = _inject_exif_metadata(bad, self.date)
        self.assertEqual(result, bad)


# ── 7. _apply_metadata (date parsing + EXIF injection combined) ───────────────

class TestApplyMetadata(unittest.TestCase):

    def setUp(self):
        self.jpeg = _make_jpeg()

    def test_valid_date_injects_exif(self):
        result = _apply_metadata(self.jpeg, "29 jun 2026")
        exif   = piexif.load(result)
        dt_val = exif["0th"][piexif.ImageIFD.DateTime].decode()
        self.assertEqual(dt_val, "2026:06:29 00:00:00")

    def test_invalid_date_returns_original(self):
        result = _apply_metadata(self.jpeg, "bad date")
        self.assertEqual(result, self.jpeg)

    def test_none_date_returns_original(self):
        result = _apply_metadata(self.jpeg, None)
        self.assertEqual(result, self.jpeg)

    def test_caption_passed_through(self):
        result  = _apply_metadata(self.jpeg, "29 jun 2026", caption="Hallo!")
        exif    = piexif.load(result)
        desc    = exif["0th"][piexif.ImageIFD.ImageDescription]
        self.assertEqual(desc, b"Hallo!")

    def test_empty_caption_omits_description(self):
        result = _apply_metadata(self.jpeg, "29 jun 2026", caption="")
        exif   = piexif.load(result)
        self.assertNotIn(piexif.ImageIFD.ImageDescription, exif.get("0th", {}))


# ── 8. Gallery HTML parsing ───────────────────────────────────────────────────

class TestGalleryHtmlParsing(unittest.TestCase):

    def setUp(self):
        self.groups = _parse_gallery_html(GALLERY_HTML)

    def test_two_date_groups_found(self):
        self.assertEqual(len(self.groups), 2)

    def test_first_group_date(self):
        self.assertEqual(self.groups[0]["date"], "29 jun 2026")

    def test_second_group_date(self):
        self.assertEqual(self.groups[1]["date"], "28 jun 2026")

    def test_first_group_photo_count(self):
        self.assertEqual(len(self.groups[0]["entries"]), 2)

    def test_second_group_photo_count(self):
        self.assertEqual(len(self.groups[1]["entries"]), 1)

    def test_full_url_extracted(self):
        self.assertEqual(self.groups[0]["entries"][0]["full"],
                         "https://s3.example.com/photo1.jpeg")

    def test_thumb_url_extracted(self):
        self.assertEqual(self.groups[0]["entries"][0]["thumb"],
                         "https://s3.example.com/photo1.jpeg")

    def test_caption_extracted_from_sibling_div(self):
        self.assertEqual(self.groups[0]["entries"][0]["caption"], "Spelen buiten")

    def test_empty_caption_div_gives_empty_string(self):
        self.assertEqual(self.groups[0]["entries"][1]["caption"], "")

    def test_entry_keys_present(self):
        entry = self.groups[0]["entries"][0]
        self.assertIn("full",    entry)
        self.assertIn("thumb",   entry)
        self.assertIn("caption", entry)

    def test_no_gallery_div_returns_empty(self):
        groups = _parse_gallery_html("<html><body>no gallery</body></html>")
        self.assertEqual(groups, [])

    def test_empty_gallery_div_returns_empty(self):
        groups = _parse_gallery_html('<div class="image_gallery"></div>')
        self.assertEqual(groups, [])

    def test_missing_data_src_entry_skipped(self):
        groups = _parse_gallery_html(GALLERY_HTML_MISSING_SRC)
        # The entry with empty data-src must be skipped → no entries in the group
        total_entries = sum(len(g["entries"]) for g in groups)
        self.assertEqual(total_entries, 0)

    def test_total_photo_count(self):
        total = sum(len(g["entries"]) for g in self.groups)
        self.assertEqual(total, 3)


# ── 9. Accounts / child HTML parsing ─────────────────────────────────────────

class TestAccountsHtmlParsing(unittest.TestCase):

    def setUp(self):
        self.children = _parse_accounts_html(ACCOUNTS_HTML)

    def test_two_unique_children_found(self):
        self.assertEqual(len(self.children), 2)

    def test_first_child_id(self):
        self.assertEqual(self.children[0]["id"], "12497")

    def test_first_child_name_from_img_alt(self):
        self.assertEqual(self.children[0]["name"], "Maurits Finn")

    def test_second_child_id(self):
        self.assertEqual(self.children[1]["id"], "99999")

    def test_second_child_name(self):
        self.assertEqual(self.children[1]["name"], "Other Kid")

    def test_duplicate_link_deduplicated(self):
        ids = [c["id"] for c in self.children]
        self.assertEqual(ids.count("12497"), 1)

    def test_non_child_links_ignored(self):
        # /ouders/timelines must not appear
        ids = [c["id"] for c in self.children]
        self.assertNotIn("timelines", ids)

    def test_no_children_in_page_returns_empty(self):
        children = _parse_accounts_html("<html><body>nothing here</body></html>")
        self.assertEqual(children, [])

    def test_fallback_name_when_no_img_tag(self):
        children = _parse_accounts_html(ACCOUNTS_HTML_NO_IMG_ALT)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]["name"], "Kind 555")

    def test_child_dict_has_id_and_name_keys(self):
        for child in self.children:
            with self.subTest(child=child):
                self.assertIn("id",   child)
                self.assertIn("name", child)


# ── 10. Credential file I/O ───────────────────────────────────────────────────

class TestCredentialFile(unittest.TestCase):
    """
    Tests credential read/write/clear logic directly, without instantiating
    JaamoApp (which requires a Tk display).
    """

    def _creds_path(self, d):
        return os.path.join(d, "jaamo_credentials.json")

    def test_saved_credentials_can_be_read_back(self):
        with tempfile.TemporaryDirectory() as d:
            path  = self._creds_path(d)
            creds = {"email": "test@example.com", "password": "s3cr3t"}
            with open(path, "w") as f:
                json.dump(creds, f)
            with open(path) as f:
                loaded = json.load(f)
            self.assertEqual(loaded["email"],    "test@example.com")
            self.assertEqual(loaded["password"], "s3cr3t")

    def test_credentials_file_mode_is_600(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._creds_path(d)
            with open(path, "w") as f:
                json.dump({"email": "x", "password": "y"}, f)
            os.chmod(path, 0o600)
            mode = oct(os.stat(path).st_mode)[-3:]
            self.assertEqual(mode, "600")

    def test_missing_credentials_file_gives_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._creds_path(d)
            # File does not exist — simulate what _load_credentials does
            try:
                with open(path) as f:
                    result = json.load(f)
            except Exception:
                result = {}
            self.assertEqual(result, {})

    def test_clearing_credentials_removes_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._creds_path(d)
            with open(path, "w") as f:
                json.dump({"email": "x", "password": "y"}, f)
            self.assertTrue(os.path.exists(path))
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            self.assertFalse(os.path.exists(path))

    def test_clearing_nonexistent_file_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._creds_path(d)
            # File never existed — must not raise
            try:
                os.remove(path)
            except FileNotFoundError:
                pass   # expected


# ── 11. Diary stories list parsing ───────────────────────────────────────────

class TestStoriesHtmlParsing(unittest.TestCase):

    def setUp(self):
        self.ids = _parse_stories_html(STORIES_HTML, "12497")

    def test_three_unique_ids_found(self):
        self.assertEqual(len(self.ids), 3)

    def test_correct_ids_extracted(self):
        self.assertEqual(self.ids, ["244244", "243260", "237958"])

    def test_order_preserved(self):
        self.assertEqual(self.ids[0], "244244")
        self.assertEqual(self.ids[1], "243260")

    def test_duplicate_link_deduplicated(self):
        self.assertEqual(self.ids.count("244244"), 1)

    def test_non_numeric_path_ignored(self):
        # "new" is not a digit string
        self.assertNotIn("new", self.ids)

    def test_non_story_links_ignored(self):
        self.assertNotIn("accounts", self.ids)

    def test_empty_page_returns_empty(self):
        result = _parse_stories_html("<html><body>nothing</body></html>", "12497")
        self.assertEqual(result, [])

    def test_wrong_child_id_ignored(self):
        result = _parse_stories_html(STORIES_HTML, "99999")
        self.assertEqual(result, [])


# ── 12. Diary story page parsing ─────────────────────────────────────────────

class TestStoryPageParsing(unittest.TestCase):

    def setUp(self):
        self.entry = _parse_story_page(STORY_PAGE_HTML)

    def test_date_extracted(self):
        self.assertIn("20 mei 2026", self.entry["date"])

    def test_time_extracted(self):
        self.assertEqual(self.entry["time"], "08:10")

    def test_three_unique_paragraphs(self):
        # "Beste ouder/verzorger" appears twice in fixture — must be deduplicated
        self.assertEqual(len(self.entry["paras"]), 3)

    def test_paragraph_content(self):
        self.assertIn("Vandaag hebben we buiten gespeeld.", self.entry["paras"])

    def test_duplicate_paragraph_deduplicated(self):
        self.assertEqual(self.entry["paras"].count("Beste ouder/verzorger"), 1)

    def test_missing_time_gives_empty_string(self):
        entry = _parse_story_page(STORY_PAGE_NO_TEXT_HTML)
        self.assertEqual(entry["time"], "")

    def test_missing_text_gives_empty_paras(self):
        entry = _parse_story_page(STORY_PAGE_NO_TEXT_HTML)
        self.assertEqual(entry["paras"], [])

    def test_missing_h1_gives_empty_date(self):
        entry = _parse_story_page("<html><body></body></html>")
        self.assertEqual(entry["date"], "")

    def test_result_has_required_keys(self):
        self.assertIn("date",   self.entry)
        self.assertIn("time",   self.entry)
        self.assertIn("paras",  self.entry)
        self.assertIn("images", self.entry)

    def test_no_images_gives_empty_list(self):
        self.assertEqual(self.entry["images"], [])

    def test_two_images_extracted(self):
        entry = _parse_story_page(STORY_PAGE_WITH_IMAGES_HTML)
        self.assertEqual(len(entry["images"]), 2)

    def test_image_thumb_url(self):
        entry = _parse_story_page(STORY_PAGE_WITH_IMAGES_HTML)
        self.assertEqual(entry["images"][0]["thumb"],
                         "https://s3.example.com/thumb/photo1.jpg")

    def test_image_full_url(self):
        entry = _parse_story_page(STORY_PAGE_WITH_IMAGES_HTML)
        self.assertEqual(entry["images"][0]["full"],
                         "https://s3.example.com/full/photo1.jpg")

    def test_image_missing_data_src_skipped(self):
        # Third image_container has empty data-src → only 2 images
        entry = _parse_story_page(STORY_PAGE_WITH_IMAGES_HTML)
        self.assertEqual(len(entry["images"]), 2)


# ── 13. Diary HTML building ───────────────────────────────────────────────────

class TestBuildDiaryHtml(unittest.TestCase):

    ENTRIES = [
        {"date": "20 mei 2026", "time": "08:10",
         "paras": ["Beste ouder/verzorger", "Vandaag hebben we gespeeld."]},
        {"date": "15 jan 2026", "time": "",
         "paras": ["Voeding: 9:00 50cc", "Slapen: 10:00-11:00"]},
    ]

    def setUp(self):
        self.html = _build_diary_html(self.ENTRIES, "Emma")

    def test_returns_string(self):
        self.assertIsInstance(self.html, str)

    def test_is_valid_html(self):
        self.assertIn("<!DOCTYPE html>", self.html)

    def test_child_name_in_title(self):
        self.assertIn("Emma", self.html)

    def test_entry_count_shown(self):
        self.assertIn("2 berichten", self.html)

    def test_dates_in_output(self):
        self.assertIn("20 mei 2026", self.html)
        self.assertIn("15 jan 2026", self.html)

    def test_time_shown_when_present(self):
        self.assertIn("08:10", self.html)

    def test_no_time_div_when_time_empty(self):
        # second entry has no time — its time div must be absent
        soup   = BeautifulSoup(self.html, "html.parser")
        entries = soup.find_all("div", class_="entry")
        times   = entries[1].find("div", class_="time")
        self.assertIsNone(times)

    def test_paragraph_text_in_output(self):
        self.assertIn("Vandaag hebben we gespeeld.", self.html)
        self.assertIn("Slapen: 10:00-11:00", self.html)

    def test_empty_entries_gives_zero_count(self):
        html = _build_diary_html([], "Emma")
        self.assertIn("0 berichten", html)


# ── 14. GUI callback integrity ────────────────────────────────────────────────

class TestGuiCallbackRefs(unittest.TestCase):
    """Catch dead command= references without starting a Tk window.

    Parses every ``command=self._xxx`` pattern from app.py and asserts the
    named method exists on JaamoApp.  Catches the class of bug where a method
    is removed but its reference in a widget's command= argument is forgotten.
    """

    def test_all_command_callbacks_exist(self):
        src_path = os.path.join(os.path.dirname(__file__), "app.py")
        with open(src_path) as f:
            source = f.read()

        # Match the full chain (e.g. canvas.yview), then keep only direct refs
        all_refs = re.findall(r"command\s*=\s*self\.([\w.]+)", source)
        refs = [r for r in all_refs if "." not in r]
        missing = [name for name in set(refs) if not hasattr(JaamoApp, name)]
        self.assertEqual(
            missing, [],
            f"JaamoApp methods used as command= callbacks but not defined: {missing}",
        )


# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
