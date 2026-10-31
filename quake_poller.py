#!/usr/bin/env python3
"""Earthquake, tsunami and volcano poller. Quakes: USGS, GFZ, JMA, BMKG, AFAD. Tsunami: PTWC/NTWC, JMA, BMKG.
Volcano: aviation ash SIGMETs, GDACS, MAGMA Indonesia, GeoNet NZ. Stdlib only. GitHub Actions edition.
Run once per schedule tick: python3 quake_poller.py --once --ledger ledger.txt
The Slack workflow webhook is read from the SLACK_WEBHOOK environment variable (a GitHub secret).

poll mode: checks all sources every --interval seconds until something needs a
Slack action, the --max-seconds budget is used, or the --deadline passes.
Prints one JSON object per action on lines starting with ACTION, then a STATUS line.
"""
import argparse, json, math, os, re, sys, time, urllib.request
from datetime import datetime, timedelta, timezone

UA = {"User-Agent": "seerist-quake-alert/1.0"}
PAGER_RANK = {None: 0, "green": 1, "yellow": 2, "orange": 3, "red": 4}
PAGER_TEXT = {"yellow": "est. 1-99 deaths or USD 1M-100M losses",
              "orange": "est. 100-999 deaths or USD 100M-1B losses",
              "red": "est. 1,000+ deaths or USD 1B+ losses"}
SHINDO = ["1", "2", "3", "4", "5-", "5+", "6-", "6+", "7"]
JMA_STRONG = "5+"          # JMA max intensity at or above this alerts
MATCH_SECONDS = 120        # same quake: origin times within 2 minutes
MATCH_KM = 100             # and epicentres within 100 km
FLOOR = 5.0                # watch everything from M5.0 up
NEW_ALERT_MAX_AGE_H = 12   # never send a NEW alert for a quake older than this
REMOTE_WORDS = re.compile(r"\b(region|ridge|rise|ocean|sea|trench|basin|zone|fracture|south of|north of|east of|west of|southeast of|southwest of|northeast of|northwest of)\b", re.I)
IST = timezone(timedelta(hours=5, minutes=30))
TEST_LOOKBACK_H = None     # testing only: widen all source windows


def lb(default_h):
    return timedelta(hours=TEST_LOOKBACK_H or default_h)


