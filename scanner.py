#!/usr/bin/env python3
"""Football in-play PAPER betting scanner. No real money is ever staked.

Every run (scheduled by GitHub Actions):
  1. Settles open rows whose games should be finished.
  2. Scans live games with live odds. Games with the right minute, score and
     odds (Over current goals + 0.5) are logged:
       qualified = YES -> also enough shots: this is a paper bet (5 EUR)
       qualified = NO  -> "watch" row, NOT counted in the profit; it is settled
                          anyway so you can see if the shot rule is too strict.
  3. Appends one line to funnel.csv showing where games dropped out.
Run `python scanner.py report` to print the summary.
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
MINUTE_FROM, MINUTE_TO = 70, 90  # "around minute 80" (wide window, so late runs still catch games)
MIN_TOTAL_SHOTS = 20           # both teams together ...
MIN_SHOTS_ON_TARGET = 6        # ... OR this many shots on target (both teams)
RECHECK_AFTER_MIN = 4          # a not-qualified game gets ONE more stats check this many minutes later
DAILY_LIMIT = 90               # stop at this many API requests per day (free plan = 100)
SCORES = {(0, 0), (1, 1), (2, 2)}
MARKETS = {"Over/Under Line"}  # market name used by the live odds feed
MAX_ODDS_AGE_SEC = 300         # ignore odds older than this
SETTLE_AFTER_MIN = 35          # check a result this long after logging

LEAGUES = {
    2: "Champions League", 3: "Europa League", 88: "Eredivisie",
    39: "Premier League", 40: "Championship", 140: "La Liga", 141: "La Liga 2",
    78: "Bundesliga", 79: "2. Bundesliga", 135: "Serie A", 136: "Serie B",
    61: "Ligue 1", 144: "Jupiler Pro League", 94: "Primeira Liga",
    203: "Super Lig", 119: "Superliga (DK)", 113: "Allsvenskan",
}
# --------------------------------------------------------------------------------

BASE = "https://v3.football.api-sports.io"
BETS_FILE, USAGE_FILE, FUNNEL_FILE = "bets.csv", "usage.json", "funnel.csv"
FIELDS = ["fixture_id", "logged_at_utc", "league", "home", "away", "minute",
          "score", "goals_at_bet", "market", "line", "odds", "total_shots",
          "shots_on_target", "corners", "stake", "qualified", "status",
          "final_goals", "profit"]
FUNNEL_FIELDS = ["time_utc", "live_with_odds", "in_your_leagues", "in_minute_window",
                 "right_score", "odds_ok", "stats_checked", "new_bets", "new_watch_rows"]


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
        u = {"date": today, "count": 0}
    u.setdefault("rechecked", [])
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


def write_funnel(f):
    new = not os.path.exists(FUNNEL_FILE)
    with open(FUNNEL_FILE, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(FUNNEL_FIELDS)
        w.writerow([now().strftime("%Y-%m-%d %H:%M"), f["live"], f["leagues"], f["minute"],
                    f["score"], f["odds_ok"], f["stats"], f["bets"], f["watch"]])


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
    rows = {b["fixture_id"]: b for b in bets}
    f = dict(live=len(resp), leagues=0, minute=0, score=0, odds_ok=0, stats=0, bets=0, watch=0)
    try:
        for g in resp:
            fid = str(g["fixture"]["id"])
            if g["league"]["id"] not in LEAGUES:
                continue
            f["leagues"] += 1
            minute = g["fixture"]["status"].get("elapsed")
            h, a = g["teams"]["home"].get("goals"), g["teams"]["away"].get("goals")
            if minute is None or h is None or a is None or not (MINUTE_FROM <= minute <= MINUTE_TO):
                continue
            f["minute"] += 1
            if (h, a) not in SCORES:
                continue
            f["score"] += 1
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
            f["odds_ok"] += 1

            row = rows.get(fid)
            if row is not None:  # already logged: allow ONE re-check of a not-qualified game
                if not (row.get("qualified") == "NO" and row["status"] == "OPEN"
                        and fid not in usage["rechecked"]
                        and minute >= int(row["minute"]) + RECHECK_AFTER_MIN):
                    continue
                usage["rechecked"].append(fid)

            stats = api(usage, "fixtures/statistics", fixture=fid)
            if stats is None:
                continue
            f["stats"] += 1
            shots = sum(stat(t, "Total Shots") for t in stats)
            sot = sum(stat(t, "Shots on Goal") for t in stats)
            corners = sum(stat(t, "Corner Kicks") for t in stats)
            ok = shots >= MIN_TOTAL_SHOTS or sot >= MIN_SHOTS_ON_TARGET
            if row is not None and not ok:
                continue  # still not qualified: keep the first row as it was
            new = {
                "fixture_id": fid, "logged_at_utc": now().strftime("%Y-%m-%d %H:%M:%S"),
                "league": LEAGUES[g["league"]["id"]], "minute": minute, "score": f"{h}-{a}",
                "goals_at_bet": total, "market": pick[0], "line": total + 0.5, "odds": pick[1],
                "total_shots": shots, "shots_on_target": sot, "corners": corners,
                "stake": STAKE, "qualified": "YES" if ok else "NO", "status": "OPEN",
            }
            if row is not None:
                row.update(new)
            else:
                bets.append(new)
                rows[fid] = new
            f["bets" if ok else "watch"] += 1
            if ok:
                print(f"PAPER BET: fixture {fid}, {h}-{a} min {minute}, odds {pick[1]}, shots {shots}/{sot}")
    finally:
        write_funnel(f)
        print("Funnel:", f)


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
        print(f"  {k:<22} rows {len(g):>3}  won {w:>3} ({100 * w / len(g):.0f}%)  profit {p:+.2f}")


def band(v, edges):
    v = int(v)
    for lo, hi in zip(edges, edges[1:]):
        if lo <= v < hi:
            return f"{lo:02d}-{hi - 1:02d}"
    return f"{edges[-1]:02d}+"


def report(bets):
    is_bet = lambda b: b.get("qualified") != "NO"
    mine = [b for b in bets if is_bet(b)]
    watch = [b for b in bets if not is_bet(b)]
    done = [b for b in mine if b["status"] in ("WON", "LOST")]
    print(f"PAPER BETS: {len(mine)} logged, {len(done)} settled, "
          f"{sum(b['status'] == 'OPEN' for b in mine)} still open")
    print(f"WATCH ROWS (not bets, shots too low): {len(watch)}")
    if done:
        staked = sum(float(b["stake"]) for b in done)
        profit = sum(float(b["profit"]) for b in done)
        wins = sum(b["status"] == "WON" for b in done)
        print(f"Won {wins} / lost {len(done) - wins}  (hit rate {100 * wins / len(done):.0f}%)")
        print(f"Total staked {staked:.2f} EUR | returned {staked + profit:.2f} EUR | "
              f"PROFIT {profit:+.2f} EUR | ROI {100 * profit / staked:+.1f}%")
        breakdown("Bets by league", done, lambda r: r["league"])
        breakdown("Bets by score at bet", done, lambda r: r["score"])
        breakdown("Bets by odds", done, lambda r: "2.00-2.29" if float(r["odds"]) < 2.3
                  else "2.30-2.69" if float(r["odds"]) < 2.7 else "2.70+")
    every = [b for b in bets if b["status"] in ("WON", "LOST")]
    if every:
        print("\n=== WHAT-IF: all settled rows incl. watch rows (hypothetical bets) ===")
        breakdown("By total shots", every, lambda r: band(r["total_shots"], [0, 10, 15, 20, 25]))
        breakdown("By shots on target", every, lambda r: band(r["shots_on_target"], [0, 3, 6, 9]))
        breakdown("By minute at check", every, lambda r: band(r["minute"], [70, 76, 80, 84, 88]))


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
