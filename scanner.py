#!/usr/bin/env python3
"""Football in-play PAPER betting scanner. No real money is ever staked.

Every run (scheduled by GitHub Actions):
  1. Settles open paper bets whose games should be finished.
  2. Scans live games with live odds, and logs a 5 EUR paper bet on
     "another goal" (Over current total + 0.5) when all rules match.
Run `python scanner.py report` to print the profit/loss summary.
"""
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ----------------------------- SETTINGS (edit here) -----------------------------
STAKE = 5.0                    # paper stake in EUR
MIN_ODDS = 2.00                # minimum odds of the "another goal" bet
MINUTE_FROM, MINUTE_TO = 76, 86  # "around minute 80"
MIN_TOTAL_SHOTS = 20           # both teams together ...
MIN_SHOTS_ON_TARGET = 6        # ... OR this many shots on target (both teams)
DAILY_LIMIT = 90               # stop at this many API requests per day (free plan = 100)
SCORES = {(0, 0), (1, 1), (2, 2)}
MARKETS = {"Over/Under Line"}  # market name used by the live odds feed
MAX_ODDS_AGE_SEC = 300         # ignore odds older than this
SETTLE_AFTER_MIN = 35          # check a bet's result this long after logging it

LEAGUES = {
    2: "Champions League", 3: "Europa League", 88: "Eredivisie",
    39: "Premier League", 40: "Championship", 140: "La Liga", 141: "La Liga 2",
    78: "Bundesliga", 79: "2. Bundesliga", 135: "Serie A", 136: "Serie B",
    61: "Ligue 1", 144: "Jupiler Pro League", 94: "Primeira Liga",
    203: "Super Lig", 119: "Superliga (DK)", 113: "Allsvenskan",
}
# --------------------------------------------------------------------------------

BASE = "https://v3.football.api-sports.io"
BETS_FILE, USAGE_FILE = "bets.csv", "usage.json"
FIELDS = ["fixture_id", "logged_at_utc", "league", "home", "away", "minute",
          "score", "goals_at_bet", "market", "line", "odds", "total_shots",
          "shots_on_target", "corners", "stake", "status", "final_goals", "profit"]


def now():
    return datetime.now(timezone.utc)


def load_usage():
    today = now().strftime("%Y-%m-%d")
    try:
        with open(USAGE_FILE) as f:
            u = json.load(f)
    except Exception:
        u = {}
    if u.get("date") != today:
        u = {"date": today, "count": 0, "checked": []}
    return u


def save_usage(u):
    with open(USAGE_FILE, "w") as f:
        json.dump(u, f)


def read_bets():
    if not os.path.exists(BETS_FILE):
        return []
    with open(BETS_FILE, newline="") as f:
        return list(csv.DictReader(f))


