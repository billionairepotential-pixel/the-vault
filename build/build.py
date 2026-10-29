#!/usr/bin/env python3
"""Build The Vault from its sources.

Sources (edit these):
  ydk/<slug>.ydk            each deck's card list (the final word on what is in a deck)
  content/decks.json        chapters, deck names, labels, taglines, best opening hands
  content/analysis.json     collection report: ratings, verdicts, hand-odds roles
  content/notes.json        deck notes from the reference guides
  data/cards.json           card text shown in the decklist viewer
  data/guides.json          visual-guide slides and card positions
  build/template.html       the page itself (layout, styles, scripts)

Generated (do not edit by hand):
  index.html                the live page, with all data built in
  content/decklists.json    decklists rebuilt from the .ydk files
  web/                      smaller copies of art/ and cards/ that the live site loads
  dist/                    offline copies (with --dist)

What the build does:
  * reads every .ydk file and rebuilds that deck's list; when a list changes, the
    deck's "updated" date is set to today
  * fills in card text for cards new to a deck (copied from another deck that runs the
    card, or imported from YGOPRODeck and flagged for checking against Konami's database)
  * recalculates card counts, the Extra Deck breakdown, exact hand odds, overall scores
    and the ranking
  * checks the result and prints anything that needs a human look

Usage:  python build/build.py [--dist DIR] [--offline-ok]
"""
import argparse, base64, datetime, hashlib, io, json, os, re, struct, sys, urllib.parse, urllib.request, zipfile
from math import comb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = lambda *a: os.path.join(ROOT, *a)
TODAY = datetime.datetime.now(datetime.timezone.utc).astimezone().date().isoformat()

warnings, errors, changes = [], [], []
def warn(m): warnings.append(m)
def error(m): errors.append(m)

def load(path):
    with open(P(path), encoding="utf8") as f: return json.load(f)
def save(path, obj, indent=2):
    if indent is None:   # compact, like the site's own data files
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(obj, ensure_ascii=False, indent=indent) + "\n"
    old = open(P(path), encoding="utf8").read() if os.path.exists(P(path)) else None
    if old != text:
        with open(P(path), "w", encoding="utf8", newline="\n") as f: f.write(text)
        return True
    return False

# ---------------------------------------------------------------- .ydk handling
def parse_ydk(text):
    out, sec = {"main": [], "extra": [], "side": []}, None
    for line in text.splitlines():
        line = line.strip()
        if line in ("#main", "#extra"): sec = line[1:]; continue
        if line == "!side": sec = "side"; continue
        if not line or line.startswith("#") or sec is None: continue
        if not line.isdigit(): error(f"unreadable line in .ydk: {line!r}"); continue
        out[sec].append(str(int(line)))
    return out

def ydke(p):
    enc = lambda a: base64.b64encode(b"".join(struct.pack("<I", int(x)) for x in a)).decode()
    return "ydke://" + enc(p["main"]) + "!" + enc(p["extra"]) + "!" + enc(p["side"]) + "!"

# ---------------------------------------------------------------- card lookups (YGOPRODeck fallback)
API = "https://db.ygoprodeck.com/api/v7/cardinfo.php?misc=yes&"
_api_cache = {}
def api_card(**q):
    key = json.dumps(q, sort_keys=True)
    if key not in _api_cache:
        url = API + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers={"User-Agent": "the-vault-build"})
        with urllib.request.urlopen(req, timeout=30) as r:
            _api_cache[key] = json.load(r)["data"][0]
    return _api_cache[key]

