"""
Steam deal finder: IsThereAnyDeal (discovery + store prices) + Steam (reviews) -> gg.deals (keyshops + historical lows) -> Discord

Usage:
  python find_deals.py                       # top deals, default filters
  python find_deals.py --min-rating 90 --min-reviews 5000 --max-price 15
  python find_deals.py --pages 5 --csv out.csv  # 200 deals per page
  python find_deals.py --ids 1245620 413150  # check specific Steam AppIDs
  python find_deals.py --wishlist            # price-check your Steam wishlist
  python find_deals.py --buy-now 20          # only rows within 20% of historical low
  python find_deals.py --min-score 2         # quality floor (see README)
  python find_deals.py --include-owned       # don't hide games already in your library
  python find_deals.py --wishlist --discord  # post results to a Discord webhook
  python find_deals.py --discord --state history/posted.json  # only post deals not announced before
  python find_deals.py --steam-keys          # store price only from shops selling Steam keys (no GOG/Epic/Ubisoft)
  python find_deals.py --gg-cache history/gg_cache.json  # reuse gg.deals prices; fetch at most 300 IDs per run
  python find_deals.py --bundles             # also list discounted Steam bundles/editions that include each game

Env (.env or environment): ITAD_API_KEY, GGDEALS_API_KEY (required); STEAM_API_KEY, STEAM_ID (steam64); DISCORD_WEBHOOK.
"""
import argparse, csv, datetime, glob, json, math, os, sys, time, urllib.error, urllib.parse, urllib.request

UA = "ggdeals-finder/1.0 (personal use)"
ITAD = "https://api.isthereanydeal.com"
GG = "https://api.gg.deals/v1/prices/by-steam-app-id/"

def load_env():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def get(url, params, retries=6, body=None, headers=None):
    """GET (or POST `body` as JSON) and parse the JSON reply. A 429 waits out its Retry-After, or fails at once if
    that is over 5 minutes - retrying into a long ban only extends it."""
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"User-Agent": UA, **({"Content-Type": "application/json"} if data else {}), **(headers or {})}
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), data=data, headers=hdrs)
    for attempt in range(retries + 1):
        backoff = min(30 * 2 ** attempt, 300)             # 30s..5min, ~17min total: ride out short outages
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if (e.code != 429 and e.code < 500) or attempt == retries: raise
            ra = e.headers.get("Retry-After", "") if e.code == 429 else ""
            if ra.isdigit() and int(ra) > 300:
                print(f"429 from {url.split('/')[2]}: blocked for {ra}s; giving up", file=sys.stderr); raise
            wait = (int(ra) + 1 if ra.isdigit() else 65 * (attempt + 1)) if e.code == 429 else backoff
            print(f"{e.code} from {url.split('/')[2]}; waiting {wait}s", file=sys.stderr); time.sleep(wait)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries: raise
            print(f"{e} from {url.split('/')[2]}; waiting {backoff}s", file=sys.stderr); time.sleep(backoff)

def f(x):
    try: return float(x) if x not in (None, "") else None
    except ValueError: return None

# ITAD shops that sell Steam keys; leaves out GOG, Epic, Ubisoft, EA, Microsoft and the other launchers
STEAM_KEY_SHOPS = {61: "Steam", 24: "GamersGate", 36: "GreenManGaming", 37: "Humble", 6: "Fanatical", 64: "WinGameStore",
                   20: "GameBillet", 29: "Gamesplanet", 25: "Gamesload", 42: "IndieGala", 15: "DreamGame"}

def itad(path, key, params=None, body=None):
    return get(ITAD + path, {"country": "US", **(params or {})}, body=body, headers={"ITAD-API-Key": key})

