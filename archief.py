"""Radar-archief voor de Sneeuwradar-app.

Haalt de radarkaarten (sneeuw- en regenval per 3 uur) van wintersport.nl op,
bewaart ze 2 maanden en noteert per kaart hoeveel neerslag er op Les Sybelles valt.
Draait elke 3 uur via GitHub Actions; de app leest index.json en de PNG's.

Gebruik: python archief.py <map>
"""
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from io import BytesIO

from PIL import Image

DOMAIN = "https://maps.nskiv.com"
KEEP_DAYS = 62
STEP, MAX_H = 3, 48  # bewaar de eerste 2 dagen van elke modelrun; nieuwere runs overschrijven
UA = {"User-Agent": "sneeuwradar-archief/1.0"}

# Positie van Les Sybelles op de Alpenkaart (zelfde Mercator-formule als de app)
BOUNDS = ((43.5317, 4.98676), (48.4116, 16.5236))
RATIO = 586 / 960
LAT, LON = 45.245, 6.27

SNOW = [(0, 255, 255), (0, 200, 255), (0, 150, 255), (83, 129, 232), (157, 111, 212),
        (215, 89, 243), (240, 134, 98), (255, 165, 0), (255, 85, 0), (255, 0, 0)]
RAIN = [(214, 221, 235), (189, 205, 224), (159, 176, 206), (133, 155, 194),
        (110, 134, 180), (89, 115, 162), (73, 94, 133)]


def merc(lat):
    return math.atanh(math.sin(math.radians(lat)))


def position():
    span = BOUNDS[1][1] - BOUNDS[0][1]
    x = (LON - BOUNDS[0][1]) / span
    k = 360 / (2 * math.pi * span)
    y = 1 - k * (merc(LAT) - merc(BOUNDS[0][0])) / RATIO
    return x, y


def nearest(rgb, pal):
    best, bd = -1, 1e9
    for i, c in enumerate(pal):
        d = sum((a - b) ** 2 for a, b in zip(rgb, c))
        if d < bd:
            best, bd = i, d
    return best, bd


def classify(png_bytes):
    """Zwaarste neerslag in een klein rondje rond Les Sybelles -> legendaklasse."""
    im = Image.open(BytesIO(png_bytes)).convert("RGBA")
    w, h = im.size
    px, py = position()
    cx, cy = round(px * w), round(py * h)
    snow = rain = -1
    for dx in range(-3, 4):
        for dy in range(-3, 4):
            x, y = cx + dx, cy + dy
            if not (0 <= x < w and 0 <= y < h):
                continue
            r, g, b, a = im.getpixel((x, y))
            if a < 120:
                continue
            si, sd = nearest((r, g, b), SNOW)
            ri, rd = nearest((r, g, b), RAIN)
            if sd <= rd and sd < 2500:
                snow = max(snow, si)
            elif rd < 2500:
                rain = max(rain, ri)
    if snow >= 0:
        return {"kind": "snow", "i": snow}
    if rain >= 0:
        return {"kind": "rain", "i": rain}
    return None


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def stamp(d):
    return d.strftime("%Y-%m-%d_%H")


CAMS = {
    "ouillon": ("https://skaping.s3.gra.io.cloud.ovh.net/les-sybelles/ouillon", (0,)),
    "corbier": ("https://skaping.s3.gra.io.cloud.ovh.net/le-corbier/front-de-neige/panoramique", (0, 1)),
}


def snow_share(jpg_bytes):
    """Deel van het onderste deel van het webcambeeld dat wit/grijs-wit is (sneeuw), in procenten."""
    im = Image.open(BytesIO(jpg_bytes)).convert("RGB")
    w, h = im.size
    im = im.crop((0, int(h * 0.62), w, h)).resize((240, 80))  # alleen de grond, geen lucht of mist
    px = list(im.getdata())
    # licht, weinig kleur en niet geler dan blauw (sneeuw in de schaduw is blauwig)
    snow = sum(1 for r, g, b in px
               if max(r, g, b) > 140 and min(r, g, b) > 90 and max(r, g, b) - min(r, g, b) < 70 and b >= r - 10)
    return round(100 * snow / len(px))


def webcams(folder):
    """Zoekt per webcam het nieuwste beeld overdag (10-16 uur Franse tijd) en meet hoeveel sneeuw er te zien is."""
    from zoneinfo import ZoneInfo
    paris = datetime.now(ZoneInfo("Europe/Paris"))
    out = {}
    for cam, (base, offsets) in CAMS.items():
        t = paris.replace(second=0, microsecond=0)
        t = t - timedelta(minutes=t.minute % 10)
        for _ in range(26 * 6):
            if 10 <= t.hour < 16:
                hit = None
                for off in offsets:
                    url = f"{base}/{t:%Y/%m/%d}/small/{t:%H}-{t.minute + off:02d}.jpg"
                    try:
                        hit = (url, get(url))
                        break
                    except Exception:
                        pass
                if hit:
                    out[cam] = {"pct": snow_share(hit[1]), "time": t.strftime("%Y-%m-%d %H:%M"), "url": hit[0]}
                    break
            t -= timedelta(minutes=10)
    os.makedirs(os.path.join(folder, "bron"), exist_ok=True)
    with open(os.path.join(folder, "bron", "webcam.json"), "w", encoding="utf-8") as f:
        json.dump(out, f)
    print(f"Webcams: {out}")


def main(folder):
    os.makedirs(os.path.join(folder, "radar"), exist_ok=True)
    index_path = os.path.join(folder, "index.json")
    index = {"frames": {}}
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
    frames = index.setdefault("frames", {})

    cur = json.loads(get(f"{DOMAIN}/alps/current_run_18.json?t={int(datetime.now().timestamp())}"))
    run = f"{cur['rundate']}_{cur.get('rundir', '18')}"
    base = datetime.strptime(f"{cur['rundate']} {cur.get('rundir', '18')}", "%Y-%m-%d %H").replace(tzinfo=timezone.utc) + timedelta(hours=6)
    print(f"Modelrun {run}")

    added = 0
    for h in range(STEP, MAX_H + 1, STEP):
        end = base + timedelta(hours=h)
        start = end - timedelta(hours=STEP)
        key = stamp(end)
        old = frames.get(key)
        if old and old.get("run", "") >= run:
            continue  # al bewaard uit deze of een nieuwere run
        url = f"{DOMAIN}/alps/precip/{run}/{stamp(start)}_{stamp(end)}.png"
        try:
            png = get(url)
        except Exception as e:  # beeld (nog) niet beschikbaar
            print(f"  overgeslagen {key}: {e}")
            continue
        with open(os.path.join(folder, "radar", f"{key}.png"), "wb") as f:
            f.write(png)
        frames[key] = {
            "start": int(start.timestamp() * 1000),
            "end": int(end.timestamp() * 1000),
            "run": run,
            "here": classify(png),
        }
        added += 1

    # Ouder dan 2 maanden opruimen
    limit = int((datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).timestamp() * 1000)
    for key in [k for k, v in frames.items() if v["end"] < limit]:
        frames.pop(key)
        try:
            os.remove(os.path.join(folder, "radar", f"{key}.png"))
        except FileNotFoundError:
            pass

    index["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    index["frames"] = dict(sorted(frames.items()))
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, separators=(",", ":"))
    print(f"{added} kaarten toegevoegd, {len(frames)} in archief")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "data"
    try:
        webcams(target)
    except Exception as e:  # webcams zijn een extraatje; de radar gaat voor
        print(f"Webcams overgeslagen: {e}")
    main(target)