def list_type(c):
    """YGOPRODeck card -> the type label the decklist uses (e.g. 'Quick-Play Spell', 'Synchro Tuner')."""
    t = c["type"]
    if t == "Spell Card": return c["race"] + " Spell"
    if t == "Trap Card": return c["race"] + " Trap"
    m = {"Effect Monster": "Effect", "Flip Effect Monster": "Effect", "Spirit Monster": "Effect",
         "Union Effect Monster": "Effect", "Gemini Monster": "Effect", "Toon Monster": "Effect",
         "Tuner Monster": "Tuner", "Normal Tuner Monster": "Tuner", "Flip Tuner Effect Monster": "Tuner",
         "Normal Monster": "Normal", "Ritual Monster": "Ritual", "Ritual Effect Monster": "Ritual Effect",
         "Pendulum Effect Monster": "Pendulum Effect", "Pendulum Normal Monster": "Pendulum Normal",
         "Pendulum Tuner Effect Monster": "Pendulum Effect", "Pendulum Effect Ritual Monster": "Pendulum Effect Ritual",
         "Fusion Monster": "Fusion", "Pendulum Effect Fusion Monster": "Fusion",
         "Synchro Monster": "Synchro", "Synchro Tuner Monster": "Synchro Tuner", "Synchro Pendulum Effect Monster": "Synchro",
         "XYZ Monster": "Xyz", "XYZ Pendulum Effect Monster": "Xyz", "Link Monster": "Link"}
    return m.get(t, t.replace(" Monster", ""))

def section_of(t):
    if t.endswith(" Spell"): return "spells"
    if t.endswith(" Trap"): return "traps"
    if t.split()[0] in ("Fusion", "Synchro", "Xyz", "Link"): return "extra"
    return "monsters"

def card_entry_from_api(c):
    """A basic viewer entry for a card that no deck in the collection had before."""
    t = list_type(c)
    frame = c.get("frameType", "effect").replace("_pendulum", "")
    chips = [t]
    if "attribute" in c:
        chips += [c["attribute"], c["race"]]
        if frame == "link": chips += [f"Link-{c.get('linkval')}", f"ATK {c.get('atk')}"]
        else:
            chips += [("Rank " if frame == "xyz" else "Level ") + str(c.get("level")), f"ATK {c.get('atk')} / DEF {c.get('def')}"]
        if c.get("linkmarkers"): chips.append("Arrows: " + ", ".join(c["linkmarkers"]))
        if "scale" in c: chips.append(f"Pendulum Scale {c['scale']}")
    ban = (c.get("banlist_info") or {}).get("ban_tcg")
    misc = (c.get("misc_info") or [{}])[0]
    paras = lambda s: [{"t": x.strip()} for x in re.split(r"\r?\n", s or "") if x.strip()]
    mat, desc = "", c.get("monster_desc") or c.get("desc", "")
    if section_of(t) == "extra":
        first, _, rest = desc.partition("\n"); mat, desc = first.strip(), rest
    fx = ([{"l": "Pendulum Effect", "x": paras(c.get("pend_desc"))}, {"l": "Monster Effect", "x": paras(desc)}]
          if c.get("pend_desc") else [{"l": "", "x": paras(desc)}])
    return {"f": frame, "chips": chips, "pw": str(c["id"]), "rel": misc.get("tcg_date", ""),
            "fl": ban or "Unlimited", "ocg": "", "mat": mat, "fx": fx, "src": "ygoprodeck",
            "n": [{"l": "Source", "h": "<ul>\n<li>Card text imported automatically from YGOPRODeck. Check it against Konami's official card database.</li>\n</ul>"}]}

# ---------------------------------------------------------------- hand odds (exact, hypergeometric)
def odds(decklist, roles, deck_name):
    q = {}
    for g in ("monsters", "spells", "traps"):
        for c in decklist[g]: q[c["n"]] = q.get(c["n"], 0) + c["q"]
    N = sum(q.values())
    st, pc, it = set(roles["starters"]), set(roles["pieces"]), set(roles["interaction"])
    for name in sorted((st | pc | it) - set(q)):
        warn(f"{deck_name}: the hand-odds role lists still name {name}, which is no longer in the Main Deck")
    groups = {}
    for name, n in q.items():
        sig = (name in st, name in pc, name in it); groups[sig] = groups.get(sig, 0) + n
    sigs = list(groups)
    C = lambda n, k: comb(n, k) if 0 <= k <= n else 0
    def prob(hand, test):
        acc = 0
        def rec(i, left, w, s, e, x):
            nonlocal acc
            if i == len(sigs):
                if left == 0 and test(s, e, x): acc += w
                return
            g = sigs[i]
            for k in range(left + 1):
                ww = C(groups[g], k)
                if ww: rec(i + 1, left - k, w * ww, s + k * g[0], e + k * g[1], x + k * g[2])
        rec(0, hand, 1, 0, 0, 0)
        return round(acc / C(N, hand) * 100, 1)
    S = sum(q.get(n, 0) for n in roles["starters"]); E = sum(q.get(n, 0) for n in roles["pieces"])
    I = sum(q.get(n, 0) for n in roles["interaction"]); B = roles["breakerCount"]
    return {"S": S, "E": E, "I": I, "B": B,
            "brick5": prob(5, lambda s, e, x: not (s >= 1 or e >= 2)),
            "starter5": prob(5, lambda s, e, x: s >= 1),
            "ideal5": prob(5, lambda s, e, x: (s >= 1 or e >= 2) and x >= 1),
            "int6": prob(6, lambda s, e, x: x >= 1),
            "int6x2": prob(6, lambda s, e, x: x >= 2),
            "breaker6": round((1 - C(N - B, 6) / C(N, 6)) * 100, 1),
            "starters": roles["starters"], "pieces": roles["pieces"], "interaction": roles["interaction"]}