def steam_info(appids):
    """{appid: (rating %, review count, release year)} from Steam's store, 50 games per call."""
    out = {}
    for it in steam_items([{"appid": int(a)} for a in appids], {"include_reviews": True, "include_release": True}):
        rv = (it.get("reviews") or {}).get("summary_filtered") or {}
        rel = (it.get("release") or {}).get("steam_release_date")
        out[str(it["appid"])] = (int(rv.get("percent_positive") or 0), int(rv.get("review_count") or 0),
                                 time.gmtime(rel).tm_year if rel else None)
    return out

def itad_candidates(key, pages, min_rating, min_reviews, max_price, shops=None):
    """Games on sale with a Steam key, one row per game. ITAD filters by Steam reviews and price; each row's
    `retail` is the cheapest current sale price among `shops` (all shops if None)."""
    filt = {"price": {"min": None, "max": max_price}, "steamPerc": {"min": min_rating, "max": 100},
            "steamCount": {"min": min_reviews, "max": None}, "drm": [61], "type": [1, 3]}   # 3 = package: ITAD files some games (e.g. Psychonauts 2) as packages
    deals, offset = [], 0
    for _ in range(pages):
        r = itad("/deals/v2", key, body={"country": "US", "limit": 200, "offset": offset, "filter": filt,
                                          **({"shops": shops} if shops else {})})
        deals += r["list"]
        if not r.get("hasMore"): break
        offset = r["nextOffset"]
    apps = {}
    for i in range(0, len(deals), 200):                   # ITAD game ID -> Steam app ID
        for gid, ids in itad("/lookup/shop/61/id/v1", key, body=[d["id"] for d in deals[i:i+200]]).items():
            app = next((x[4:] for x in ids or [] if x.startswith("app/")), None)
            if app: apps[gid] = app
    info = steam_info(sorted(set(apps.values())))
    out, seen = [], set()
    for d in deals:
        app = apps.get(d["id"])
        if not app or app in seen or app not in info: continue
        rating, reviews, year = info[app]
        if rating < min_rating or reviews < min_reviews: continue   # Steam's own numbers, fresher than ITAD's
        seen.add(app)
        out.append({"appid": app, "title": d["title"], "rating": rating, "reviews": reviews, "metacritic": 0,
                    "normal": d["deal"]["regular"]["amount"], "year": year,
                    "retail": (d["deal"]["price"]["amount"], d["deal"]["shop"]["name"])})
    return out

def itad_prices(key, appids, shops):
    """{appid: (price, shop, regular price)} - cheapest current price among `shops`, for --ids / --wishlist."""
    gids = {}
    for i in range(0, len(appids), 200):
        r = itad("/lookup/id/shop/61/v1", key, body=[f"app/{a}" for a in appids[i:i+200]])
        gids.update({v: k[4:] for k, v in r.items() if v})
    out, ids = {}, list(gids)
    for i in range(0, len(ids), 200):
        params = {"shops": ",".join(map(str, shops))} if shops else {}
        for g in itad("/games/prices/v3", key, params, body=ids[i:i+200]):
            ds = [d for d in g.get("deals", []) if d["price"]["amount"] > 0]
            if ds:
                d = min(ds, key=lambda d: d["price"]["amount"])
                out[gids[g["id"]]] = (d["price"]["amount"], d["shop"]["name"], d["regular"]["amount"])
    return out