def get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def km_between(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def lead_km(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*km", text or "")
    return float(m.group(1)) if m else None


def fnum(x):
    try:
        return float(str(x).strip())
    except Exception:
        return None


# ---------------- sources -> list of reports ----------------
# report: src, id, t (datetime UTC), lat, lon, depth, mag, magtype, place, near, pager, url, shindo

def src_usgs(now):
    u = ("https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson&minmagnitude=5.0"
         "&orderby=time&updatedafter=" + iso(now - lb(1.5)))
    out = []
    for f in json.loads(get(u))["features"]:
        p, g = f["properties"], f["geometry"]["coordinates"]
        place = p.get("place") or ""
        k = lead_km(place) if re.match(r"^\s*\d", place) else None
        near = (k is not None and k <= 100) or (k is None and place and not REMOTE_WORDS.search(place))
        out.append(dict(src="USGS", id=f["id"], t=datetime.fromtimestamp(p["time"] / 1000, timezone.utc),
                        lat=g[1], lon=g[0], depth=g[2], mag=p.get("mag"), magtype=p.get("magType") or "M",
                        place=place, near=bool(near), pager=p.get("alert"), url=p.get("url")))
    return out


def src_gfz(now):
    u = ("https://geofon.gfz.de/fdsnws/event/1/query?format=text&minmag=5.0&start="
         + iso(now - lb(3)))
    out = []
    for line in get(u).splitlines():
        if not line or line.startswith("#"):
            continue
        c = line.split("|")
        if len(c) < 13:
            continue
        out.append(dict(src="GFZ", id=c[0], t=datetime.fromisoformat(c[1][:19]).replace(tzinfo=timezone.utc),
                        lat=float(c[2]), lon=float(c[3]), depth=fnum(c[4]), mag=fnum(c[10]),
                        magtype=c[9] or "M", place=c[12], near=False))
    return out


def src_jma(now):
    rows = json.loads(get("https://www.jma.go.jp/bosai/quake/data/list.json"))
    by_eid = {}
    for r in rows:
        try:
            t = datetime.fromisoformat(r["at"]).astimezone(timezone.utc)
        except Exception:
            continue
        if now - t > lb(3):
            continue
        e = by_eid.setdefault(r["eid"], dict(src="JMA", id="jma:" + r["eid"], t=t, lat=None, lon=None,
                                              depth=None, mag=None, magtype="Mj", place="", near=False,
                                              shindo=None, distant=False))
        if r.get("ttl") == "遠地地震に関する情報":
            e["distant"] = True
        m = re.match(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+)?", r.get("cod") or "")
        if m:
            e["lat"], e["lon"] = float(m.group(1)), float(m.group(2))
            if m.group(3):
                e["depth"] = abs(int(m.group(3))) / 1000
        mg = fnum(r.get("mag"))
        if mg is not None:
            e["mag"] = mg
        if r.get("en_anm"):
            e["place"] = r["en_anm"] + ("" if e["distant"] else ", Japan")
        mx = r.get("maxi") or ""
        if mx in SHINDO and (e["shindo"] is None or SHINDO.index(mx) > SHINDO.index(e["shindo"])):
            e["shindo"] = mx
    out = []
    for e in by_eid.values():
        e["near"] = not e["distant"] and e["lat"] is not None
        if (e["mag"] or 0) >= FLOOR or (e["shindo"] and SHINDO.index(e["shindo"]) >= SHINDO.index(JMA_STRONG)):
            out.append(e)
    return out


def src_bmkg(now):
    items = []
    for u in ("https://data.bmkg.go.id/DataMKG/TEWS/gempaterkini.json",
              "https://data.bmkg.go.id/DataMKG/TEWS/autogempa.json"):
        g = json.loads(get(u))["Infogempa"]["gempa"]
        items += g if isinstance(g, list) else [g]
    out, seen = [], set()
    for g in items:
        t = datetime.fromisoformat(g["DateTime"]).astimezone(timezone.utc)
        mag = fnum(g.get("Magnitude"))
        if g["DateTime"] in seen or now - t > lb(3) or (mag or 0) < FLOOR:
            continue
        seen.add(g["DateTime"])
        lat, lon = (float(x) for x in g["Coordinates"].split(","))
        k = lead_km(g.get("Wilayah"))
        out.append(dict(src="BMKG", id="bmkg:" + t.strftime("%Y%m%d%H%M%S"), t=t, lat=lat, lon=lon,
                        depth=lead_km(g.get("Kedalaman")), mag=mag, magtype="M",
                        place=(g.get("Wilayah") or "") + ", Indonesia", near=(k is not None and k <= 100)))
    return out


def src_afad(now):
    u = ("https://servisnet.afad.gov.tr/apigateway/deprem/apiv2/event/filter?format=json&minmag=5.0"
         "&start=" + iso(now - lb(3)) + "&end=" + iso(now + timedelta(minutes=5)))
    out = []
    for e in json.loads(get(u)):
        place = ", ".join(x for x in (e.get("location"), e.get("district"), e.get("province")) if x)
        out.append(dict(src="AFAD", id="afad:" + str(e["eventID"]),
                        t=datetime.fromisoformat(e["date"][:19]).replace(tzinfo=timezone.utc),
                        lat=float(e["latitude"]), lon=float(e["longitude"]), depth=fnum(e.get("depth")),
                        mag=fnum(e.get("magnitude")), magtype=e.get("type") or "M",
                        place=place + ", Turkey region", near=bool(e.get("province"))))
    return out


SOURCES = [("USGS", src_usgs), ("GFZ", src_gfz), ("JMA", src_jma), ("BMKG", src_bmkg), ("AFAD", src_afad)]


# ---------------- clustering ----------------

def same_quake(a, b):
    if abs((a["t"] - b["t"]).total_seconds()) > MATCH_SECONDS:
        return False
    if a.get("lat") is None or b.get("lat") is None:
        # JMA intensity flash without hypocentre: match anything near Japan
        other = b if a.get("lat") is None else a
        return other.get("lat") is not None and 20 <= other["lat"] <= 50 and 120 <= other["lon"] <= 155
    return km_between((a["lat"], a["lon"]), (b["lat"], b["lon"])) <= MATCH_KM


def cluster(reports):
    groups = []
    for r in sorted(reports, key=lambda x: x["t"]):
        for g in groups:
            if any(same_quake(r, m) for m in g):
                g.append(r)
                break
        else:
            groups.append([r])
    return groups


def summarize(g):
    pref = ["USGS", "GFZ", "JMA", "AFAD", "BMKG"]
    us = [r for r in g if r["src"] == "USGS"]
    usgs = max(us, key=lambda r: (r.get("mag") or 0)) if us else None
    srt = sorted(g, key=lambda r: (pref.index(r["src"]), -(r.get("mag") or 0)))
    best = next((r for r in srt if r.get("lat") is not None), srt[0])
    jma = next((r for r in g if r["src"] == "JMA"), None)
    mags = [r["mag"] for r in g if r.get("mag") is not None]
    prefmag = next((r["mag"] for r in srt if r.get("mag") is not None), None)
    gl = [r["mag"] for r in g if r["src"] in ("USGS", "GFZ") and r.get("mag") is not None]
    rulemag = max(gl) if gl else (max(mags) if mags else None)  # global agencies win; local only until they report
    depth = next((r["depth"] for r in srt if r.get("depth") is not None), None)
    return dict(t=min(r["t"] for r in g), lat=best.get("lat"), lon=best.get("lon"), depth=depth,
                maxmag=max(mags) if mags else None, mag=prefmag, rulemag=rulemag, place=best.get("place") or next((r["place"] for r in srt if r.get("place")), ""),
                near=any(r["near"] for r in g), pager=usgs.get("pager") if usgs else None,
                url=usgs.get("url") if usgs else None, shindo=jma.get("shindo") if jma else None,
                ids=[r["id"] for r in g], reports=g)


def decide(s):
    """Return reason string to alert, 'SILENT', or 'HOLD'."""
    m = s["rulemag"] or 0
    if m >= 7.0:
        return "magnitude 7.0 or above"
    if PAGER_RANK[s["pager"]] >= 2:
        return "USGS PAGER " + s["pager"] + " (" + PAGER_TEXT[s["pager"]] + ")"
    if s["shindo"] and SHINDO.index(s["shindo"]) >= SHINDO.index(JMA_STRONG):
        return "JMA max intensity " + s["shindo"] + " (strong shaking in Japan)"
    if s["pager"] == "green":
        return "SILENT"
    if m > 6.0 and s["depth"] is not None and s["depth"] < 70 and s["near"]:
        return "magnitude above 6.0, shallow and near a populated place (PAGER not yet issued)"
    return "HOLD"


# ---------------- ledger (from Slack DM) ----------------
# ledger file lines: "<key> <text>" where text holds a Ref: line (from the Slack canvas log) or older formats

def load_ledger(path):
    entries = {}
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        parts = line.strip().split(" ", 1)
        if len(parts) < 2:
            continue
        ts, text = parts
        text = text.replace("\\", "")   # canvas markdown may escape characters
        e = entries.setdefault(ts, dict(ts=ts, t=None, lat=None, lon=None, pager=None, ids=set()))
        m = re.search(r"Ref:\s*(\S+)\s*\|\s*([-\d.na]+),([-\d.na]+)\s*\|\s*PAGER\s+(\w+)\s*\|\s*ids\s+(\S+)", text)
        if m:
            e["t"] = datetime.fromisoformat(m.group(1).rstrip("Z")).replace(tzinfo=timezone.utc)
            if m.group(2) != "na":
                e["lat"], e["lon"] = float(m.group(2)), float(m.group(3))
            e["pager"] = None if m.group(4) == "none" else m.group(4)
            e["ids"].update(m.group(5).split(","))
        m = re.search(r"Ref-update:\s*PAGER\s+(\w+)", text)
        if m and PAGER_RANK.get(m.group(1), 0) > PAGER_RANK.get(e["pager"], 0):
            e["pager"] = m.group(1)
        m = re.search(r"USGS id:\s*(\w+)", text)          # older alert format
        if m:
            e["ids"].add(m.group(1))
        m = re.search(r"PAGER alert:\s*(\w+)", text)
        if m and PAGER_RANK.get(m.group(1).lower(), 0) > PAGER_RANK.get(e["pager"], 0):
            e["pager"] = m.group(1).lower()
    return list(entries.values())


def ledger_match(s, ledger):
    hits = []
    for e in ledger:
        if e["ids"] & set(s["ids"]):
            hits.append(e)
        elif e["t"] and abs((e["t"] - s["t"]).total_seconds()) <= MATCH_SECONDS:
            if e["lat"] is None or s["lat"] is None or km_between((e["lat"], e["lon"]), (s["lat"], s["lon"])) <= MATCH_KM:
                hits.append(e)
    if not hits:
        return None
    return max(hits, key=lambda e: PAGER_RANK.get(e["pager"], 0))


# ---------------- messages ----------------

def fmt_time(t):
    return t.strftime("%d %b %Y %H:%M UTC") + " (" + t.astimezone(IST).strftime("%d %b %H:%M IST") + ")"


def ref_line(s):
    ll = "na,na" if s["lat"] is None else "%.2f,%.2f" % (s["lat"], s["lon"])
    return "Ref: %sZ | %s | PAGER %s | ids %s" % (s["t"].strftime("%Y-%m-%dT%H:%M:%S"), ll, s["pager"] or "none", ",".join(s["ids"]))


def headline(s):
    mtxt = ("M%.1f" % s["mag"]) if s["mag"] is not None else "magnitude pending"
    return "%s, %s" % (mtxt, s["place"] or "location pending")


def alert_text(s, reason):
    srcs = ", ".join("%s %s %s" % (r["src"], r["magtype"], ("%.1f" % r["mag"]) if r["mag"] is not None else "n/a") for r in s["reports"])
    lines = ["EARTHQUAKE ALERT: %s" % headline(s),
             "Time: " + fmt_time(s["t"]),
             "Depth: %s | USGS PAGER: %s | JMA max intensity: %s" % (
                 ("%.0f km" % s["depth"]) if s["depth"] is not None else "n/a",
                 s["pager"] or "not yet issued", s["shindo"] or "n/a"),
             "Sources: " + srcs,
             "Why alerted: " + reason]
    if s["url"]:
        lines.append("USGS: " + s["url"])
    return "\n".join(lines)


def upgrade_text(s):
    return ("EARTHQUAKE UPDATE: USGS PAGER raised to %s (%s)\n%s, %s%s" % (
        s["pager"].upper(), PAGER_TEXT[s["pager"]], headline(s), fmt_time(s["t"]),
        ("\nUSGS: " + s["url"]) if s["url"] else ""))


def post_webhook(url, text):
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json", **UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status == 200


# ---------------- tsunami and volcano sources (independent alerts) ----------------
# each hazard item: key (unique, no spaces), text (alert message), group (seed group or None)
ASH_MIN_FT = 25000          # ash cloud top at/above this (FL250) = flight-disrupting eruption
TSUNAMI_MAX_AGE_H = 12
JMA_TSU_LEVELS = [("大津波警報", "Major tsunami warning"), ("津波警報", "Tsunami warning"), ("津波注意報", "Tsunami advisory")]


def _xml_text(block, tag):
    m = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), block, re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def hz_ptwc(now):
    out = []
    for feed, centre in (("PHEB", "Pacific Tsunami Warning Center"), ("PAAQ", "US National Tsunami Warning Center")):
        xml = get("https://www.tsunami.gov/events/xml/%sAtom.xml" % feed)
        for ent in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
            summ = re.sub(r"<[^>]+>", " ", _xml_text(ent, "summary"))
            cat = re.search(r"Category:\s*([A-Za-z ]+?)\s+Bulletin", summ)
            cat = cat.group(1).strip() if cat else ""
            upd = _xml_text(ent, "updated")
            try:
                t = datetime.fromisoformat(upd.replace("Z", "+00:00"))
            except Exception:
                continue
            if not cat or cat.lower() == "information" or now - t > timedelta(hours=TSUNAMI_MAX_AGE_H):
                continue
            mag = re.search(r"Preliminary Magnitude:\s*([\d.]+)", summ)
            link = re.search(r'title="Bulletin" href="([^"]+)"', ent)
            region = _xml_text(ent, "title")
            text = "\n".join([
                "TSUNAMI ALERT: %s issued by the %s" % (cat, centre),
                "Region: " + region.title(),
                "Issued: " + fmt_time(t),
                "Preliminary quake magnitude: " + (mag.group(1) if mag else "n/a"),
                "Bulletin: " + (link.group(1) if link else "https://www.tsunami.gov/")])
            # one alert per region, category and day: hourly update bulletins and the twin US centre do not repeat it
            out.append(dict(key="tsu-us-%s-%s-%s" % (re.sub(r"[^a-z0-9]+", "-", region.lower()).strip("-"),
                                                     cat.lower().replace(" ", "-"), t.strftime("%Y%m%d")), text=text, group=None))
    return out


def hz_jma_tsunami(now):
    out = []
    for r in json.loads(get("https://www.jma.go.jp/bosai/tsunami/data/list.json")):
        try:
            t = datetime.fromisoformat(r["rdt"]).astimezone(timezone.utc)
        except Exception:
            continue
        if now - t > timedelta(hours=TSUNAMI_MAX_AGE_H) or r.get("ift") != "発表":
            continue
        kinds = [k.get("kind", "") for k in r.get("kind", [])]
        level = next((en for jp, en in JMA_TSU_LEVELS if any(jp in k and "解除" not in k for k in kinds)), None)
        if not level:
            continue
        text = "\n".join([
            "TSUNAMI ALERT: Japan %s (JMA)" % level,
            "Quake: %s, M%s" % (r.get("en_anm") or r.get("anm") or "location n/a", r.get("mag") or "n/a"),
            "Issued: " + fmt_time(t),
            "Details: https://www.jma.go.jp/bosai/map.html#contents=tsunami"])
        out.append(dict(key="tsu-jma-%s-%s" % (r.get("eid"), level.replace(" ", "-").lower()), text=text, group=None))
    return out


def hz_bmkg_tsunami(now):
    out, seen = [], set()
    for u in ("https://data.bmkg.go.id/DataMKG/TEWS/autogempa.json",
              "https://data.bmkg.go.id/DataMKG/TEWS/gempaterkini.json"):
        g = json.loads(get(u))["Infogempa"]["gempa"]
        for e in (g if isinstance(g, list) else [g]):
            pot = (e.get("Potensi") or "").lower()
            if "berpotensi tsunami" not in pot or "tidak berpotensi" in pot or e["DateTime"] in seen:
                continue
            seen.add(e["DateTime"])
            t = datetime.fromisoformat(e["DateTime"]).astimezone(timezone.utc)
            if now - t > timedelta(hours=TSUNAMI_MAX_AGE_H):
                continue
            text = "\n".join([
                "TSUNAMI ALERT: Indonesia's BMKG says this quake has tsunami potential",
                "Quake: M%s, %s, depth %s" % (e.get("Magnitude"), e.get("Wilayah"), e.get("Kedalaman")),
                "Time: " + fmt_time(t),
                "BMKG note: " + (e.get("Potensi") or ""),
                "Details: https://www.bmkg.go.id/"])
            out.append(dict(key="tsu-bmkg-" + t.strftime("%Y%m%d%H%M%S"), text=text, group=None))
    return out


def hz_ash_sigmet(now):
    out = []
    for x in json.loads(get("https://aviationweather.gov/api/data/isigmet?format=json&hazard=va")):
        raw = (x.get("rawSigmet") or "").replace("\n", " ")
        top = x.get("top") or max([int(f) * 100 for f in re.findall(r"FL(\d{3})", raw)] or [0])
        if not top or top < ASH_MIN_FT:
            continue
        m = re.search(r"VA(?:\s+ERUPTION)?\s+(?:MT\s+|VOLCAN\s+)?(.+?)\s+PSN", raw)
        name = re.sub(r"\s+", " ", m.group(1)).strip().title() if m else "Unnamed volcano"
        fir = (x.get("firName") or x.get("firId") or "").strip()
        vf = datetime.fromtimestamp(x["validTimeFrom"], timezone.utc) if x.get("validTimeFrom") else now
        vt = datetime.fromtimestamp(x["validTimeTo"], timezone.utc) if x.get("validTimeTo") else None
        text = "\n".join([
            "VOLCANO ALERT: %s, ash cloud up to FL%03d (about %.0f km)" % (name, top // 100, top * 0.0003048),
            "Aviation ash warning (SIGMET) for %s airspace, valid %s%s" % (
                fir, vf.strftime("%d %b %H:%M UTC"), (" to " + vt.strftime("%H:%M UTC")) if vt else ""),
            "Why alerted: ash at or above FL250 usually disrupts flights",
            "Source: https://aviationweather.gov/gfa/#sigmet"])
        out.append(dict(key="ash-%s-%s" % (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"), vf.strftime("%Y%m%d")),
                        text=text, group=None))
    return out


def hz_gdacs_volcano(now):
    out = []
    d = json.loads(get("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist=VO"))
    for f in d.get("features", []):
        p = f["properties"]
        lvl = (p.get("alertlevel") or "").capitalize()
        try:
            mod = datetime.fromisoformat(p.get("datemodified")[:19]).replace(tzinfo=timezone.utc)
            frm = datetime.fromisoformat(p.get("fromdate")[:19]).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if lvl not in ("Orange", "Red") or now - mod > timedelta(days=3) or now - frm > timedelta(days=7):
            continue
        text = "\n".join([
            "VOLCANO ALERT: %s eruption alert, %s, %s (GDACS)" % (lvl, p.get("eventname"), p.get("country")),
            "Eruption reported: " + fmt_time(frm),
            "Report: " + ((p.get("url") or {}).get("report") or "https://www.gdacs.org/")])
        out.append(dict(key="gdacs-vo-%s-%s-%s" % (p.get("eventid"), p.get("episodeid"), lvl.lower()), text=text, group=None))
    return out


def hz_magma_levels(now):
    html_ = get("https://magma.esdm.go.id/v1/gunung-api/tingkat-aktivitas")
    out = []
    marks = [("Level IV (Awas)</a>", "Level IV (Awas, highest)", "4"), ("Level III (Siaga)</a>", "Level III (Siaga)", "3"),
             ("Level II (Waspada)</a>", None, None)]
    pos = [html_.find(m[0]) for m in marks]
    if min(pos) < 0:
        raise ValueError("MAGMA page layout changed")
    for i in range(2):
        seg = html_[pos[i]:pos[i + 1]]
        for name, link in re.findall(r"<td>\s*([^<]+?)\s*<a href=\"([^\"]*laporan[^\"]*)\"", seg):
            name = re.sub(r"\s+", " ", name).strip()
            vol = name.split(" - ")[0].strip()
            text = "\n".join([
                "VOLCANO ALERT: %s (Indonesia) is at %s" % (name, marks[i][1]),
                "Set by Indonesia's volcano agency PVMBG (MAGMA Indonesia); Level III means danger zones are expanded, Level IV means evacuation",
                "Report: " + link.split("?")[0]])
            out.append(dict(key="magma-%s-l%s-%s" % (re.sub(r"[^a-z0-9]+", "-", vol.lower()).strip("-"), marks[i][2], now.year),
                            text=text, group="magma"))
    return out


def hz_geonet_val(now):
    out = []
    for f in json.loads(get("https://api.geonet.org.nz/volcano/val"))["features"]:
        p = f["properties"]
        if int(p.get("level", 0)) < 3:
            continue
        text = "\n".join([
            "VOLCANO ALERT: %s (New Zealand) at Volcanic Alert Level %s: %s" % (p.get("volcanoTitle"), p.get("level"), p.get("activity")),
            "Hazards: " + (p.get("hazards") or "n/a"),
            "Source: https://www.geonet.org.nz/volcano"])
        out.append(dict(key="geonet-%s-l%s-%s" % (p.get("volcanoID"), p.get("level"), now.year), text=text, group="geonet"))
    return out


HAZARD_SOURCES = [("PTWC/NTWC", hz_ptwc), ("JMA-tsunami", hz_jma_tsunami), ("BMKG-tsunami", hz_bmkg_tsunami),
                  ("Ash-SIGMET", hz_ash_sigmet), ("GDACS-volcano", hz_gdacs_volcano),
                  ("MAGMA-Indonesia", hz_magma_levels), ("GeoNet-NZ", hz_geonet_val)]


def load_hz_keys(path):
    keys = set()
    try:
        for line in open(path, encoding="utf-8").read().splitlines():
            m = re.search(r"Ref:\s*HZ\s+(\S+)", line.replace("\\", ""))
            if m:
                keys.add(m.group(1))
    except FileNotFoundError:
        pass
    return keys


def hz_ref(key, now):
    return "Ref: HZ %s | %s" % (key, now.strftime("%Y-%m-%dT%H:%M:%SZ"))


# ---------------- main loop ----------------

def poll_once(now, ledger, webhook, ledger_path):
    reports, health = [], {}
    for name, fn in SOURCES:
        try:
            r = fn(now)
            reports += r
            health[name] = "ok(%d)" % len(r)
        except Exception as ex:
            health[name] = "error: " + type(ex).__name__
    actions = []
    for g in cluster(reports):
        s = summarize(g)
        led = ledger_match(s, ledger)
        if led:
            if PAGER_RANK[s["pager"]] >= 3 and PAGER_RANK[s["pager"]] > PAGER_RANK.get(led["pager"], 0):
                kind, text, label = "update", upgrade_text(s), "PAGER update to " + s["pager"].upper()
            else:
                continue
        else:
            reason = decide(s)
            if reason in ("SILENT", "HOLD") or now - s["t"] > timedelta(hours=NEW_ALERT_MAX_AGE_H):
                continue
            kind, text, label = "alert", alert_text(s, reason), "alert"
        sent = False
        if webhook:
            try:
                sent = post_webhook(webhook, text)
            except Exception:
                sent = False
        ref = ref_line(s)
        if sent or not webhook:   # only record what really went out, so a failed send is retried next run
            with open(ledger_path, "a", encoding="utf-8") as f:
                f.write("local-%d-%d %s\n" % (time.time() * 1000, len(actions), ref))
        act = dict(action="log_to_canvas", kind=kind, webhook_sent=sent, line=ref)
        if not sent:
            act["fallback_text"] = text
        actions.append(act)

    # tsunami and volcano alerts
    hz_keys = load_hz_keys(ledger_path)
    for name, fn in HAZARD_SOURCES:
        try:
            items = fn(now)
            health[name] = "ok(%d)" % len(items)
        except Exception as ex:
            health[name] = "error: " + type(ex).__name__
            continue
        groups = set(i["group"] for i in items if i["group"])
        for grp in groups:           # first time we see a seeded source: record what is already active, silently
            if "seed-" + grp not in hz_keys:
                for i in items:
                    if i["group"] == grp and i["key"] not in hz_keys:
                        hz_keys.add(i["key"])
                        line = hz_ref(i["key"], now)
                        with open(ledger_path, "a", encoding="utf-8") as f:
                            f.write("local-%d-s %s\n" % (time.time() * 1000, line))
                        actions.append(dict(action="log_to_canvas", kind="seed", webhook_sent=True, silent=True, line=line))
                hz_keys.add("seed-" + grp)
                line = hz_ref("seed-" + grp, now)
                with open(ledger_path, "a", encoding="utf-8") as f:
                    f.write("local-%d-s %s\n" % (time.time() * 1000, line))
                actions.append(dict(action="log_to_canvas", kind="seed", webhook_sent=True, silent=True, line=line))
        for i in items:
            if i["key"] in hz_keys:
                continue
            sent = False
            if webhook:
                try:
                    sent = post_webhook(webhook, i["text"])
                except Exception:
                    sent = False
            line = hz_ref(i["key"], now)
            hz_keys.add(i["key"])
            if sent or not webhook:
                with open(ledger_path, "a", encoding="utf-8") as f:
                    f.write("local-%d-h %s\n" % (time.time() * 1000, line))
            act = dict(action="log_to_canvas", kind="hazard", webhook_sent=sent, line=line)
            if not sent:
                act["fallback_text"] = i["text"]
            actions.append(act)
    return actions, health


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default="/tmp/quake/ledger.txt")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--max-seconds", type=int, default=540)
    ap.add_argument("--deadline", type=float, default=0, help="epoch seconds; stop when passed")
    ap.add_argument("--webhook", default=os.environ.get("SLACK_WEBHOOK", ""))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test-lookback-hours", type=float, default=None)
    a = ap.parse_args()
    global TEST_LOOKBACK_H, NEW_ALERT_MAX_AGE_H
    if a.test_lookback_hours:
        TEST_LOOKBACK_H = a.test_lookback_hours
        NEW_ALERT_MAX_AGE_H = a.test_lookback_hours
    start = time.time()
    polls = 0
    while True:
        now = datetime.now(timezone.utc)
        actions, health = poll_once(now, load_ledger(a.ledger), a.webhook, a.ledger)
        polls += 1
        if actions:
            for x in actions:
                x.pop("fallback_text", None)
                print("ACTION " + json.dumps(x, ensure_ascii=False))
            print("STATUS actions polls=%d health=%s" % (polls, json.dumps(health)))
            if any(not x["webhook_sent"] for x in actions):
                sys.exit(1)   # makes the GitHub run fail, which emails the owner
            return
        if a.once:
            break
        left = a.max_seconds - (time.time() - start)
        if a.deadline and time.time() + a.interval > a.deadline:
            print("STATUS deadline polls=%d health=%s" % (polls, json.dumps(health)))
            return
        if left < a.interval:
            break
        time.sleep(a.interval)
    print("STATUS idle polls=%d health=%s" % (polls, json.dumps(health)))


if __name__ == "__main__":
    main()