def breakdown(dl):
    n = lambda g: sum(c["q"] for c in dl[g])
    pl = lambda k, w: f"{k} {w}" + ("" if k == 1 else "s")
    order, cnt = [], {}
    for c in dl["extra"]:
        t = "Synchro" if c["t"] == "Synchro Tuner" else c["t"]
        if t not in cnt: order.append(t); cnt[t] = 0
        cnt[t] += c["q"]
    order.sort(key=lambda t: -cnt[t])          # largest first; ties keep Extra Deck order
    return (f"{pl(n('monsters'), 'Monster')} · {pl(n('spells'), 'Spell')} · {pl(n('traps'), 'Trap')}  /  "
            + " · ".join(f"{cnt[t]} {t}" for t in order))

# ---------------------------------------------------------------- main build
def build(allow_offline):
    decks = load("content/decks.json"); analysis = load("content/analysis.json")
    notes = load("content/notes.json"); decklists = load("content/decklists.json")
    aliases = load("content/passcode-aliases.json"); cards = load("data/cards.json")
    guide_rects = load("data/guides.json")["rects"]

    known = {}                                   # passcode -> (name, list type) for every card already in the collection
    for slug, dl in decklists.items():
        for g in dl.values():
            for c in g:
                pw = cards.get(slug, {}).get(c["n"], {}).get("pw")
                if pw: known[pw] = (c["n"], c["t"])
    for pw, a in aliases.items():
        if "type" in a or a["name"] in {v[0] for v in known.values()}:
            known.setdefault(pw, (a["name"], a.get("type") or next(v[1] for v in known.values() if v[0] == a["name"])))

    def resolve(pw):
        if pw in known: return known[pw]
        try:
            c = api_card(id=pw)
        except Exception as e:
            (warn if allow_offline else error)(f"passcode {pw} is unknown and YGOPRODeck could not be reached ({e})")
            return None
        name, t = c["name"], list_type(c)
        aliases[pw] = {"name": name, "type": t}
        if str(c["id"]) != pw: aliases[pw]["note"] = "alternate-artwork passcode"
        known[pw] = (name, t)
        return known[pw]

    slugs = [d["slug"] for col in decks["collections"] for d in col["decks"]]
    for f in sorted(os.listdir(P("ydk"))):
        if f.endswith(".ydk") and f[:-4] not in slugs:
            warn(f"ydk/{f} doesn't match any deck in content/decks.json, so it isn't on the site (check the file name)")
    all_entries = {}                              # card name -> viewer entry from any deck (for reuse)
    for slug in cards:
        for name, e in cards[slug].items(): all_entries.setdefault(name, e)

    ydk_out = {}
    for col in decks["collections"]:
        for deck in col["decks"]:
            slug = deck["slug"]
            path = P("ydk", slug + ".ydk")
            if not os.path.exists(path): error(f"{slug}: ydk/{slug}.ydk is missing"); continue
            text = open(path, encoding="utf8").read().replace("\r\n", "\n")
            p = parse_ydk(text)
            ydk_out[slug] = {"ydk": text, "ydke": ydke(p)}

            want = {}                               # (section, name) -> copies, from the .ydk
            types = {}
            for sec in ("main", "extra"):
                for pw in p[sec]:
                    r = resolve(pw)
                    if not r: continue
                    name, t = r
                    s = section_of(t) if sec == "main" else "extra"
                    if sec == "main" and s == "extra": error(f"{slug}: {name} is an Extra Deck card listed under #main")
                    if sec == "extra" and section_of(t) != "extra": error(f"{slug}: {name} is a Main Deck card listed under #extra")
                    want[(s, name)] = want.get((s, name), 0) + 1; types[name] = t
            old = decklists.get(slug, {"monsters": [], "spells": [], "traps": [], "extra": []})
            have = {(g, c["n"]): c["q"] for g in old for c in old[g]}
            if want and want != have:
                new = {g: [] for g in ("monsters", "spells", "traps", "extra")}
                for g in new:                       # keep the existing order, update copies, drop removed cards
                    for c in old.get(g, []):
                        k = (g, c["n"])
                        if want.get(k): new[g].append({"q": want[k], "n": c["n"], "t": c["t"]})
                for (g, name), q in want.items():   # new cards go at the end of their section
                    if (g, name) not in have: new[g].append({"q": q, "n": name, "t": types[name]})
                added = sorted(n for (g, n) in want if (g, n) not in have)
                removed = sorted(n for (g, n) in have if (g, n) not in want)
                recount = sorted(n for k, n in [(k, k[1]) for k in want] if k in have and have[k] != want[k])
                changes.append(f"{deck['name']}: list rebuilt from ydk/{slug}.ydk"
                               + (f"; added {', '.join(added)}" if added else "")
                               + (f"; removed {', '.join(removed)}" if removed else "")
                               + (f"; new copy counts for {', '.join(recount)}" if recount else ""))
                decklists[slug] = new
                deck["updated"] = TODAY

            dl = decklists[slug]
            # card text for the viewer: keep in step with the list
            names = [c["n"] for g in dl.values() for c in g]
            cd = cards.setdefault(slug, {})
            for name in list(cd):
                if name not in names: del cd[name]
            for name in names:
                if name in cd: continue
                if name in all_entries:
                    e = json.loads(json.dumps(all_entries[name]))
                    e["n"] = [n for n in e.get("n", []) if n.get("l") == "All decks"]
                    cd[name] = e
                else:
                    try:
                        cd[name] = card_entry_from_api(api_card(name=name))
                        warn(f"{deck['name']}: card text for {name} was imported from YGOPRODeck; check it against Konami's database")
                    except Exception as ex:
                        warn(f"{deck['name']}: no card text for {name} ({ex}); its entry in the decklist viewer will be empty")
                    all_entries.setdefault(name, cd.get(name))
            cards[slug] = {n: cd[n] for n in names if n in cd}   # same order as the list

            # checks
            main = sum(c["q"] for g in ("monsters", "spells", "traps") for c in dl[g]); extra = sum(c["q"] for c in dl["extra"])
            if not 40 <= main <= 60: error(f"{deck['name']}: Main Deck has {main} cards (must be 40-60)")
            if extra > 15: error(f"{deck['name']}: Extra Deck has {extra} cards (max 15)")
            if len(p["side"]) > 15: error(f"{deck['name']}: Side Deck has {len(p['side'])} cards (max 15)")
            per = {}
            for g in dl.values():
                for c in g: per[c["n"]] = per.get(c["n"], 0) + c["q"]
            limit = {"Forbidden": 0, "Limited": 1, "Semi-Limited": 2}
            for name, n in per.items():
                if n > 3: error(f"{deck['name']}: {n} copies of {name} (max 3)")
                fl = cards[slug].get(name, {}).get("fl", "Unlimited")
                if n > limit.get(fl, 3):
                    warn(f"{deck['name']}: runs {n}× {name}, which is {fl} in the TCG (per the card text file; confirm against the current Forbidden & Limited List)")
            kc = {}
            for k in deck.get("keyCards", []): kc[k] = kc.get(k, 0) + 1
            for k, n in kc.items():
                if per.get(k, 0) < n: warn(f"{deck['name']}: best opening hand shows {n}× {k}, but the deck runs {per.get(k, 0)}")
            a = analysis["decks"].get(slug)
            if not a: error(f"{deck['name']}: no entry in content/analysis.json"); continue
            roles = a["roles"]; inrole = set(roles["starters"]) | set(roles["pieces"]) | set(roles["interaction"])
            fresh = [n for g in ("monsters", "spells", "traps") for c in dl[g] for n in [c["n"]] if n not in inrole and deck["updated"] > analysis["asOf"]]
            if fresh: warn(f"{deck['name']}: not counted as starter, engine piece or interaction: {', '.join(fresh)}")
            if deck["updated"] > analysis["asOf"]:
                warn(f"{deck['name']}: list updated {deck['updated']}, after the collection report ({analysis['asOf']}); review its ratings, role lists, breaker count and verdict text")
            gd = guide_rects.get(slug, {}).get("r", {})
            missing = [n for n in names if gd and n not in gd]
            if missing: warn(f"{deck['name']}: the visual guide photo has no position for {', '.join(missing)} (retake the card photo to update it)")

    # derived numbers
    collections = json.loads(json.dumps(decks["collections"]))
    for col in collections:
        for d in col["decks"]:
            dl = decklists[d["slug"]]
            d["main"] = sum(c["q"] for g in ("monsters", "spells", "traps") for c in dl[g])
            d["extra"] = sum(c["q"] for c in dl["extra"])
            d["breakdown"] = breakdown(dl)
    w = analysis["weights"]
    page_analysis = json.loads(json.dumps(analysis))
    names_by_slug = {d["slug"]: d["name"] for col in decks["collections"] for d in col["decks"]}
    for slug, a in page_analysis["decks"].items():
        a["overall"] = round(sum(w[k] * a[k] for k in w), 2)
        a["odds"] = odds(decklists[slug], a.pop("roles"), names_by_slug.get(slug, slug))
    prev = {s: i for i, s in enumerate(analysis.get("rank", []))}
    page_analysis["rank"] = sorted(page_analysis["decks"], key=lambda s: (-page_analysis["decks"][s]["overall"], prev.get(s, 99)))
    for i, s in enumerate(page_analysis["rank"]): page_analysis["decks"][s]["rank"] = i + 1
    analysis["rank"] = page_analysis["rank"]
    for s in analysis["decks"]:
        analysis["decks"][s]["rank"] = page_analysis["decks"][s]["rank"]
        analysis["decks"][s]["overall"] = page_analysis["decks"][s]["overall"]

    # write sources back (only if something changed)
    save("content/decklists.json", decklists, indent=1); save("content/decks.json", decks)
    save("content/analysis.json", analysis); save("content/passcode-aliases.json", aliases)
    if save("data/cards.json", cards, indent=None): changes.append("card text file updated")

    # render the page
    js = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    page = open(P("build/template.html"), encoding="utf8").read()
    for key, val in {"COLLECTIONS": js(collections), "DECKLISTS": js({s: decklists[s] for s in slugs}),
                     "YDK": js(dict(sorted(ydk_out.items()))), "NOTES": js(notes), "ANALYSIS": js(page_analysis),
                     "DATA_GUIDES": open(P("data/guides.json"), encoding="utf8").read().strip().replace("</", "<\\/"),
                     "DATA_CARDS": open(P("data/cards.json"), encoding="utf8").read().strip().replace("</", "<\\/")}.items():
        marker = f"/*@{key}@*/null" if key not in ("DATA_GUIDES", "DATA_CARDS") else f"/*@{key}@*/"
        if page.count(marker) != 1: error(f"template marker {marker} found {page.count(marker)} times"); continue
        page = page.replace(marker, val)
    if not errors:
        with open(P("index.html"), "w", encoding="utf8", newline="\n") as f: f.write(page)
    return page