def gg_prices(key, appids, cache_path=None, budget=None, max_age=12):
    """{appid: gg.deals entry}. The API allows 100 IDs a minute and 1000 an hour, so with a cache file
    at most `budget` IDs are fetched per run: uncached ones first, then the oldest; the rest reuse the cache."""
    cache = json.load(open(cache_path, encoding="utf-8")) if cache_path and os.path.exists(cache_path) else {}
    now = time.time()
    want = [a for a in appids if now - cache.get(a, {}).get("t", 0) > max_age * 3600]
    want.sort(key=lambda a: cache.get(a, {}).get("t", 0))  # never fetched (t=0) first, then oldest
    if budget is not None: want = want[:budget]
    for i in range(0, len(want), 100):                    # 100 IDs per call
        chunk = want[i:i+100]
        r = get(GG, {"ids": ",".join(chunk), "key": key})
        if not r.get("success"):
            sys.exit(f"gg.deals error: {r}")    # fail the run rather than post a silently partial list
        for a in chunk: cache[a] = {"t": now, "data": (r["data"] or {}).get(a)}
        if i + 100 < len(want): time.sleep(61)
    if cache_path:
        cutoff = now - 14 * 86400                         # drop games that left the candidate list
        cache = {a: v for a, v in cache.items() if v["t"] > cutoff}
        with open(cache_path, "w", encoding="utf-8") as fh:
            fh.write("{\n" + ",\n".join(f"{json.dumps(a)}: {json.dumps(cache[a], separators=(',', ':'))}" for a in sorted(cache)) + "\n}\n")
    print(f"gg.deals: fetched {len(want)}, cached {sum(a in cache for a in appids) - len(want)}", file=sys.stderr)
    return {a: cache[a]["data"] for a in appids if a in cache and cache[a]["data"]}

STEAM = "https://api.steampowered.com"
STATE_DAYS = 45  # forget a posted deal after this long so the next sale can announce it again

def steam_owned(key, sid):
    r = get(f"{STEAM}/IPlayerService/GetOwnedGames/v1/", {"key": key, "steamid": sid, "include_played_free_games": 1, "format": "json"})
    return {str(g["appid"]) for g in r["response"].get("games", [])}

def steam_wishlist(key, sid):
    r = get(f"{STEAM}/IWishlistService/GetWishlist/v1/", {"key": key, "steamid": sid})
    return [str(i["appid"]) for i in r["response"].get("items", [])]

def steam_items(ids, request):
    """Steam store items for ids like {"appid": 1} / {"bundleid": 2} / {"packageid": 3}, 50 per call."""
    out = []
    for i in range(0, len(ids), 50):
        body = {"ids": ids[i:i+50], "context": {"language": "english", "country_code": "US"}, "data_request": request}
        out += get(f"{STEAM}/IStoreBrowseService/GetItems/v1/", {"input_json": json.dumps(body)})["response"].get("store_items", [])
        time.sleep(0.3)
    return out

def steam_bundles(appids, owned, per_game=3):
    """{appid: [bundle, ...]} - discounted Steam bundles and editions that include each game, beyond the
    game's own cheapest package. Steam's prices are anonymous US ones; for bundles, `mine` redoes Steam's
    "complete the set" pricing - the bundle discount over only the games not in `owned` - which can come out
    cheaper than the game alone. Packages (editions) have a fixed price, owned games or not."""
    found = {}
    for it in steam_items([{"appid": int(a)} for a in appids], {"include_all_purchase_options": True}):
        opts = [o for o in it.get("purchase_options", []) if o.get("final_price_in_cents")]
        singles = [o for o in opts if o.get("packageid") and (o.get("included_game_count") or 1) <= 1]
        if not singles: continue
        base = min(singles, key=lambda o: int(o["final_price_in_cents"]))
        extra = [o for o in opts if o is not base and o.get("discount_pct")]
        if extra: found[str(it["appid"])] = (int(base["final_price_in_cents"]), extra)
    ids = [{"bundleid": o["bundleid"]} if o.get("bundleid") else {"packageid": o["packageid"]}
           for _, extra in found.values() for o in extra]
    contents = {}                                         # (item_type, id) -> {appid: current price in cents or None}
    for it in steam_items(ids, {"include_included_items": True}):
        prices = {str(a["appid"]): int((a.get("best_purchase_option") or {}).get("final_price_in_cents") or 0) or None
                  for a in it.get("included_items", {}).get("included_apps", [])}
        contents[(it["item_type"], it["id"])] = {**{str(a): None for a in it.get("included_appids", [])}, **prices}
    out = {}
    for app, (base, extra) in found.items():
        bs = []
        for o in extra:
            kind, oid = ("bundle", o["bundleid"]) if o.get("bundleid") else ("sub", o["packageid"])
            items = contents.get((2 if kind == "bundle" else 1, oid), {})
            mine = list_price = int(o["final_price_in_cents"])
            own = [a for a in items if a in owned]
            if kind == "bundle" and own and not o.get("must_purchase_as_set"):
                rest = [items[a] for a in items if a not in owned]
                if None not in rest:                      # every remaining game has a price to sum
                    mine = round(sum(rest) * (1 - (o.get("bundle_discount_pct") or 0) / 100))
            bs.append({"name": o["purchase_option_name"], "price": list_price / 100, "mine": mine / 100,
                       "pct": o["discount_pct"], "extra": (mine - base) / 100, "items": len(items), "own": len(own),
                       "url": f"https://store.steampowered.com/{kind}/{oid}"})
        out[app] = sorted(bs, key=lambda b: b["extra"])[:per_game]
    return out

