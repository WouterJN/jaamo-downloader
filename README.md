# Jaamo Downloader

A Python desktop app that logs into the Jaamo parent portal and gives you full access to your child's **photos** and **diary** (dagboek) — all in one place.

Tested with **SKSG** (`sksg.jaamo.nl`). Should work for other daycares on the Jaamo platform by changing the URL constants at the top of `app.py`.

## Features

- Secure login with optional saved credentials
- Child selector with **Foto's** and **Dagboek** per child
- **Photos:** gallery view grouped by date with captions; select and download individual photos, a full day, or everything at once
- EXIF metadata (date, caption, GPS location) written into every downloaded JPEG
- Smart filenames: `SKSG_{name}_{date}_{original}.jpeg`
- **Diary:** scrollable view of all entries in-app; download everything as a single HTML file

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
