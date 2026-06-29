# Jaamo Photo Downloader

A Python desktop app that logs into the Jaamo parent portal for SKSG daycare and lets you browse, select, and download all photos of your child — with EXIF metadata (date, caption, GPS) automatically injected into every file.

## Features

- Secure login with optional saved credentials
- Automatic child detection — pick from all children on your account
- Gallery view grouped by date with captions
- Select individual photos, a full day, or everything at once
- EXIF metadata (date, caption, GPS location) written into every downloaded JPEG
- Smart filenames: `SKSG_{name}_{date}_{original}.jpeg`

## Requirements

- Python 3.9+
- pip packages: `requests`, `beautifulsoup4`, `Pillow`, `piexif`

## Installation

```bash
git clone https://github.com/WouterJN/jaamo-downloader.git
cd jaamo-downloader
pip install -r requirements.txt
python3 app.py
```

## Manual

See the full manual on the [project page](https://wouterjn.github.io/jaamo-downloader/).