def bundle_line(b):
    price = f"${b['price']:.2f} (-{b['pct']}%)"
    if b["mine"] != b["price"]:
        price = f"${b['mine']:.2f} for you (list ${b['price']:.2f}, -{b['pct']}%)"
    vs = f"+${b['extra']:.2f} over" if b["extra"] >= 0 else f"${-b['extra']:.2f} cheaper than"
    own = f", you own {b['own']}" if b["own"] else ""
    return f"{price} | {vs} the game alone | {b['items']} items{own}"

def discord_post(webhook, title, rows, bundles=None):
    """Post rows as a Discord embed (chunks to stay under limits)."""
    if not rows:
        lines = ["Nothing within range right now."]
    else:
        lines = []
        for t in ("BUY", "GOOD"):
            grp = [r for r in rows if r.get("tier", "GOOD") == t]
            if not grp: continue
            lines.append(f"__**{t}**__")
            for r in grp:
                vs = f"{(r['best']/r['low']-1)*100:+.0f}%" if r["low"] else "n/a"
                yr = f" ({r['year']})" if r.get("year") else ""
                tag = f" ({r['tag']})" if r.get("tag") else ""
                wish = " [wishlist]" if r.get("wish") else ""
                rpt = f" | seen in {r['repeats']} earlier sale{'s' * (r['repeats'] > 1)}" if r.get("repeats") else ""
                lines.append(f"**[{r['title'][:50]}]({r['url']})**{tag}{wish}{yr} - ${r['best']:.2f} ({r['src']}) | low ${r['low'] or 0:.2f} ({vs}) | {r['rating']}%{rpt}")
                for b in (bundles or {}).get(r["appid"], []):
                    if b["extra"] >= 0: continue          # only bundles that cost less than the game alone
                    lines.append(f" ↳ [{b['name'][:45]}]({b['url']}) - {bundle_line(b)}")
    desc, chunks = "", []
    for ln in lines:
        if len(desc) + len(ln) > 3900: chunks.append(desc); desc = ""
        desc += ln + chr(10)
    chunks.append(desc)
    for i, d in enumerate(chunks):
        body = {"embeds": [{"title": title if i == 0 else f"{title} (cont.)", "description": d, "color": 0x1b9e77,
                            "footer": {"text": "Prices via gg.deals"}}]}
        req = urllib.request.Request(webhook, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": UA})
        urllib.request.urlopen(req, timeout=30).read()
        time.sleep(0.5)

def suspect(r):
    # >10% under the recorded low: a real new low or a bad listing - can't tell, so never BUY on it
    return bool(r["low"]) and r["best"] < r["low"] * 0.9

def tier(r):
    # BUY = store price within 3% of its historical low on a well-reviewed game, or a wishlisted game
    # at its low from any source. Keyshop-only deals and suspect lows stay GOOD.
    vs = r["best"] / r["low"] - 1 if r["low"] else 1
    if vs > 0.03 or suspect(r): return "GOOD"
    if r.get("wish") or (r["src"] == "store" and r["score"] >= 2.5 and r["reviews"] >= 3000):
        return "BUY"
    return "GOOD"

def price_history(folder):
    """{appid: [(date, best), ...]} from the dated daily CSVs (YYYY-MM-DD.csv) in folder."""
    hist = {}
    for p in sorted(glob.glob(os.path.join(folder, "20??-??-??.csv"))):
        day = os.path.basename(p)[:-4]
        for r in csv.DictReader(open(p, encoding="utf-8")):
            b = f(r.get("best"))
            if b: hist.setdefault(r["appid"], []).append((day, b))
    return hist

def prior_sales(seen, price, today, gap=3):
    """How many earlier, finished sales got to this price (within 2%). A sale is a run of days the game was
    listed, allowing gaps of up to `gap` days (it can drop off the list for a day); the one still running
    (listed within `gap` days of today) doesn't count. A sale's price is its second-lowest day, so a
    one-day bogus listing inside a longer sale doesn't count."""
    today = datetime.date.fromisoformat(today)
    runs = []                                             # (last day, [prices]) per sale
    for d, b in sorted((datetime.date.fromisoformat(d), b) for d, b in seen):
        if d >= today: continue
        if runs and (d - runs[-1][0]).days <= gap: runs[-1] = (d, runs[-1][1] + [b])
        else: runs.append((d, [b]))
    if runs and (today - runs[-1][0]).days <= gap: runs.pop()   # the current sale
    return sum(sorted(p)[min(1, len(p) - 1)] <= price * 1.02 for _, p in runs)

def score(row):
    # Value = how close to historical low x quality x confidence in quality
    best, low = row["best"], row["low"]
    low_ratio = max(1.0, best / low) if (best and low) else 1.2  # 1.0 = at low (below-low capped); unknown: mild neutral
    discount = 1 - best / row["normal"] if row["normal"] else 0
    quality = (row["rating"] / 100) ** 3 * math.log10(max(row["reviews"], 10))
    proximity = max(0, 1.6 - low_ratio)                                   # 0.6 at low, 0 if 60% above it
    return round(quality * (proximity + 0.5 * discount), 3)

def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=20, help="ITAD deal pages (200/page)")
    ap.add_argument("--min-rating", type=int, default=85)
    ap.add_argument("--min-reviews", type=int, default=1000)
    ap.add_argument("--max-price", type=float, default=30)
    ap.add_argument("--top", type=int, default=15, help="Show/post the top N GOOD rows; BUY rows are always included")
    ap.add_argument("--ids", nargs="*", help="Specific Steam AppIDs instead of discovery")
    ap.add_argument("--csv")
    ap.add_argument("--include-owned", action="store_true", help="Don't filter out games in your Steam library")
    ap.add_argument("--wishlist", action="store_true", help="Check your Steam wishlist instead of discovery")
    ap.add_argument("--history", metavar="DIR", help="Folder of daily CSVs; flags deals that already hit this price in earlier sales")
    ap.add_argument("--state", metavar="JSON", help="Posted-deals state file; with --discord, only post deals not announced before")
    ap.add_argument("--steam-keys", action="store_true", help="Store price only from shops selling Steam keys (ITAD); keyshops unchanged")
    ap.add_argument("--bundles", action="store_true", help="List discounted Steam bundles/editions that include each shown game")
    ap.add_argument("--gg-cache", metavar="JSON", help="Reuse gg.deals prices between runs; fetch at most --gg-budget IDs per run")
    ap.add_argument("--gg-budget", type=int, default=300, help="Max IDs fetched from gg.deals per run with --gg-cache (default 300)")
    ap.add_argument("--gg-max-age", type=float, default=12, metavar="HOURS", help="With --gg-cache, refetch prices older than this (default 12)")
    ap.add_argument("--discord", action="store_true", help="Post results to DISCORD_WEBHOOK from .env")
    ap.add_argument("--min-score", type=float, default=0, help="Drop rows below this score (2.0 = good game near its low)")
    ap.add_argument("--buy-now", type=float, default=None, metavar="PCT", help="Only show rows within PCT%% of historical low (e.g. 20)")
    a = ap.parse_args()
    key, ikey = os.environ.get("GGDEALS_API_KEY"), os.environ.get("ITAD_API_KEY")
    if not (key and ikey): sys.exit("Set GGDEALS_API_KEY and ITAD_API_KEY in .env")
    shops = list(STEAM_KEY_SHOPS) if a.steam_keys else None

    skey, sid = os.environ.get("STEAM_API_KEY"), os.environ.get("STEAM_ID")
    owned = set()
    if skey and sid and not a.include_owned:
        owned = steam_owned(skey, sid)
        print(f"{len(owned)} owned games will be excluded", file=sys.stderr)
        if not owned: print("WARNING: no owned games returned - is the Steam profile's game details private?", file=sys.stderr)

    wish = set()
    if a.wishlist and not (skey and sid): sys.exit("Set STEAM_API_KEY and STEAM_ID in .env")
    if skey and sid:
        try: wish = set(steam_wishlist(skey, sid))
        except Exception as e: print(f"wishlist fetch failed: {e}", file=sys.stderr)
    if a.wishlist:
        a.ids = sorted(wish)
        print(f"{len(a.ids)} wishlist items", file=sys.stderr)
    if a.ids:
        cands = [{"appid": i, "title": "?", "rating": 0, "reviews": 0, "metacritic": 0, "normal": None, "year": None} for i in a.ids]
    else:
        cands = itad_candidates(ikey, a.pages, a.min_rating, a.min_reviews, a.max_price, shops)
        print(f"{len(cands)} candidates from IsThereAnyDeal; querying gg.deals...", file=sys.stderr)

    cands = [c for c in cands if c["appid"] not in owned]
    if a.ids:
        info, ip = steam_info([c["appid"] for c in cands]), itad_prices(ikey, [c["appid"] for c in cands], shops)
        for c in cands:
            c["rating"], c["reviews"], c["year"] = info.get(c["appid"], (0, 0, None))
            if c["appid"] in ip: c["retail"], c["normal"] = ip[c["appid"]][:2], ip[c["appid"]][2]
    prices = gg_prices(key, [c["appid"] for c in cands], a.gg_cache, a.gg_budget if a.gg_cache else None, a.gg_max_age)
    rows = []
    for c in cands:
        sk = c.pop("retail", None)
        p = prices.get(c["appid"])
        if not p: continue
        pr = p["prices"]
        # 0.00 = free/unlisted now, or was free once historically; ignore either way
        retail, keyshop, lo_r, lo_k = (x if x and x > 0 else None for x in (f(pr["currentRetail"]), f(pr["currentKeyshops"]),
                                                                            f(pr["historicalRetail"]), f(pr["historicalKeyshops"])))
        if a.steam_keys:                          # gg.deals' store price may be GOG/Epic; use the Steam-key shops' instead
            retail = sk[0] if sk else None
        if retail is None and keyshop is None: continue
        if retail is not None and (keyshop is None or keyshop > retail - max(0.5, 0.1 * retail)):
            best, src = retail, "store"          # keyshop must beat store by >10% (min $0.50) to be worth the risk
        else:
            best, src = keyshop, "keyshop"
        low = min([x for x in (lo_r, lo_k) if x] or [None])
        row = {**c, "title": p["title"], "retail": retail, "keyshop": keyshop, "best": best, "src": src,
               "low": low, "url": p["url"], "wish": c["appid"] in wish}
        row["score"] = score(row)
        row["tier"] = tier(row)
        rows.append(row)
    if a.buy_now is not None:
        rows = [r for r in rows if r["low"] and r["best"] / r["low"] - 1 <= a.buy_now / 100]
    rows = [r for r in rows if r["score"] >= a.min_score]
    rows.sort(key=lambda r: -r["score"])
    if a.history:
        hist, today = price_history(a.history), time.strftime("%Y-%m-%d", time.gmtime())
        for r in rows: r["repeats"] = prior_sales(hist.get(r["appid"], []), r["best"], today)
    # --top caps only the GOOD rows: a BUY ranked below the top N by score still gets shown and posted
    good = {r["appid"] for r in [r for r in rows if r["tier"] != "BUY"][:a.top]}
    shown = [r for r in rows if r["tier"] == "BUY" or r["appid"] in good]
    bundles = {}
    if a.bundles and shown:
        try: bundles = steam_bundles([r["appid"] for r in shown], owned)
        except Exception as e: print(f"bundle lookup failed: {e}", file=sys.stderr)

    hdr = f"{'Score':>6} {'Best':>7} {'Src':7} {'Store':>7} {'Key':>7} {'Low':>7} {'vsLow':>6} {'Rate':>4} {'Revs':>7} {'Year':>4} {'Tier':4} {'Rpt':>3}  Title"
    print(hdr); print("-" * len(hdr))
    for r in shown:
        vs = f"{(r['best']/r['low']-1)*100:+.0f}%" if r["low"] else "  n/a"
        print(f"{r['score']:>6.2f} {r['best']:>7.2f} {r['src']:7} {r['retail'] or 0:>7.2f} {r['keyshop'] or 0:>7.2f} "
              f"{(r['low'] or 0):>7.2f} {vs:>6} {r['rating']:>4} {r['reviews']:>7} {r['year'] or '-':>4} {r['tier']:4} {r.get('repeats', '-'):>3}  {r['title'][:40]}")
        for b in bundles.get(r["appid"], []):
            print(f"{'':>8}+ {b['name'][:40]} - {bundle_line(b)}")
    print("\nPrices via gg.deals (https://gg.deals) - Src=keyshop means grey-market may be cheapest; check seller.")

    if a.discord:
        hook = os.environ.get("DISCORD_WEBHOOK")
        if not hook: sys.exit("Set DISCORD_WEBHOOK in .env")
        post = shown
        label = "Wishlist" if a.wishlist else "Top Steam deals"
        if a.state:
            # Remember what was announced and at what price: a deal is posted once, then again only when it
            # is >=5% cheaper than when last posted. Entries expire so a later sale can announce it again.
            today = time.strftime("%Y-%m-%d", time.gmtime())
            cutoff = time.strftime("%Y-%m-%d", time.gmtime(time.time() - STATE_DAYS * 86400))
            state = json.load(open(a.state, encoding="utf-8")) if os.path.exists(a.state) else {}
            state = {k: v for k, v in state.items() if v["date"] >= cutoff}
            kept = []
            for r in post:
                old = state.get(r["appid"], {}).get("best")
                if old is None:
                    kept.append(r)                                      # not announced recently
                elif r["best"] <= old * 0.95:                           # >=5% cheaper than last announced
                    tag = "NEW LOW" if r["low"] and r["best"] <= r["low"] else f"drop from ${old:.2f}"
                    kept.append({**r, "tag": tag})
            post = kept
            label = f"New deals ({len(post)}) - {len(shown) - len(post)} still on sale from before"
        else:
            label = f"{label} ({len(post)})"
        if a.state and not post:
            print("Nothing new; not posting to Discord")   # everything listed was already announced
        else:
            discord_post(hook, label, post, bundles)
            print("Posted to Discord")
        if a.state:
            for r in post: state[r["appid"]] = {"best": r["best"], "date": today, "title": r["title"]}
            with open(a.state, "w", encoding="utf-8") as fh: json.dump(state, fh, indent=1, sort_keys=True)

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8") as fh:
            if rows: w = csv.DictWriter(fh, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
        print(f"Wrote {a.csv}")

if __name__ == "__main__":
    main()