def write_bets(bets):
    with open(BETS_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for b in bets:
            w.writerow({k: b.get(k, "") for k in FIELDS})


def api(usage, endpoint, **params):
    """One API call. Returns the 'response' list, or None on any problem."""
    if usage["count"] >= DAILY_LIMIT:
        print("Daily request limit reached; stopping.")
        return None
    usage["count"] += 1
    url = f"{BASE}/{endpoint}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"x-apisports-key": os.environ["API_FOOTBALL_KEY"]})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
    except Exception as e:
        print("Request failed:", e)
        return None
    if data.get("errors"):
        print("API errors:", data["errors"])
        return None
    return data.get("response", [])


def stat(team, name):
    for s in team.get("statistics", []):
        if s.get("type") == name:
            try:
                return int(str(s.get("value")).rstrip("%"))
            except ValueError:
                return 0
    return 0


def find_over(odds, line):
    for m in odds or []:
        if m.get("name") in MARKETS:
            for v in m.get("values", []):
                try:
                    if (v.get("value") == "Over" and float(v.get("handicap")) == line
                            and not v.get("suspended")):
                        return m["name"], float(v["odd"])
                except (TypeError, ValueError):
                    continue
    return None


def scan(usage, bets):
    resp = api(usage, "odds/live")
    if resp is None:
        return
    logged = {b["fixture_id"] for b in bets}
    checked = set(usage["checked"])
    for g in resp:
        fid = str(g["fixture"]["id"])
        if fid in logged or fid in checked or g["league"]["id"] not in LEAGUES:
            continue
        minute = g["fixture"]["status"].get("elapsed")
        h, a = g["teams"]["home"].get("goals"), g["teams"]["away"].get("goals")
        if minute is None or h is None or a is None:
            continue
        if not (MINUTE_FROM <= minute <= MINUTE_TO) or (h, a) not in SCORES:
            continue
        st = g.get("status", {})
        if st.get("stopped") or st.get("blocked") or st.get("finished"):
            continue
        try:
            if (now() - datetime.fromisoformat(g["update"])).total_seconds() > MAX_ODDS_AGE_SEC:
                continue
        except Exception:
            pass
        total = h + a
        pick = find_over(g.get("odds"), total + 0.5)
        if not pick or pick[1] < MIN_ODDS:
            continue
        stats = api(usage, "fixtures/statistics", fixture=fid)
        if stats is None:
            continue
        usage["checked"].append(fid)  # never spend a stats request on this game again
        shots = sum(stat(t, "Total Shots") for t in stats)
        sot = sum(stat(t, "Shots on Goal") for t in stats)
        corners = sum(stat(t, "Corner Kicks") for t in stats)
        if not (shots >= MIN_TOTAL_SHOTS or sot >= MIN_SHOTS_ON_TARGET):
            continue
        bets.append({
            "fixture_id": fid, "logged_at_utc": now().strftime("%Y-%m-%d %H:%M:%S"),
            "league": LEAGUES[g["league"]["id"]], "minute": minute, "score": f"{h}-{a}",
            "goals_at_bet": total, "market": pick[0], "line": total + 0.5, "odds": pick[1],
            "total_shots": shots, "shots_on_target": sot, "corners": corners,
            "stake": STAKE, "status": "OPEN",
        })
        print(f"PAPER BET logged: fixture {fid}, {h}-{a} min {minute}, odds {pick[1]}")


def settle(usage, bets):
    cutoff = now() - timedelta(minutes=SETTLE_AFTER_MIN)
    due = [b for b in bets if b["status"] == "OPEN"
           and datetime.strptime(b["logged_at_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) < cutoff]
    if not due:
        return
    due = due[:20]  # one request can look up up to 20 games
    resp = api(usage, "fixtures", ids="-".join(b["fixture_id"] for b in due))
    if resp is None:
        return
    by_id = {str(g["fixture"]["id"]): g for g in resp}
    for b in due:
        g = by_id.get(b["fixture_id"])
        if not g:
            continue
        b["home"], b["away"] = g["teams"]["home"]["name"], g["teams"]["away"]["name"]
        short = g["fixture"]["status"]["short"]
        if short in ("ABD", "CANC", "AWD", "WO"):
            b["status"], b["profit"] = "VOID", 0
        elif short in ("FT", "AET", "PEN"):
            ft = g["score"]["fulltime"]  # 90-minute score
            final = (ft["home"] or 0) + (ft["away"] or 0)
            b["final_goals"] = final
            if final > int(b["goals_at_bet"]):
                b["status"] = "WON"
                b["profit"] = round(float(b["stake"]) * (float(b["odds"]) - 1), 2)
            else:
                b["status"] = "LOST"
                b["profit"] = -float(b["stake"])


def breakdown(title, rows, keyfn):
    groups = {}
    for r in rows:
        groups.setdefault(keyfn(r), []).append(r)
    print(f"\n{title}")
    for k in sorted(groups):
        g = groups[k]
        w = sum(r["status"] == "WON" for r in g)
        p = sum(float(r["profit"]) for r in g)
        print(f"  {k:<22} bets {len(g):>3}  won {w:>3} ({100 * w / len(g):.0f}%)  profit {p:+.2f}")


def report(bets):
    done = [b for b in bets if b["status"] in ("WON", "LOST")]
    open_n = sum(b["status"] == "OPEN" for b in bets)
    print(f"Paper bets: {len(bets)} total, {len(done)} settled, {open_n} still open")
    if not done:
        return
    staked = sum(float(b["stake"]) for b in done)
    profit = sum(float(b["profit"]) for b in done)
    wins = sum(b["status"] == "WON" for b in done)
    print(f"Won {wins} / lost {len(done) - wins}  (hit rate {100 * wins / len(done):.0f}%)")
    print(f"Total staked {staked:.2f} EUR | returned {staked + profit:.2f} EUR | "
          f"PROFIT {profit:+.2f} EUR | ROI {100 * profit / staked:+.1f}%")
    breakdown("By league", done, lambda r: r["league"])
    breakdown("By score at bet", done, lambda r: r["score"])
    breakdown("By odds", done, lambda r: "2.00-2.29" if float(r["odds"]) < 2.3
              else "2.30-2.69" if float(r["odds"]) < 2.7 else "2.70+")
    breakdown("By total shots", done, lambda r: "<20" if int(r["total_shots"]) < 20
              else "20-24" if int(r["total_shots"]) < 25 else "25+")
    breakdown("By shots on target", done, lambda r: "<6" if int(r["shots_on_target"]) < 6
              else "6-8" if int(r["shots_on_target"]) < 9 else "9+")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report(read_bets())
        sys.exit()
    usage, bets = load_usage(), read_bets()
    try:
        settle(usage, bets)
        scan(usage, bets)
    finally:
        write_bets(bets)
        save_usage(usage)
        print(f"Requests used today: {usage['count']}")
