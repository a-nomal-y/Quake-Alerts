#!/usr/bin/env python3
"""Earthquake poller: USGS, GFZ, JMA, BMKG, AFAD. Stdlib only. GitHub Actions edition.

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