# ---------------------------------------------------------------- web images
# The live site loads smaller copies of the photos from web/ (the offline zip keeps the full-quality originals).
WEB_SCALES = (("art", 0.85), ("cards", 0.6))
def web_path(folder, f): return P("web", folder, os.path.splitext(f)[0] + ".webp")

def web_images():
    """Write web/<folder>/<name>.webp for every photo whose source changed since the last build."""
    try:
        from PIL import Image
    except ImportError:
        error("the photo step needs Pillow: pip install Pillow"); return
    manifest_path = "web/sources.json"
    manifest = load(manifest_path) if os.path.exists(P(manifest_path)) else {}
    fresh, made = {}, []
    for folder, scale in WEB_SCALES:
        os.makedirs(P("web", folder), exist_ok=True)
        for f in sorted(os.listdir(P(folder))):
            src = f"{folder}/{f}"
            digest = hashlib.sha1(open(P(folder, f), "rb").read()).hexdigest()
            fresh[src] = digest
            if manifest.get(src) == digest and os.path.exists(web_path(folder, f)): continue
            im = Image.open(P(folder, f)); im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
            im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
            im.save(web_path(folder, f), "WEBP", quality=68, method=6)
            made.append(src)
        keep = {os.path.basename(web_path(folder, f)) for f in os.listdir(P(folder))}
        for f in os.listdir(P("web", folder)):            # photos whose source was deleted
            if f not in keep: os.remove(P("web", folder, f)); made.append(f"web/{folder}/{f} (removed)")
    save(manifest_path, fresh)
    if made: changes.append(f"web photos updated: {', '.join(made)}")

