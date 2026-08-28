#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FPL Copilot — darmowy fetcher danych.

Ciągnie prawdziwe dane z publicznego API FPL (bez klucza, bez logowania),
opcjonalnie pogodę z Open-Meteo, liczy heurystyczne xPts i zapisuje data.json,
który czyta statyczny frontend (index.html).

Uruchamiany lokalnie albo przez GitHub Action (co kilka godzin).
Jedyne, co musisz ustawić, to swoje ID drużyny w config.json.
"""

import json
import sys
import time
import datetime as dt
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

ROOT = Path(__file__).parent
FPL = "https://fantasy.premierleague.com/api"
UA = {"User-Agent": "Mozilla/5.0 (FPL-Copilot; +https://github.com/)"}
HORIZON = 5  # ile kolejek do przodu w tickerze FDR

# ── stadiony (do pogody) — przybliżone współrzędne ────────────────────────────
STADIUMS = {
    "ARS": (51.5549, -0.1084), "AVL": (52.5092, -1.8848), "BOU": (50.7352, -1.8383),
    "BRE": (51.4907, -0.2887), "BHA": (50.8616, -0.0836), "CHE": (51.4817, -0.1910),
    "COV": (52.4480, -1.4950), "CRY": (51.3983, -0.0855), "EVE": (53.4388, -2.9689),
    "FUL": (51.4749, -0.2217), "HUL": (53.7460, -0.3676), "IPS": (52.0550, 1.1450),
    "LEE": (53.7778, -1.5722), "LIV": (53.4308, -2.9608), "MCI": (53.4831, -2.2004),
    "MUN": (53.4631, -2.2913), "NEW": (54.9756, -1.6217), "NFO": (52.9400, -1.1327),
    "SUN": (54.9145, -1.3882), "TOT": (51.6043, -0.0665),
}


def get_json(url, tries=3, pause=1.5):
    """Pobiera JSON z retry. Zwraca None przy porażce (nie wywala buildu)."""
    for i in range(tries):
        try:
            req = Request(url, headers=UA)
            with urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except (URLError, HTTPError, TimeoutError) as e:
            sys.stderr.write(f"  ! {url} -> {e} (proba {i+1})\n")
            time.sleep(pause)
    return None


# ── model xPts (heurystyczny, przejrzysty) ────────────────────────────────────
BASE_POS = {1: 3.0, 2: 3.3, 3: 3.5, 4: 3.7}  # GK, DEF, MID, FWD


def compute_xpts(p, fdr, is_home):
    """
    Prosty, uczciwy model. Łańcuch:
      baza(forma+PPG) × szansa_minut × trudność(FDR) × dom/wyjazd
    Zwraca (xpts, lista_czynnikow_do_wizualizacji).
    """
    form = float(p.get("form") or 0)
    ppg = float(p.get("points_per_game") or 0)
    pos_base = BASE_POS.get(p.get("element_type", 3), 3.4)

    # baza: mieszanka bieżącej formy, średniej sezonu i sufitu pozycji
    base = 0.5 * form + 0.3 * ppg + 0.2 * pos_base

    # szansa na minuty
    chance = p.get("chance_of_playing_next_round")
    if chance is None:
        chance = 100 if p.get("status") == "a" else 0
    mins_prob = chance / 100.0

    # trudność rywala: FDR 1..5, 3 = neutralne; każdy krok ~8%
    fdr = fdr or 3
    fix_mult = 1 + (3 - fdr) * 0.08

    # dom/wyjazd
    venue_mult = 1.03 if is_home else 0.97

    xpts = base * mins_prob * fix_mult * venue_mult

    factors = [
        {"label": "Baza (forma + PPG)", "value": round(base, 2)},
        {"label": "Szansa na minuty", "mult": round(mins_prob, 2)},
        {"label": f"Trudnosc (FDR {fdr})", "mult": round(fix_mult, 2)},
        {"label": "Dom" if is_home else "Wyjazd", "mult": round(venue_mult, 2)},
    ]
    return round(xpts, 1), factors


# ── pogoda (Open-Meteo, opcjonalna) ───────────────────────────────────────────
def get_weather(short_name, kickoff_iso):
    if not kickoff_iso or short_name not in STADIUMS:
        return None
    try:
        ko = dt.datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
    except Exception:
        return None
    # tylko jeśli mecz w zasięgu prognozy (~16 dni)
    if ko < dt.datetime.now(dt.timezone.utc) or (ko - dt.datetime.now(dt.timezone.utc)).days > 15:
        return None
    lat, lon = STADIUMS[short_name]
    d = ko.date().isoformat()
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           f"&hourly=temperature_2m,precipitation,wind_speed_10m"
           f"&start_date={d}&end_date={d}&timezone=UTC")
    w = get_json(url, tries=2)
    if not w or "hourly" not in w:
        return None
    times = w["hourly"]["time"]
    target = ko.strftime("%Y-%m-%dT%H:00")
    idx = min(range(len(times)), key=lambda i: abs(
        dt.datetime.fromisoformat(times[i]) - ko.replace(tzinfo=None)))
    try:
        return {
            "temp": round(w["hourly"]["temperature_2m"][idx]),
            "rain": w["hourly"]["precipitation"][idx],
            "wind": round(w["hourly"]["wind_speed_10m"][idx]),
        }
    except Exception:
        return None


def current_gw(events):
    """Zwraca (current, next) numery kolejek."""
    cur = nxt = None
    for e in events:
        if e.get("is_current"):
            cur = e["id"]
        if e.get("is_next"):
            nxt = e["id"]
    if cur is None and nxt is None:  # przed startem sezonu
        nxt = events[0]["id"] if events else 1
    if nxt is None:
        nxt = (cur or 0) + 1
    if cur is None:
        cur = nxt
    return cur, nxt


def build():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    team_id = str(cfg.get("team_id", "")).strip()
    manual = cfg.get("manual_squad") or []

    print("· pobieram bootstrap-static ...")
    boot = get_json(f"{FPL}/bootstrap-static/")
    if not boot:
        sys.exit("BLAD: nie udalo sie pobrac bootstrap-static (API FPL niedostepne).")

    print("· pobieram fixtures ...")
    fixtures = get_json(f"{FPL}/fixtures/") or []

    teams = {t["id"]: t for t in boot["teams"]}
    tshort = {t["id"]: t["short_name"] for t in boot["teams"]}
    players = {p["id"]: p for p in boot["elements"]}
    by_name = {p["web_name"].lower(): p for p in boot["elements"]}
    pos_name = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

    cur_gw, next_gw = current_gw(boot["events"])
    gw_name = next(("Gameweek %d" % e["id"] for e in boot["events"] if e["id"] == next_gw), f"GW{next_gw}")

    # ── następne fixtury dla każdej drużyny (oficjalny FDR) ───────────────────
    team_fixtures = {tid: [] for tid in teams}
    for f in sorted(fixtures, key=lambda x: (x.get("event") or 999)):
        if f.get("finished") or f.get("event") is None or f["event"] < next_gw:
            continue
        h, a = f["team_h"], f["team_a"]
        team_fixtures[h].append({"gw": f["event"], "opp": tshort[a], "ven": "H",
                                 "fdr": f["team_h_difficulty"], "kickoff": f.get("kickoff_time")})
        team_fixtures[a].append({"gw": f["event"], "opp": tshort[h], "ven": "A",
                                 "fdr": f["team_a_difficulty"], "kickoff": f.get("kickoff_time")})
    for tid in team_fixtures:
        team_fixtures[tid] = team_fixtures[tid][:HORIZON]

    def next_fix(tid):
        fx = team_fixtures.get(tid) or []
        return fx[0] if fx else None

    # ── skład użytkownika ─────────────────────────────────────────────────────
    picks, entry = [], {}
    if team_id:
        print(f"· pobieram druzyne {team_id} ...")
        entry = get_json(f"{FPL}/entry/{team_id}/") or {}
        pk = get_json(f"{FPL}/entry/{team_id}/event/{cur_gw}/picks/")
        if pk and pk.get("picks"):
            picks = pk["picks"]

    squad_ids = []
    if picks:
        for pk in picks:
            squad_ids.append({"id": pk["element"], "captain": pk["is_captain"],
                              "vice": pk["is_vice_captain"], "mult": pk["multiplier"],
                              "order": pk["position"]})
    elif manual:  # awaryjnie: lista web_name z configu (np. ze screenshota)
        print("· brak picks z API — uzywam manual_squad z config.json")
        for i, nm in enumerate(manual):
            pl = by_name.get(str(nm).lower())
            if pl:
                squad_ids.append({"id": pl["id"], "captain": i == 0, "vice": i == 1,
                                  "mult": 2 if i == 0 else 1, "order": i + 1})
    else:
        print("! Brak team_id i manual_squad — data.json bez skladu (tylko ticker ligowy).")

    squad, xi_xpts, cap_name = [], 0.0, None
    for s in squad_ids:
        p = players.get(s["id"])
        if not p:
            continue
        tid = p["team"]
        nf = next_fix(tid)
        fdr = nf["fdr"] if nf else 3
        is_home = (nf["ven"] == "H") if nf else True
        xpts, factors = compute_xpts(p, fdr, is_home)
        on_bench = s["order"] > 11
        weather = get_weather(tshort[tid], nf["kickoff"]) if nf else None
        entrypl = {
            "id": p["id"], "name": p["web_name"], "team": tshort[tid],
            "team_full": teams[tid]["name"], "pos": pos_name[p["element_type"]],
            "price": p["now_cost"] / 10.0, "form": p.get("form"),
            "ppg": p.get("points_per_game"), "selected_by": p.get("selected_by_percent"),
            "status": p.get("status"), "news": p.get("news") or "",
            "chance": p.get("chance_of_playing_next_round"),
            "ep_next": p.get("ep_next"), "xpts": xpts, "factors": factors,
            "next": ({"opp": nf["opp"], "ven": nf["ven"], "fdr": nf["fdr"]} if nf else None),
            "weather": weather,
            "is_captain": s["captain"], "is_vice": s["vice"],
            "multiplier": s["mult"], "on_bench": on_bench, "order": s["order"],
        }
        squad.append(entrypl)
        if not on_bench:
            xi_xpts += xpts * (2 if s["captain"] else 1)
        if s["captain"]:
            cap_name = p["web_name"]

    # ── ticker FDR pogrupowany po klubie (tylko kluby użytkownika) ─────────────
    xi = [p for p in squad if not p["on_bench"]]
    club_groups = {}
    for p in xi:
        club_groups.setdefault(p["team"], {"team": p["team"], "team_full": p["team_full"],
                                           "players": [], "tid": None})
        club_groups[p["team"]]["players"].append(p["name"])
    # dołóż fixtury klubu
    ticker = []
    name_to_tid = {tshort[tid]: tid for tid in teams}
    for short, g in club_groups.items():
        tid = name_to_tid.get(short)
        fx = [{"gw": f["gw"], "opp": f["opp"], "ven": f["ven"], "fdr": f["fdr"]}
              for f in (team_fixtures.get(tid) or [])]
        avg = round(sum(f["fdr"] for f in fx) / len(fx), 1) if fx else None
        ticker.append({"team": short, "team_full": g["team_full"],
                       "count": len(g["players"]), "players": g["players"],
                       "fixtures": fx, "avg_fdr": avg})
    ticker.sort(key=lambda x: (-x["count"], x["avg_fdr"] or 9))

    # ── koncentracja (najbliższa kolejka): ile graczy na ten sam mecz ─────────
    conc = {}
    for p in xi:
        if not p["next"]:
            continue
        key = f'{p["next"]["opp"]} ({p["next"]["ven"]})'
        conc.setdefault(key, {"fixture": key, "count": 0, "fdr": p["next"]["fdr"], "players": []})
        conc[key]["count"] += 1
        conc[key]["players"].append(p["name"])
    concentration = sorted(conc.values(), key=lambda x: -x["count"])

    data = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gw": {"current": cur_gw, "next": next_gw, "name": gw_name},
        "entry": {
            "team_id": team_id,
            "name": entry.get("name", ""),
            "player_name": (entry.get("player_first_name", "") + " " + entry.get("player_last_name", "")).strip(),
            "overall_points": entry.get("summary_overall_points"),
            "overall_rank": entry.get("summary_overall_rank"),
            "bank": (entry.get("last_deadline_bank") or 0) / 10.0 if entry.get("last_deadline_bank") is not None else None,
            "value": (entry.get("last_deadline_value") or 0) / 10.0 if entry.get("last_deadline_value") is not None else None,
        },
        "squad": squad,
        "ticker": ticker,
        "concentration": concentration,
        "totals": {"xi_xpts": round(xi_xpts, 1), "captain": cap_name},
        "note": "Terminarz i FDR z oficjalnego API FPL. xPts to model heurystyczny (nie oficjalny).",
    }

    out = ROOT / "data.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ zapisano {out}  ({len(squad)} zawodnikow, {len(ticker)} klubow w tickerze)")


if __name__ == "__main__":
    build()