# ---------------------------------------------------------------- offline copies
def build_dist(page, out):
    os.makedirs(out, exist_ok=True)
    # the offline copies load the original photos (package) or embedded ones (standalone), not web/
    flag = "var WEB_IMAGES = true;"
    if page.count(flag) != 1: raise SystemExit("offline copies: could not find the WEB_IMAGES switch")
    page = page.replace(flag, "var WEB_IMAGES = false;")
    # 1. package: the site folder as a zip
    top = "The Vault offline package (local host)/The Vault/"
    zpath = os.path.join(out, "The Vault offline package (local host).zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(top, "")
        z.writestr(top + "index.html", page)
        z.write(P("build/README-offline.txt"), top + "README.txt")
        for folder in ("art", "cards", "fonts", "ydk"):
            for f in sorted(os.listdir(P(folder))): z.write(P(folder, f), top + folder + "/" + f)
        for f in ("cards.json", "guides.json"): z.write(P("data", f), top + "data/" + f)
    # 2. standalone: one file, fonts and smaller photos built in
    def font(m):
        b = open(P(m.group(1)), "rb").read()
        return "url(data:font/woff2;base64," + base64.b64encode(b).decode() + ")"
    st = re.sub(r"url\((fonts/[^)]+\.woff2)\)", font, page)
    blocks = []
    for folder, _ in WEB_SCALES:                         # same smaller photos as the live site
        for f in sorted(os.listdir(P(folder))):
            b = open(web_path(folder, f), "rb").read()
            blocks.append(f'<script type="text/plain" id="img:{folder}/{f}" data-type="image/webp">{base64.b64encode(b).decode()}</script>')
    anchor = "<script>\n/* Offline build"
    if st.count(anchor) != 1: raise SystemExit("standalone: could not find where to insert the photos")
    st = st.replace(anchor, "\n".join(blocks) + "\n" + anchor)
    with open(os.path.join(out, "The Vault offline (standalone).html"), "w", encoding="utf8", newline="\n") as f: f.write(st)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", help="also build the offline copies into this folder")
    ap.add_argument("--offline-ok", action="store_true", help="don't fail when YGOPRODeck can't be reached")
    a = ap.parse_args()
    web_images()
    page = build(a.offline_ok)
    report = []
    if changes: report += ["## Changes", *[f"- {c}" for c in changes]]
    if warnings: report += ["## Needs a look", *[f"- {w}" for w in warnings]]
    if errors: report += ["## Errors (site not rebuilt)", *[f"- {e}" for e in errors]]
    if not report: report = ["Everything is in step: no deck lists changed."]
    print("\n".join(report))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf8") as f: f.write("# The Vault build\n" + "\n".join(report) + "\n")
    if errors: sys.exit(1)
    if a.dist: build_dist(page, a.dist); print(f"Offline copies written to {a.dist}")

if __name__ == "__main__":
    main()
