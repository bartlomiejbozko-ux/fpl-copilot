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
import os
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


def get_json_auth(url, session, tries=2, pause=1.5):
    """Zapytanie z ciasteczkiem sesji — do endpointu my-team (oczekujące transfery)."""
    for i in range(tries):
        try:
            req = Request(url, headers={
                **UA,
                "Cookie": f"sessionid={session}",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://fantasy.premierleague.com/",
            })
            with urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except (URLError, HTTPError, TimeoutError) as e:
            sys.stderr.write(f"  ! auth {url} -> {e} (proba {i+1})\n")
            time.sleep(pause)
    return None


# ── model xPts (komponentowy, wg reguł FPL + dane Opta z API) ─────────────────
import math
import random

GOAL_PTS = {1: 10, 2: 6, 3: 5, 4: 4}    # punkty za gola: BR, OBR, POM, NAP
CS_PTS = {1: 4, 2: 4, 3: 1, 4: 0}        # czyste konto
DC_THRESH = {2: 10, 3: 12, 4: 12}        # próg akcji obronnych: OBR 10 (CBIT), POM/NAP 12 (CBIRT)


def fit_match_model(fixtures, teams):
    """Model goli Dixon-Coles (dwuwymiarowy Poisson): szacuje siłę ataku/obrony
    każdej drużyny z realnych wyników i miesza z priorem z ocen FPL (blending),
    żeby na starcie sezonu nie 'widzieć' przypadków. Zwraca λ na mecz + predykcje 1×2/CS."""
    tids = [t["id"] for t in teams]
    tmap = {t["id"]: t for t in teams}
    matches = []
    for f in fixtures:
        if f.get("finished") and f.get("team_h_score") is not None and f.get("team_a_score") is not None:
            matches.append((f["team_h"], f["team_a"], f["team_h_score"], f["team_a_score"]))
    n = len(matches)
    games = {t: 0 for t in tids}
    for h, a, hs, as_ in matches:
        games[h] = games.get(h, 0) + 1
        games[a] = games.get(a, 0) + 1
    league_avg = (sum(hs + a_ for _, _, hs, a_ in matches) / (2 * n)) if n else 1.4

    # prior z ocen siły FPL (skala ~1000–1400 → log-ratingi)
    sa = {t["id"]: (t.get("strength_attack_home", 1100) + t.get("strength_attack_away", 1100)) / 2 for t in teams}
    sd = {t["id"]: (t.get("strength_defence_home", 1100) + t.get("strength_defence_away", 1100)) / 2 for t in teams}
    msa = (sum(sa.values()) / len(sa)) if sa else 1100
    msd = (sum(sd.values()) / len(sd)) if sd else 1100
    if msa <= 0: msa = 1100
    if msd <= 0: msd = 1100
    att_p = {t: 0.35 * math.log((sa[t] or msa) / msa) for t in tids}   # prior atak (log)
    def_p = {t: -0.35 * math.log((sd[t] or msd) / msd) for t in tids}  # prior obrona (log; mocniejsza=niższe λ)

    att = dict(att_p); dfn = dict(def_p); home_adv = 0.26
    if n >= 20:  # dość danych, by dopasować model
        lr = 0.06
        for _ in range(400):
            ga = {t: 0.0 for t in tids}; gd = {t: 0.0 for t in tids}; gh = 0.0
            for h, a, hs, as_ in matches:
                lh = math.exp(att[h] - dfn[a] + home_adv)
                la = math.exp(att[a] - dfn[h])
                ga[h] += (hs - lh); gd[a] -= (hs - lh); gh += (hs - lh)
                ga[a] += (as_ - la); gd[h] -= (as_ - la)
            for t in tids:  # gradient + regularyzacja do prioru (blending w MLE)
                att[t] += lr * (ga[t] / n - 0.08 * (att[t] - att_p[t]))
                dfn[t] += lr * (gd[t] / n - 0.08 * (dfn[t] - def_p[t]))
            home_adv += lr * (gh / n - 0.05 * (home_adv - 0.26))
            ma = sum(att.values()) / len(tids); md = sum(dfn.values()) / len(tids)
            for t in tids:
                att[t] -= ma; dfn[t] -= md

    def clamp(x): return max(0.15, min(4.0, x))

    def match_lambdas(team_id, opp_id, is_home):
        if team_id not in att or opp_id not in att:
            return league_avg, league_avg
        if is_home:
            lf = math.exp(att[team_id] - dfn[opp_id] + home_adv)
            la = math.exp(att[opp_id] - dfn[team_id])
        else:
            lf = math.exp(att[team_id] - dfn[opp_id])
            la = math.exp(att[opp_id] - dfn[team_id] + home_adv)
        return clamp(lf), clamp(la)

    def predict(home_id, away_id):
        lh = match_lambdas(home_id, away_id, True)[0]
        la = match_lambdas(away_id, home_id, False)[0]
        # macierz wyników (Poisson) do 8 goli
        def pois(k, l): return math.exp(-l) * (l ** k) / math.factorial(k)
        ph = pa = pd = 0.0
        for i in range(9):
            for j in range(9):
                p = pois(i, lh) * pois(j, la)
                if i > j: ph += p
                elif i == j: pd += p
                else: pa += p
        return {"lam_h": round(lh, 2), "lam_a": round(la, 2),
                "p_home": round(ph, 3), "p_draw": round(pd, 3), "p_away": round(pa, 3),
                "cs_home": round(math.exp(-la), 3), "cs_away": round(math.exp(-lh), 3)}

    fitted = n >= 20
    return {"match_lambdas": match_lambdas, "predict": predict, "league_avg": league_avg,
            "n_matches": n, "fitted": fitted, "games": games}


def make_model(teams, match_lambdas=None, league_avg=1.4):
    """Buduje funkcję liczącą xPts jako sumę komponentów wg reguł FPL.
    Korzysta z metryk Opta per 90 (xG, xA, xGC, obrony, akcje obronne) i ocen siły drużyn."""
    atts, defs = [], []
    for t in teams:
        atts += [t.get("strength_attack_home", 1100), t.get("strength_attack_away", 1100)]
        defs += [t.get("strength_defence_home", 1100), t.get("strength_defence_away", 1100)]
    mean_att = (sum(atts) / len(atts)) if atts else 1100
    mean_def = (sum(defs) / len(defs)) if defs else 1100
    if mean_att <= 0: mean_att = 1100.0   # wczesny sezon: oceny siły mogą być 0
    if mean_def <= 0: mean_def = 1100.0
    tmap = {t["id"]: t for t in teams}

    def fnum(p, key, d=0.0):
        try:
            return float(p.get(key) or 0)
        except (TypeError, ValueError):
            return d

    def _rates(p, opp_id, is_home, team_id=None):
        """Rdzeń modelu: stawki zdarzeń (współdzielone przez compute i symulator)."""
        et = p.get("element_type", 3)
        chance = p.get("chance_of_playing_next_round")
        if chance is None:
            chance = 100 if p.get("status") == "a" else 0
        m = chance / 100.0
        starts = fnum(p, "starts")
        mins = fnum(p, "minutes")
        g90 = (mins / 90.0) if mins > 0 else 0
        xg90 = fnum(p, "expected_goals_per_90")
        xa90 = fnum(p, "expected_assists_per_90")
        sv90 = fnum(p, "saves_per_90")
        dc90 = fnum(p, "defensive_contribution_per_90")
        if dc90 == 0:
            cbi = fnum(p, "clearances_blocks_interceptions"); tk = fnum(p, "tackles")
            rec = fnum(p, "recoveries") if et in (3, 4) else 0
            dc90 = ((cbi + tk + rec) / g90) if g90 > 0 else 0
        # λ z modelu meczu (Dixon-Coles) jeśli dostępny; inaczej proxy z ocen siły FPL
        if match_lambdas and team_id:
            lam_for, lam = match_lambdas(team_id, opp_id, is_home)   # gole zespołu / stracone
            att_mult = max(0.6, min(1.6, lam_for / max(0.4, league_avg)))
            lam = max(0.25, min(3.5, lam))
        else:
            opp = tmap.get(opp_id, {})
            opp_def = opp.get("strength_defence_away" if is_home else "strength_defence_home", mean_def) or mean_def
            opp_att = opp.get("strength_attack_away" if is_home else "strength_attack_home", mean_att) or mean_att
            opp_def = opp_def if opp_def and opp_def > 0 else mean_def
            opp_att = opp_att if opp_att and opp_att > 0 else mean_att
            att_mult = max(0.6, min(1.5, mean_def / opp_def)) * (1.05 if is_home else 0.97)
            lam = max(0.25, min(3.0, 1.25 * (opp_att / mean_att) * (0.9 if is_home else 1.12)))
        p_cs = math.exp(-lam)
        e_goals = xg90 * m * att_mult
        e_assist = xa90 * m * att_mult
        thr = DC_THRESH.get(et)
        dc_prob = (1 / (1 + math.exp(-(dc90 - thr) / 2.5))) if (thr and dc90 > 0) else 0.0
        yc = fnum(p, "yellow_cards")
        gp = starts if starts > 0 else max(g90, 1)
        yc_rate = min(0.4, yc / gp)
        return {"et": et, "m": m, "e_goals": e_goals, "e_assist": e_assist, "p_cs": p_cs,
                "lam": lam, "sv90": sv90, "dc_prob": dc_prob, "yc_rate": yc_rate}

    def compute(p, opp_id, is_home, team_id=None):
        if not opp_id:  # pusta kolejka (BGW) — brak meczu
            return 0.0, [{"label": "Brak meczu (BGW)", "pts": 0.0}]
        r = _rates(p, opp_id, is_home, team_id)
        et, m = r["et"], r["m"]
        pts_app = m * 2.0
        pts_goals = r["e_goals"] * GOAL_PTS.get(et, 4)
        pts_assist = r["e_assist"] * 3.0
        pts_cs = (r["p_cs"] * m) * CS_PTS.get(et, 0)
        pts_conc = -(r["lam"] / 2.0) * m if et in (1, 2) else 0.0
        pts_sv = (r["sv90"] * m / 3.0) if et == 1 else 0.0
        pts_dc = r["dc_prob"] * 2.0 * m
        pts_bonus = min(1.6, r["e_goals"] * 0.9 + r["e_assist"] * 0.6 + (r["p_cs"] * m * 0.3 if et in (1, 2) else 0))
        pts_cards = -r["yc_rate"] * m

        total = (pts_app + pts_goals + pts_assist + pts_cs + pts_conc
                 + pts_sv + pts_dc + pts_bonus + pts_cards)

        factors = [{"label": "Występ (minuty)", "pts": round(pts_app, 2)},
                   {"label": "Gole (xG)", "pts": round(pts_goals, 2)},
                   {"label": "Asysty (xA)", "pts": round(pts_assist, 2)}]
        if et == 1:
            factors.append({"label": "Obrony", "pts": round(pts_sv, 2)})
            factors.append({"label": "Czyste konto", "pts": round(pts_cs, 2)})
        elif et == 2:
            factors.append({"label": "Czyste konto", "pts": round(pts_cs, 2)})
            factors.append({"label": "Akcje obronne", "pts": round(pts_dc, 2)})
        else:
            factors.append({"label": "Czyste konto", "pts": round(pts_cs, 2)})
            factors.append({"label": "Akcje obronne", "pts": round(pts_dc, 2)})
        factors.append({"label": "Bonus", "pts": round(pts_bonus, 2)})
        factors.append({"label": "Stracone + kartki", "pts": round(pts_conc + pts_cards, 2)})

        return round(max(0.0, total), 1), factors

    def sim_spec(p, opp_id, is_home, team_id=None):
        return None if not opp_id else _rates(p, opp_id, is_home, team_id)

    def explain(et, team_id, opp_id, is_home):
        """Krótkie, ludzkie uzasadnienie wyboru na bazie sił drużyn i miejsca meczu."""
        my = tmap.get(team_id, {})
        opp = tmap.get(opp_id, {})
        rs = []
        hs = my.get("strength_overall_home", 1100) or 1100
        aw = my.get("strength_overall_away", 1100) or 1100
        if is_home and hs > aw + 25:
            rs.append("gra u siebie, gdzie ta drużyna jest wyraźnie mocniejsza")
        elif (not is_home) and aw >= hs - 10:
            rs.append("dobrze radzi sobie na wyjeździe")
        opp_def = opp.get("strength_defence_away" if is_home else "strength_defence_home", mean_def) or mean_def
        opp_att = opp.get("strength_attack_away" if is_home else "strength_attack_home", mean_att) or mean_att
        if et in (3, 4, 2):  # ofensywa
            if opp_def <= mean_def - 60:
                rs.append("rywal słabo broni — sprzyja zdobyciu punktów")
            elif opp_def >= mean_def + 70:
                if opp_att <= mean_att - 60:
                    rs.append("rywal gra defensywnie (zaparkowany autobus) — trudno o gola")
                else:
                    rs.append("rywal broni się mocno")
        if et in (1, 2):  # obrona / czyste konto
            if opp_att <= mean_att - 60:
                rs.append("wysoka szansa na czyste konto (rywal słabo atakuje)")
            elif opp_att >= mean_att + 70:
                rs.append("czyste konto mało prawdopodobne (groźny atak rywala)")
        return "; ".join(rs[:2]) if rs else "korzystniejszy profil xPts na tę kolejkę"

    def simulate(spec, n=4000):
        """Zwraca listę n wylosowanych wyników punktowych zawodnika w kolejce.
        Losuje realne zdarzenia (gole/asysty/CK/obrona) z tych samych stawek co model —
        więc rozkład jest 'grudkowaty' jak prawdziwe punkty FPL, bez zmyślonych mnożników."""
        if spec is None:
            return [0.0] * n
        et, m = spec["et"], spec["m"]
        gpts = GOAL_PTS.get(et, 4)
        xg, xa = spec["e_goals"], spec["e_assist"]
        p_cs, lam = spec["p_cs"], spec["lam"]
        sv90, dcp, ycr = spec["sv90"], spec["dc_prob"], spec["yc_rate"]
        def pois(l):
            if l <= 0: return 0
            L, k, pr = math.exp(-l), 0, 1.0
            while True:
                pr *= random.random(); k += 1
                if pr <= L: return k - 1
        out = []
        for _ in range(n):
            if random.random() >= m:      # nie zagrał
                out.append(0.0); continue
            plays60 = random.random() < 0.9
            pts = 2.0 if plays60 else 1.0
            g = pois(xg); pts += g * gpts
            a = pois(xa); pts += a * 3
            if et in (1, 2) and plays60 and random.random() < p_cs:
                pts += CS_PTS.get(et, 0)
            if et in (1, 2):
                pts -= (pois(lam) // 2)
            if et == 1:
                pts += (pois(sv90) // 3)
            if et in (2, 3, 4) and dcp and random.random() < dcp:
                pts += 2
            if g > 0 or a > 0:            # bonus zależny od returnów
                rr = random.random()
                pts += 3 if rr < 0.25 else 2 if rr < 0.5 else 1 if rr < 0.75 else 0
            if random.random() < ycr:
                pts -= 1
            out.append(float(pts))
        return out

    return compute, explain, sim_spec, simulate


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


def best_xi_value(players, gw):
    """Wartość najlepszej XI z zestawu graczy w danej kolejce (xPts wg formacji FPL)."""
    by = {1: [], 2: [], 3: [], 4: []}
    for p in players:
        by[p["et"]].append(p["gwx"].get(gw, 0.0))
    for k in by:
        by[k].sort(reverse=True)
    if len(by[1]) < 1 or len(by[2]) < 3 or len(by[4]) < 1:
        return 0.0
    best = 0.0
    for d in range(3, 6):
        for f in range(1, 4):
            m = 10 - d - f
            if m < 2 or m > 5:
                continue
            if len(by[2]) < d or len(by[3]) < m or len(by[4]) < f:
                continue
            tot = by[1][0] + sum(by[2][:d]) + sum(by[3][:m]) + sum(by[4][:f])
            if tot > best:
                best = tot
    return best


def plan_transfers(squad, pool, bank_tenths, start_ft, horizon_gws):
    """Planer wieloetapowy (beam search): sekwencja transferów przez horyzont,
    z bankiem wolnych transferów (max 5), hitami −4 i terminarzem. Heurystyka
    bliska optymalnej — nie pełne przeszukanie (to jest NP-trudne)."""
    if not squad or not horizon_gws:
        return None
    def mkp(p):
        return {"id": p["id"], "et": p.get("etype") or p.get("et"), "team": p["team"],
                "price": int(round(p["price"] * 10)),
                "gwx": {r["gw"]: r["xpts"] for r in (p.get("roadmap") or [])}, "name": p["name"]}
    sq = [mkp(p) for p in squad]
    sqids = {p["id"] for p in sq}
    poolp = [mkp(p) for p in pool if p["id"] not in sqids and p.get("roadmap")]
    by_pos = {1: [], 2: [], 3: [], 4: []}
    for c in poolp:
        by_pos.setdefault(c["et"], []).append(c)
    for k in by_pos:
        by_pos[k].sort(key=lambda c: -sum(c["gwx"].get(g, 0) for g in horizon_gws))

    def hzn(p, from_i):
        return sum(p["gwx"].get(g, 0) for g in horizon_gws[from_i:])

    def club_counts(ids_map):
        cc = {}
        for p in ids_map.values():
            cc[p["team"]] = cc.get(p["team"], 0) + 1
        return cc

    # stan: (dict id->player, bank, ft, cum_value, log)
    start = {"squad": {p["id"]: p for p in sq}, "bank": bank_tenths, "ft": min(5, max(1, start_ft)),
             "cum": 0.0, "hits": 0, "log": []}
    beam = [start]
    B = 6
    for gi, gw in enumerate(horizon_gws):
        nxt = []
        for stt in beam:
            smap = stt["squad"]
            cc = club_counts(smap)
            # kandydaci transferu: najsłabszy w składzie per pozycja → najlepszy dostępny
            options = [("hold", None)]
            weak_by_pos = {}
            for p in smap.values():
                pos = p["et"]
                if pos not in weak_by_pos or hzn(p, gi) < hzn(weak_by_pos[pos], gi):
                    weak_by_pos[pos] = p
            cand_moves = []
            for pos, outp in weak_by_pos.items():
                sell = outp["price"]
                for inp in by_pos.get(pos, [])[:12]:
                    if inp["id"] in smap:
                        continue
                    if inp["price"] > stt["bank"] + sell:
                        continue
                    if inp["team"] != outp["team"] and cc.get(inp["team"], 0) >= 3:
                        continue
                    gain = hzn(inp, gi) - hzn(outp, gi)
                    if gain > 0.3:
                        cand_moves.append((gain, outp, inp))
            cand_moves.sort(key=lambda x: -x[0])
            for gain, outp, inp in cand_moves[:4]:
                options.append(("move", (outp, inp)))

            for kind, mv in options:
                ns = {"squad": dict(smap), "bank": stt["bank"], "ft": stt["ft"],
                      "cum": stt["cum"], "hits": stt["hits"], "log": list(stt["log"])}
                moved = []
                if kind == "move":
                    outp, inp = mv
                    del ns["squad"][outp["id"]]
                    ns["squad"][inp["id"]] = inp
                    ns["bank"] = ns["bank"] + outp["price"] - inp["price"]
                    used = 1
                    hit = max(0, used - ns["ft"]) * 4
                    ns["ft"] = min(5, max(0, ns["ft"] - used) + 1)
                    ns["cum"] -= hit
                    ns["hits"] += hit
                    moved = [{"out": outp["name"], "in": inp["name"],
                              "gain": round(hzn(inp, gi) - hzn(outp, gi), 1)}]
                else:
                    ns["ft"] = min(5, ns["ft"] + 1)
                gwval = best_xi_value(list(ns["squad"].values()), gw)
                ns["cum"] += gwval
                ns["log"].append({"gw": gw, "action": ("transfer" if kind == "move" else "hold"),
                                  "moves": moved, "ft_after": ns["ft"],
                                  "bank_after": round(ns["bank"] / 10, 1),
                                  "gw_xpts": round(gwval, 1),
                                  "hit": (moved and max(0, 1 - stt["ft"]) * 4) or 0})
                nxt.append(ns)
        nxt.sort(key=lambda s: -s["cum"])
        beam = nxt[:B]
    best = beam[0]
    # baseline: trzymaj skład przez cały horyzont
    base = sum(best_xi_value(list(start["squad"].values()), g) for g in horizon_gws)
    return {"horizon": len(horizon_gws), "gws": horizon_gws,
            "start_bank": round(bank_tenths / 10, 1), "start_ft": start["ft"],
            "steps": best["log"], "total_value": round(best["cum"], 1),
            "baseline_value": round(base, 1),
            "gain_vs_hold": round(best["cum"] - base, 1),
            "total_hits": best["hits"]}


def fnum2(p, key, d=0.0):
    try:
        return float(p.get(key) or 0)
    except (TypeError, ValueError):
        return d


def price_pred(e, total_players):
    """Predyktor zmiany ceny (przybliżony — algorytm FPL jest tajny).
    Skaluje przepływ transferów liczbą właścicieli: rośnie/spada, gdy net-transfery
    przekroczą próg proporcjonalny do posiadania. Zwraca kierunek + 'ciśnienie' 0..1."""
    net = (e.get("transfers_in_event") or 0) - (e.get("transfers_out_event") or 0)
    try:
        owners = max(1, int((float(e.get("selected_by_percent") or 0) / 100.0) * total_players))
    except (TypeError, ValueError):
        owners = 1
    thr = max(12000, 0.06 * owners)   # próg ~ proporcjonalny do właścicieli (heurystyka)
    pressure = net / thr if thr else 0
    already = e.get("cost_change_event") or 0
    if pressure >= 1.0 or already > 0:
        direction = "rise"
    elif pressure <= -1.0 or already < 0:
        direction = "fall"
    else:
        direction = "stable"
    return {"dir": direction, "score": round(max(0.0, min(1.0, abs(pressure))), 2),
            "net": net, "moved": already}


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
        team_fixtures[h].append({"gw": f["event"], "opp": tshort[a], "ven": "H", "opp_id": a,
                                 "fdr": f["team_h_difficulty"], "kickoff": f.get("kickoff_time")})
        team_fixtures[a].append({"gw": f["event"], "opp": tshort[h], "ven": "A", "opp_id": h,
                                 "fdr": f["team_a_difficulty"], "kickoff": f.get("kickoff_time")})
    for tid in team_fixtures:
        team_fixtures[tid] = team_fixtures[tid][:HORIZON]

    def next_fix(tid):
        fx = team_fixtures.get(tid) or []
        return fx[0] if fx else None

    mm = fit_match_model(fixtures, boot["teams"])   # model goli Dixon-Coles + predykcje
    compute_xpts, explain_xpts, sim_spec, simulate = make_model(
        boot["teams"], match_lambdas=mm["match_lambdas"], league_avg=mm["league_avg"])
    print(f"· model meczu: {'dopasowany' if mm['fitted'] else 'prior (za mało meczów)'}"
          f" ({mm['n_matches']} rozegranych)")

    def sim_stats(p, opp_id, is_home, n=2000, team_id=None):
        """Floor (P25), ceiling (P90), haul% (P≥10) z symulacji Monte Carlo — uczciwa zmienność."""
        if not opp_id:
            return {"floor": 0, "ceiling": 0, "haul": 0.0, "sims": None}
        sims = simulate(sim_spec(p, opp_id, is_home, team_id), n)
        srt = sorted(sims)
        floor = srt[int(len(srt) * 0.25)]
        ceiling = srt[int(len(srt) * 0.90)]
        haul = sum(1 for x in sims if x >= 10) / len(sims)
        mean = sum(sims) / len(sims)
        var = sum((x - mean) ** 2 for x in sims) / len(sims)
        return {"floor": round(floor), "ceiling": round(ceiling), "haul": round(haul, 3),
                "sd": round(var ** 0.5, 2), "sims": sims}

    def xpts_horizon(p, tid, n=3):
        """Suma xPts z najbliższych n meczów — do rankingu transferów (nagradza dobry terminarz)."""
        tot = 0.0
        for f in (team_fixtures.get(tid) or [])[:n]:
            x, _ = compute_xpts(p, f["opp_id"], f["ven"] == "H", tid)
            tot += x
        return round(tot, 1)

    _es_cache = {}
    def get_es(pid):
        if pid not in _es_cache:
            _es_cache[pid] = get_json(f"{FPL}/element-summary/{pid}/", tries=2) or {}
        return _es_cache[pid]

    def last5(pid):
        """Punkty z ostatnich 5 rozegranych meczów (do mini-wykresu formy)."""
        hist = (get_es(pid).get("history") or [])
        return [h.get("total_points", 0) for h in hist[-5:]]

    def rotation_recent(pid, chance):
        """Ryzyko rotacji z minut z ostatnich meczów (dla składu — używa cache)."""
        hist = (get_es(pid).get("history") or [])[-5:]
        mins = [h.get("minutes", 0) for h in hist]
        if not mins:
            return None
        avg = sum(mins) / len(mins)
        start_rate = sum(1 for m in mins if m >= 60) / len(mins)
        if chance is not None and chance < 75:
            lvl = "doubt"
        elif avg >= 80 and start_rate >= 0.8:
            lvl = "nailed"
        elif avg >= 55:
            lvl = "solid"
        else:
            lvl = "risk"
        return {"level": lvl, "avg_min": round(avg), "start_rate": round(start_rate, 2), "n": len(mins)}

    def rotation_season(p):
        """Ryzyko rotacji z sezonowych sum (dla puli — bez dodatkowych zapytań)."""
        starts = fnum2(p, "starts"); mins = fnum2(p, "minutes")
        chance = p.get("chance_of_playing_next_round")
        if starts <= 0 and mins <= 0:
            return {"level": "unknown", "avg_min": 0, "start_rate": 0.0}
        avg = (mins / starts) if starts > 0 else 0
        if chance is not None and chance < 75:
            lvl = "doubt"
        elif starts >= 3 and avg >= 80:
            lvl = "nailed"
        elif avg >= 55:
            lvl = "solid"
        else:
            lvl = "risk"
        return {"level": lvl, "avg_min": round(avg), "start_rate": None}

    # ── skład użytkownika ─────────────────────────────────────────────────────
    session = os.environ.get("FPL_SESSION", "").strip()
    picks, entry, bank_override = [], {}, None
    if team_id:
        print(f"· pobieram druzyne {team_id} ...")
        entry = get_json(f"{FPL}/entry/{team_id}/") or {}
        # 1) zalogowana drużyna (my-team) — zawiera OCZEKUJĄCE transfery na najbliższy GW
        if session and not manual:
            mt = get_json_auth(f"{FPL}/my-team/{team_id}/", session)
            if mt and mt.get("picks"):
                picks = mt["picks"]
                bank_override = (mt.get("transfers") or {}).get("bank")
                print("· ✓ uzyto ZALOGOWANEJ druzyny (my-team) — z oczekujacymi transferami")
            else:
                print("! sesja podana, ale my-team nie zwrocilo skladu "
                      "(wygasla sesja lub blokada IP). Wracam do publicznego API.")
        # 2) publiczny skład zablokowany po ostatnim deadline
        if not picks and not manual:
            pk = get_json(f"{FPL}/entry/{team_id}/event/{cur_gw}/picks/")
            if pk and pk.get("picks"):
                picks = pk["picks"]
                print(f"· uzyto zablokowanego skladu z GW{cur_gw} (publiczne API — "
                      "bez oczekujacych transferow)")

    squad_ids = []
    if manual:  # ręczne nadpisanie z config.json (najwyższy priorytet)
        print("· uzyto manual_squad z config.json (nadpisanie)")
        for i, nm in enumerate(manual):
            pl = by_name.get(str(nm).lower())
            if pl:
                squad_ids.append({"id": pl["id"], "captain": i == 0, "vice": i == 1,
                                  "mult": 2 if i == 0 else (1 if i < 11 else 0), "order": i + 1})
            else:
                sys.stderr.write(f"  ! manual_squad: nie znaleziono '{nm}' — sprawdz pisownie web_name\n")
    elif picks:
        for pk in picks:
            squad_ids.append({"id": pk["element"], "captain": pk["is_captain"],
                              "vice": pk["is_vice_captain"], "mult": pk["multiplier"],
                              "order": pk["position"]})
    else:
        print("! Brak skladu (team_id/manual_squad). data.json bez skladu.")

    squad, xi_xpts, cap_name = [], 0.0, None
    squad_sims = {}   # id -> lista symulacji (do rozkładu całej XI; nie trafia do JSON)
    for s in squad_ids:
        p = players.get(s["id"])
        if not p:
            continue
        tid = p["team"]
        nf = next_fix(tid)
        fdr = nf["fdr"] if nf else 3
        is_home = (nf["ven"] == "H") if nf else True
        opp_id = nf["opp_id"] if nf else None
        xpts, factors = compute_xpts(p, opp_id, is_home, tid)
        sstat = sim_stats(p, opp_id, is_home, n=3000, team_id=tid)
        roadmap = []
        for fx in (team_fixtures.get(tid) or []):
            rx, _ = compute_xpts(p, fx["opp_id"], fx["ven"] == "H", tid)
            roadmap.append({"gw": fx["gw"], "opp": fx["opp"], "ven": fx["ven"],
                            "fdr": fx["fdr"], "xpts": rx})
        xph = xpts_horizon(p, tid)
        on_bench = s["order"] > 11
        weather = get_weather(tshort[tid], nf["kickoff"]) if nf else None
        entrypl = {
            "id": p["id"], "name": p["web_name"], "team": tshort[tid],
            "team_full": teams[tid]["name"], "pos": pos_name[p["element_type"]],
            "etype": p["element_type"], "xpts_h": xph, "price_t": p["now_cost"],
            "price": p["now_cost"] / 10.0, "form": p.get("form"),
            "ppg": p.get("points_per_game"), "selected_by": p.get("selected_by_percent"),
            "status": p.get("status"), "news": p.get("news") or "",
            "chance": p.get("chance_of_playing_next_round"),
            "ep_next": p.get("ep_next"), "xpts": xpts, "factors": factors,
            "set_pieces": {"pens": p.get("penalties_order"),
                           "corners": p.get("corners_and_indirect_freekicks_order"),
                           "fk": p.get("direct_freekicks_order")},
            "price_mom": (p.get("transfers_in_event") or 0) - (p.get("transfers_out_event") or 0),
            "cost_change_event": p.get("cost_change_event") or 0,
            "price_pred": price_pred(p, boot.get("total_players") or 10_000_000),
            "rotation": rotation_recent(p["id"], p.get("chance_of_playing_next_round")) or rotation_season(p),
            "form5": last5(p["id"]),
            "roadmap": roadmap,
            "floor": sstat["floor"], "ceiling": sstat["ceiling"], "haul": sstat["haul"], "sd": sstat["sd"],
            "next": ({"opp": nf["opp"], "ven": nf["ven"], "fdr": nf["fdr"], "opp_id": nf["opp_id"]} if nf else None),
            "team_id": tid,
            "weather": weather,
            "is_captain": s["captain"], "is_vice": s["vice"],
            "multiplier": s["mult"], "on_bench": on_bench, "order": s["order"],
        }
        squad.append(entrypl)
        squad_sims[s["id"]] = {"sims": sstat["sims"], "on_bench": entrypl["on_bench"],
                               "is_captain": entrypl.get("is_captain", False)}
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

    # ── DORADCA: pula zawodników, transfery, kapitan, alerty ─────────────────
    bank_t = bank_override if bank_override is not None else (entry.get("last_deadline_bank") or 0)
    owned = {s["id"] for s in squad_ids}

    # pula: xPts + xPts_horizon dla każdego dostępnego zawodnika
    pool_by_pos = {1: [], 2: [], 3: [], 4: []}
    for p in boot["elements"]:
        if p.get("status") in ("u", "n"):   # niedostępny / poza kadrą
            continue
        tid = p["team"]
        nf = next_fix(tid)
        if not nf:
            continue
        x, fct = compute_xpts(p, nf["opp_id"], nf["ven"] == "H", tid)
        pstat = sim_stats(p, nf["opp_id"], nf["ven"] == "H", n=1200, team_id=tid)
        p_roadmap = []
        for fx in (team_fixtures.get(tid) or []):
            rx, _ = compute_xpts(p, fx["opp_id"], fx["ven"] == "H", tid)
            p_roadmap.append({"gw": fx["gw"], "opp": fx["opp"], "ven": fx["ven"],
                              "fdr": fx["fdr"], "xpts": rx})
        cand = {
            "id": p["id"], "name": p["web_name"], "team": tshort[tid],
            "et": p["element_type"],
            "price": p["now_cost"] / 10.0, "price_t": p["now_cost"],
            "xpts": x, "xpts_h": xpts_horizon(p, tid), "status": p.get("status"),
            "chance": p.get("chance_of_playing_next_round"),
            "selected_by": p.get("selected_by_percent"),
            "next": {"opp": nf["opp"], "ven": nf["ven"], "fdr": nf["fdr"]},
            "factors": fct, "roadmap": p_roadmap,
            "floor": pstat["floor"], "ceiling": pstat["ceiling"], "haul": pstat["haul"], "sd": pstat["sd"],
            "rotation": rotation_season(p),
            "price_pred": price_pred(p, boot.get("total_players") or 10_000_000),
        }
        pool_by_pos[p["element_type"]].append(cand)

    def best_upgrade(op):
        """Najlepszy transfer za tego zawodnika w ramach budżetu (cena + bank)."""
        budget = op["price_t"] + bank_t
        cands = [c for c in pool_by_pos.get(op["etype"], [])
                 if c["id"] not in owned and c["price_t"] <= budget
                 and c["status"] == "a" and (c["chance"] is None or c["chance"] >= 75)]
        cands.sort(key=lambda c: -c["xpts_h"])
        return cands[0] if cands else None

    # rekomendacje transferów: najlepsze pary OUT→IN po całym składzie startowym
    trans = []
    for op in [p for p in squad if not p["on_bench"]]:
        up = best_upgrade(op)
        if not up:
            continue
        gain = round(up["xpts_h"] - op["xpts_h"], 1)
        if gain >= 1.5:  # próg sensowności
            trans.append({
                "gain": gain,
                "out": {"name": op["name"], "team": op["team"], "pos": op["pos"],
                        "price": op["price"], "xpts_h": op["xpts_h"], "next": op["next"]},
                "in": {"name": up["name"], "team": up["team"],
                       "price": up["price"], "xpts_h": up["xpts_h"], "next": up["next"],
                       "selected_by": up["selected_by"]},
            })
    trans.sort(key=lambda t: -t["gain"])
    trans = trans[:3]

    # kapitan-optymalizator
    xi_sorted = sorted(xi, key=lambda p: -p["xpts"])
    best_cap = xi_sorted[0] if xi_sorted else None
    cur_cap = next((p for p in xi if p["is_captain"]), None)
    captain = None
    if best_cap and cur_cap:
        captain = {
            "current": cur_cap["name"], "current_xpts": cur_cap["xpts"],
            "best": best_cap["name"], "best_xpts": best_cap["xpts"],
            "optimal": best_cap["id"] == cur_cap["id"],
            "gain": round(best_cap["xpts"] - cur_cap["xpts"], 1),
        }

    # matryca kapitańska: Tarcza (bezpieczny, wysoka własność) vs Sztylet (różnicowy)
    def _own(p):
        try:
            return float(p.get("selected_by") or 0)
        except (TypeError, ValueError):
            return 0.0
    captain_matrix = None
    if xi_sorted:
        shield = max(xi, key=lambda p: _own(p) * 0.04 + p["xpts"])  # własność wspiera, ale xPts decyduje
        dagger_cands = [p for p in xi if _own(p) < 15.0]
        dagger = max(dagger_cands, key=lambda p: p["xpts"]) if dagger_cands else xi_sorted[0]
        captain_matrix = {
            "shield": {"name": shield["name"], "xpts": shield["xpts"], "own": shield.get("selected_by"),
                       "opp": shield["next"]["opp"] if shield.get("next") else "",
                       "ven": shield["next"]["ven"] if shield.get("next") else ""},
            "dagger": {"name": dagger["name"], "xpts": dagger["xpts"], "own": dagger.get("selected_by"),
                       "opp": dagger["next"]["opp"] if dagger.get("next") else "",
                       "ven": dagger["next"]["ven"] if dagger.get("next") else ""},
            "same": shield["name"] == dagger["name"],
        }
    # alerty: kontuzje / zawieszenia / wątpliwości / newsy
    SEV = {"i": ("Kontuzja", 3), "s": ("Zawieszenie", 3), "u": ("Niedostępny", 3),
           "d": ("Wątpliwy", 2), "n": ("Poza kadrą", 2)}
    alerts = []
    for p in squad:
        st = p["status"]
        chance = p["chance"]
        flagged = (st and st != "a") or (chance is not None and chance < 100) or p["news"]
        if not flagged:
            continue
        label, sev = SEV.get(st, ("Uwaga", 1))
        if st == "a" and chance is not None and chance < 100:
            label, sev = f"Szansa gry {chance}%", 2
        alerts.append({"name": p["name"], "pos": p["pos"], "team": p["team"],
                       "label": label, "sev": sev, "news": p["news"],
                       "on_bench": p["on_bench"]})
    alerts.sort(key=lambda a: -a["sev"])

    # ── ŁAWKA + optymalna jedenastka (maksymalizacja xPts w regułach formacji) ──
    def optimize_lineup(squad):
        gks = sorted([p for p in squad if p["etype"] == 1], key=lambda p: -p["xpts"])
        by = {2: [], 3: [], 4: []}
        for p in squad:
            if p["etype"] in by:
                by[p["etype"]].append(p)
        for k in by:
            by[k].sort(key=lambda p: -p["xpts"])
        if not gks or len(by[2]) < 3 or len(by[3]) < 2 or len(by[4]) < 1:
            return None
        best = None
        for d in range(3, 6):
            for m in range(2, 6):
                for f in range(1, 4):
                    if d + m + f != 10 or len(by[2]) < d or len(by[3]) < m or len(by[4]) < f:
                        continue
                    sel = by[2][:d] + by[3][:m] + by[4][:f]
                    tot = sum(p["xpts"] for p in sel) + gks[0]["xpts"]
                    if best is None or tot > best["total"]:
                        best = {"total": round(tot, 1), "form": f"{d}-{m}-{f}",
                                "ids": {p["id"] for p in sel} | {gks[0]["id"]}}
        if not best:
            return None

        cur_starters = [p for p in squad if not p["on_bench"]]
        cur_ids = {p["id"] for p in cur_starters}
        cur_raw = round(sum(p["xpts"] for p in cur_starters), 1)
        cur_out = sorted([p for p in cur_starters if p["etype"] != 1], key=lambda p: -p["etype"])
        # formacja obecna
        cd = sum(1 for p in cur_starters if p["etype"] == 2)
        cm = sum(1 for p in cur_starters if p["etype"] == 3)
        cf = sum(1 for p in cur_starters if p["etype"] == 4)

        bring_in = sorted([p for p in squad if p["id"] in best["ids"] and p["on_bench"]],
                          key=lambda p: -p["xpts"])
        sit_out = sorted([p for p in squad if p["id"] not in best["ids"] and not p["on_bench"]],
                         key=lambda p: -p["xpts"])
        swaps = []
        for i in range(min(len(bring_in), len(sit_out))):
            inp, outp = bring_in[i], sit_out[i]
            reason = ""
            if inp.get("next"):
                reason = explain_xpts(inp["etype"], inp.get("team_id"),
                                      inp["next"].get("opp_id"), inp["next"]["ven"] == "H")
            swaps.append({"in": {"name": inp["name"], "pos": inp["pos"], "xpts": inp["xpts"],
                                 "opp": inp["next"]["opp"] if inp.get("next") else "",
                                 "ven": inp["next"]["ven"] if inp.get("next") else "",
                                 "reason": reason},
                          "out": {"name": outp["name"], "pos": outp["pos"], "xpts": outp["xpts"]},
                          "delta": round(inp["xpts"] - outp["xpts"], 1)})

        # kolejność ławki (auto-zmiany): rezerwowi outfield wg xPts malejąco + rezerwowy BR
        bench_out = sorted([p for p in squad if p["id"] not in best["ids"] and p["etype"] != 1],
                           key=lambda p: -p["xpts"])
        bench_gk = [p for p in gks if p["id"] != gks[0]["id"]]
        bench_order = ([{"name": p["name"], "pos": p["pos"], "xpts": p["xpts"]} for p in bench_out]
                       + [{"name": p["name"], "pos": p["pos"], "xpts": p["xpts"]} for p in bench_gk])

        # najlepsze wybory w jedenastce — z uzasadnieniem (nawet gdy ustawienie optymalne)
        opt_starters = sorted([p for p in squad if p["id"] in best["ids"]],
                              key=lambda p: -p["xpts"])
        highlights = []
        for p in opt_starters[:3]:
            r = explain_xpts(p["etype"], p.get("team_id"),
                             p["next"].get("opp_id") if p.get("next") else None,
                             p["next"]["ven"] == "H" if p.get("next") else True)
            highlights.append({"name": p["name"], "pos": p["pos"], "xpts": p["xpts"],
                               "opp": p["next"]["opp"] if p.get("next") else "",
                               "ven": p["next"]["ven"] if p.get("next") else "", "reason": r})

        # proponowana jedenastka na następny GW (uporządkowana, z kapitanem = najwyższe xPts)
        xi_players = [p for p in squad if p["id"] in best["ids"]]
        by_xpts = sorted(xi_players, key=lambda p: -p["xpts"])
        cap_id = by_xpts[0]["id"] if by_xpts else None
        vice_id = by_xpts[1]["id"] if len(by_xpts) > 1 else None
        pos_ord = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}
        proposed_xi = [{
            "name": p["name"], "pos": p["pos"], "team": p["team"], "xpts": p["xpts"],
            "opp": p["next"]["opp"] if p.get("next") else "",
            "ven": p["next"]["ven"] if p.get("next") else "",
            "fdr": p["next"]["fdr"] if p.get("next") else 3,
            "is_captain": p["id"] == cap_id, "is_vice": p["id"] == vice_id,
            "was_benched": p["on_bench"],
        } for p in sorted(xi_players, key=lambda p: (pos_ord.get(p["pos"], 9), -p["xpts"]))]
        # suma XI z podwojeniem kapitana
        xi_total = round(sum(p["xpts"] * (2 if p["id"] == cap_id else 1) for p in xi_players), 1)

        return {"optimal": best["ids"] == cur_ids, "gain": round(best["total"] - cur_raw, 1),
                "optimal_formation": best["form"], "current_formation": f"{cd}-{cm}-{cf}",
                "optimal_xpts": best["total"], "current_xpts": cur_raw,
                "swaps": swaps, "bench_order": bench_order, "highlights": highlights,
                "xi": proposed_xi, "xi_total": xi_total}

    lineup = optimize_lineup(squad) if len([p for p in squad]) >= 11 else None

    # rozkład sumy punktów jedenastki w najbliższej kolejce (Monte Carlo, kapitan ×2)
    gw_outlook = None
    xi_totals_sample = None
    if lineup and lineup.get("xi"):
        xi_names = {x["name"] for x in lineup["xi"]}
        cap_nm = next((x["name"] for x in lineup["xi"] if x["is_captain"]), None)
        name_to_id = {pl["name"]: pl["id"] for pl in squad}
        arrs, cap_arr = [], None
        for nm in xi_names:
            pid = name_to_id.get(nm)
            rec = squad_sims.get(pid) if pid else None
            if rec and rec["sims"]:
                arrs.append(rec["sims"])
                if nm == cap_nm:
                    cap_arr = rec["sims"]
        if arrs:
            N = min(len(a) for a in arrs)
            totals = []
            for i in range(N):
                t = sum(a[i] for a in arrs)
                if cap_arr:
                    t += cap_arr[i]   # podwojenie kapitana
                totals.append(t)
            st = sorted(totals)
            xi_totals_sample = totals
            gw_outlook = {
                "p10": round(st[int(N * 0.10)]), "p50": round(st[int(N * 0.50)]),
                "p90": round(st[int(N * 0.90)]), "mean": round(sum(totals) / N, 1),
                "sd": round((sum((t - sum(totals) / N) ** 2 for t in totals) / N) ** 0.5, 1),
                "captain": cap_nm,
            }

    brief = {"captain": captain, "captain_matrix": captain_matrix, "gw_outlook": gw_outlook,
             "transfers": trans, "alerts": alerts, "lineup": lineup}

    # ── optymalizator chipów (model: skan roadmap z wagą rotacji) ─────────────
    rot_w = {"nailed": 1.0, "solid": 0.9, "risk": 0.6, "doubt": 0.35, "unknown": 0.8}
    gw_scan = {}
    for p in squad:
        w = rot_w.get((p.get("rotation") or {}).get("level"), 0.8)
        for r in (p.get("roadmap") or []):
            g = r["gw"]
            e = gw_scan.setdefault(g, {"bb": 0.0, "pl": {}, "fix": 0})
            e["bb"] += r["xpts"] * w
            e["pl"][p["name"]] = e["pl"].get(p["name"], 0) + r["xpts"]  # DGW → sumuje 2 mecze
            e["fix"] += 1
    scan = []
    for g in sorted(gw_scan):
        e = gw_scan[g]
        playing = len(e["pl"])
        dgw = e["fix"] > playing
        scan.append({"gw": g, "bb": round(e["bb"], 1), "playing": playing, "dgw": dgw})
    # Bench Boost: max suma 15 tam, gdzie prawie wszyscy grają
    bb_c = [s for s in scan if s["playing"] >= 13]
    bench_boost = None
    if bb_c:
        b = max(bb_c, key=lambda s: s["bb"])
        bench_boost = {"gw": b["gw"], "score": b["bb"], "playing": b["playing"], "dgw": b["dgw"],
                       "note": ("podwójna kolejka — cała ławka gra 2×" if b["dgw"]
                                else "wszyscy grają, wysoka suma z ławką")}
    # Triple Captain: najlepszy pojedynczy zawodnik-kolejka (premia w DGW)
    triple_captain = None
    tc_best = None
    for g in sorted(gw_scan):
        e = gw_scan[g]
        dgw = e["fix"] > len(e["pl"])
        for nm, val in e["pl"].items():
            if tc_best is None or val > tc_best[2]:
                tc_best = (g, nm, val, dgw)
    if tc_best and tc_best[2] >= 6:
        triple_captain = {"gw": tc_best[0], "player": tc_best[1], "xpts": round(tc_best[2], 1),
                          "tripled": round(tc_best[2] * 3, 1), "dgw": tc_best[3]}
    # Free Hit: kolejka z najmniejszą liczbą Twoich grających (BGW)
    free_hit = None
    fh_c = [s for s in scan if s["playing"] < 11]
    if fh_c:
        f_ = min(fh_c, key=lambda s: s["playing"])
        free_hit = {"gw": f_["gw"], "playing": f_["playing"], "blanks": 15 - f_["playing"],
                    "note": f"tylko {f_['playing']} Twoich zawodników gra — Free Hit ratuje pustą kolejkę"}
    chips_opt = {"bench_boost": bench_boost, "triple_captain": triple_captain,
                 "free_hit": free_hit, "scan": scan}

    # ── PLANER: DGW/BGW, chipy, różnicowi, ceny, rywale, pula symulatora ──────
    # double / blank gameweeks — skan kolejnych 8 kolejek z pełnego terminarza
    horizon_gws = list(range(next_gw, next_gw + 8))
    counts = {gw: {} for gw in horizon_gws}
    for f in fixtures:
        ev = f.get("event")
        if ev in counts and not f.get("finished"):
            for t in (f["team_h"], f["team_a"]):
                counts[ev][t] = counts[ev].get(t, 0) + 1
    dgw_bgw = []
    for gw in horizon_gws:
        c = counts[gw]
        if not c:
            continue
        doubles = [tshort[t] for t, n in c.items() if n >= 2]
        playing = set(c.keys())
        blanks = [tshort[t] for t in teams if t not in playing] if len(playing) < 20 else []
        if doubles or blanks:
            dgw_bgw.append({"gw": gw, "doubles": sorted(doubles),
                            "blanks": sorted(blanks),
                            "my_doubles": sorted({p["team"] for p in xi if p["team"] in doubles}),
                            "my_blanks": sorted({p["team"] for p in xi if p["team"] in blanks})})

    # doradca chipów (heurystyki, jasno oznaczone)
    bench_xpts = round(sum(p["xpts"] for p in squad if p["on_bench"]), 1)
    next_dgw = next((d for d in dgw_bgw if d["doubles"]), None)
    next_bgw = next((d for d in dgw_bgw if d["blanks"]), None)
    n_flags = len(alerts)
    chips = []
    if bench_xpts >= 16:
        chips.append({"chip": "Bench Boost", "ready": True,
                      "reason": f"Ławka prognozuje {bench_xpts} pkt — wysoko. Dobry moment, zwłaszcza w double gameweeku."})
    if captain and captain["best_xpts"] >= 7:
        extra = " (jeszcze lepiej w DGW)" if next_dgw else ""
        chips.append({"chip": "Triple Captain", "ready": bool(next_dgw),
                      "reason": f"{captain['best']} ma wysoką prognozę ({captain['best_xpts']}){extra}."})
    if next_dgw and len(next_dgw["my_doubles"]) < 4:
        chips.append({"chip": "Wildcard / Free Hit", "ready": True,
                      "reason": f"DGW w GW{next_dgw['gw']} ({', '.join(next_dgw['doubles'][:6])}…) — masz tylko {len(next_dgw['my_doubles'])} drużyn z dubletem. Rozważ przebudowę."})
    if next_bgw and len(next_bgw["my_blanks"]) >= 4:
        chips.append({"chip": "Free Hit", "ready": True,
                      "reason": f"BGW w GW{next_bgw['gw']} — {len(next_bgw['my_blanks'])} Twoich drużyn nie gra. Free Hit ratuje kolejkę."})
    if n_flags >= 4:
        chips.append({"chip": "Wildcard", "ready": True,
                      "reason": f"{n_flags} zawodników z flagami (kontuzje/wątpliwości). Wildcard porządkuje skład."})
    if not chips:
        chips.append({"chip": "Trzymaj chipy", "ready": False,
                      "reason": "Brak wyraźnego sygnału w tej i najbliższych kolejkach. Zachowaj chipy na DGW/BGW."})

    # różnicowi: wysokie xPts_h, niska własność, nie w składzie
    diffs = []
    for lst in pool_by_pos.values():
        for c in lst:
            try:
                own = float(c["selected_by"])
            except (TypeError, ValueError):
                own = 100.0
            if c["id"] not in owned and own < 10.0 and c["status"] == "a":
                diffs.append({**c, "own": own})
    diffs.sort(key=lambda c: -c["xpts_h"])
    POS = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}
    differentials = [{"name": c["name"], "team": c["team"], "pos": POS.get(c["et"], ""),
                      "price": c["price"], "xpts_h": c["xpts_h"], "selected_by": c["selected_by"],
                      "next": c["next"]} for c in diffs[:6]]

    # predyktor zmian cen: skalowany własnością (przybliżony — algorytm FPL jest tajny)
    TP = boot.get("total_players") or 10_000_000
    scored = []
    for e in boot["elements"]:
        if (e.get("minutes") or 0) < 1:
            continue
        pp = price_pred(e, TP)
        if pp["dir"] != "stable" or abs(pp["score"]) >= 0.5:
            scored.append({"name": e["web_name"], "team": tshort[e["team"]],
                           "price": (e["now_cost"] or 0) / 10.0, "dir": pp["dir"],
                           "score": pp["score"], "net": pp["net"], "moved": pp["moved"],
                           "owned": e["id"] in owned, "own": e.get("selected_by_percent")})
    rises = sorted([x for x in scored if x["dir"] == "rise"], key=lambda x: -x["score"])[:8]
    falls = sorted([x for x in scored if x["dir"] == "fall"], key=lambda x: -x["score"])[:8]
    my_moves = [{"name": p["name"], "team": p["team"], "dir": p["price_pred"]["dir"],
                 "score": p["price_pred"]["score"], "net": p["price_pred"]["net"]}
                for p in squad if p["price_pred"]["dir"] != "stable"]
    price_watch = {"rises": rises, "falls": falls, "mine": my_moves}
    # zgodność wsteczna: prosty price_risk (używany w Planerze)
    price_risk = ([{"name": m["name"], "team": m["team"], "dir": "fall", "mom": m["net"], "owned": True}
                   for m in my_moves if m["dir"] == "fall"]
                  + [{"name": r["name"], "team": r["team"], "dir": "rise", "mom": r["net"], "owned": r["owned"]}
                     for r in rises[:5]])

    # skauting rywali (z config.json: "rivals": [id, ...])
    rivals_cfg = cfg.get("rivals") or []
    rivals = []
    my_ids = {p["id"] for p in squad}
    for rid in rivals_cfg[:5]:
        rid = str(rid).strip()
        rentry = get_json(f"{FPL}/entry/{rid}/") or {}
        rpk = get_json(f"{FPL}/entry/{rid}/event/{cur_gw}/picks/")
        if not (rpk and rpk.get("picks")):
            continue
        rids = {pk["element"] for pk in rpk["picks"] if pk["position"] <= 11}
        rcap = next((players[pk["element"]]["web_name"] for pk in rpk["picks"] if pk["is_captain"]), None)
        rxpts = 0.0
        for pk in rpk["picks"]:
            if pk["position"] > 11:
                continue
            rp = players.get(pk["element"])
            if not rp:
                continue
            nf = next_fix(rp["team"])
            x, _ = compute_xpts(rp, nf["opp_id"] if nf else None, (nf["ven"] == "H") if nf else True, rp["team"])
            rxpts += x * (2 if pk["is_captain"] else 1)
        they_have = [players[i]["web_name"] for i in (rids - my_ids) if i in players][:6]
        you_have = [players[i]["web_name"] for i in ({p["id"] for p in xi} - rids) if i in players][:6]
        rivals.append({"id": rid, "name": rentry.get("name", "?"),
                       "player": (rentry.get("player_first_name", "") + " " + rentry.get("player_last_name", "")).strip(),
                       "xi_xpts": round(rxpts, 1), "captain": rcap,
                       "they_have": they_have, "you_have": you_have})

    # pula do symulatora „co jeśli" (slim, wszyscy dostępni)
    sim_pool = []
    for et, lst in pool_by_pos.items():
        for c in lst:
            sim_pool.append({"id": c["id"], "name": c["name"], "team": c["team"], "et": et,
                             "pos": POS[et], "price": c["price"], "xpts": c["xpts"],
                             "xpts_h": c["xpts_h"], "own": c["selected_by"],
                             "owned": c["id"] in owned, "next": c["next"],
                             "factors": c.get("factors"), "roadmap": c.get("roadmap")})

    # planer wieloetapowy (beam search) — kogo brać teraz vs czekać
    plan_gws = sorted({r["gw"] for p in squad for r in (p.get("roadmap") or [])})[:HORIZON]
    transfer_plan = None
    if squad and plan_gws:
        try:
            transfer_plan = plan_transfers(squad, sim_pool, bank_t, 1, plan_gws)
        except Exception as e:
            sys.stderr.write(f"  ! planer wieloetapowy pominięty: {e}\n")
            transfer_plan = None

    planner = {"dgw_bgw": dgw_bgw, "chips": chips, "chips_opt": chips_opt,
               "transfer_plan": transfer_plan, "differentials": differentials,
               "price_risk": price_risk, "price_watch": price_watch,
               "rivals": rivals, "bank": bank_t / 10.0}

    # ── ranking wartości: xPts na 3 kolejki za milion £ ──────────────────────
    value_picks = []
    for lst in pool_by_pos.values():
        for c in lst:
            if c["status"] == "a" and c["price"] >= 4.0 and c["xpts_h"] > 0:
                value_picks.append({"name": c["name"], "team": c["team"], "pos": POS.get(c["et"], ""),
                                    "price": c["price"], "xpts_h": c["xpts_h"],
                                    "ratio": round(c["xpts_h"] / c["price"], 2),
                                    "owned": c["id"] in owned, "next": c["next"]})
    value_picks.sort(key=lambda v: -v["ratio"])
    value_picks = value_picks[:10]
    planner["value_picks"] = value_picks

    # ── miniligi (z entry, bez dodatkowych zapytań) + tabele prywatnych lig ───
    leagues = []
    for lg in ((entry.get("leagues") or {}).get("classic") or []):
        leagues.append({"id": lg.get("id"), "name": lg.get("name"),
                        "rank": lg.get("entry_rank"), "last_rank": lg.get("entry_last_rank"),
                        "type": lg.get("league_type")})

    leagues_detail = []
    if team_id:
        private = [lg for lg in leagues if lg["type"] == "x"][:3]
        for lg in private:
            st = get_json(f"{FPL}/leagues-classic/{lg['id']}/standings/?page_standings=1")
            if not st:
                continue
            results = (st.get("standings") or {}).get("results") or []
            rows = [{"rank": r.get("rank"), "player": r.get("player_name"),
                     "team": r.get("entry_name"), "total": r.get("total"),
                     "last_rank": r.get("last_rank"),
                     "is_you": str(r.get("entry")) == str(team_id)} for r in results]
            top = rows[:5]
            me_i = next((i for i, r in enumerate(rows) if r["is_you"]), None)
            neigh = rows[max(0, me_i - 1):me_i + 2] if me_i is not None and me_i > 4 else []
            leagues_detail.append({"id": lg["id"], "name": lg["name"], "size": len(rows),
                                   "top": top, "around_you": neigh})

    # ── obrona pozycji: analiza najbliższych goniących ("cover") ──────────────
    cover = None
    if team_id and pos_name:
        # mapa id -> {name,pos,xpts,team} ze składu i puli (dostępni gracze)
        xmap = {}
        for pl in squad:
            xmap[pl["id"]] = {"name": pl["name"], "pos": pl["pos"], "xpts": pl["xpts"], "team": pl["team"]}
        for lst in pool_by_pos.values():
            for c in lst:
                xmap.setdefault(c["id"], {"name": c["name"], "pos": pos_name[c["et"]],
                                          "xpts": c["xpts"], "team": c["team"]})

        def elinfo(eid):
            if eid in xmap:
                return xmap[eid]
            pp = players.get(eid)
            if pp:
                return {"name": pp["web_name"], "pos": pos_name.get(pp["element_type"], "?"),
                        "xpts": 0.0, "team": tshort.get(pp["team"], "?")}
            return {"name": "?", "pos": "?", "xpts": 0.0, "team": "?"}

        # wybór miniligi: prywatna, w której masz najlepszą (najniższą) pozycję
        priv = [lg for lg in leagues if lg["type"] == "x" and lg.get("rank")]
        priv.sort(key=lambda lg: lg["rank"])
        if priv:
            lg = priv[0]
            st = get_json(f"{FPL}/leagues-classic/{lg['id']}/standings/?page_standings=1")
            results = ((st or {}).get("standings") or {}).get("results") or []
            me_i = next((i for i, r in enumerate(results) if str(r.get("entry")) == str(team_id)), None)
            if me_i is not None:
                me_row = results[me_i]
                my_total = me_row.get("total") or 0
                # moja jedenastka + kapitan
                my_xi = {p["id"] for p in squad if not p["on_bench"]}
                my_all = {p["id"] for p in squad}
                my_cap = next((p for p in squad if p.get("is_captain")), None)
                my_cap_id = my_cap["id"] if my_cap else None
                # goniący: poniżej mnie w tabeli, najbliżsi najpierw (max 5)
                chasers_rows = results[me_i + 1: me_i + 6]
                chasers, threat_count, cap_count = [], {}, {}
                own_all, cap_all, chaser_full = {}, {}, set()   # do realnego EO w minilidze
                for r in chasers_rows:
                    rid = r.get("entry")
                    pk = get_json(f"{FPL}/entry/{rid}/event/{cur_gw}/picks/")
                    picks = (pk or {}).get("picks") or []
                    for p in picks:
                        chaser_full.add(p["element"])
                    r_xi = [p["element"] for p in picks if p.get("position", 99) <= 11]
                    r_cap = next((p["element"] for p in picks if p.get("is_captain")), None)
                    for e in r_xi:
                        own_all[e] = own_all.get(e, 0) + 1
                    if r_cap is not None:
                        cap_all[r_cap] = cap_all.get(r_cap, 0) + 1
                    diffs = [e for e in r_xi if e not in my_all]  # ich gracze, których nie masz
                    for e in diffs:
                        threat_count[e] = threat_count.get(e, 0) + 1
                    cap_diff = (r_cap is not None and r_cap != my_cap_id)
                    if cap_diff and r_cap is not None:
                        cap_count[r_cap] = cap_count.get(r_cap, 0) + 1
                    diffs_named = sorted(({"name": elinfo(e)["name"], "xpts": elinfo(e)["xpts"]} for e in diffs),
                                         key=lambda d: -d["xpts"])[:3]
                    # win probability: P(utrzymasz przewagę po tej kolejce)
                    gap = my_total - (r.get("total") or 0)
                    hold = None
                    mean_c = sum(elinfo(e)["xpts"] for e in r_xi)
                    if r_cap is not None:
                        mean_c += elinfo(r_cap)["xpts"]   # kapitan rywala ×2
                    if xi_totals_sample:
                        import statistics as _st
                        sd = _st.pstdev(xi_totals_sample) or 6.0
                        wins = 0
                        for mine in xi_totals_sample:
                            cg = random.gauss(mean_c, sd)     # przybliżenie GW goniącego
                            if mine + gap > cg:
                                wins += 1
                        hold = round(wins / len(xi_totals_sample), 2)
                    chasers.append({
                        "player": r.get("player_name"), "team": r.get("entry_name"),
                        "total": r.get("total"), "gap": gap, "hold": hold,
                        "xi_mean": round(mean_c, 1),
                        "captain": elinfo(r_cap)["name"] if r_cap else "?",
                        "cap_differs": cap_diff, "diffs": diffs_named,
                    })
                # ekspozycja kapitańska: najczęstszy różnicowy kapitan wśród goniących
                cap_exposure = None
                if cap_count:
                    top_cap = max(cap_count.items(), key=lambda kv: (kv[1], elinfo(kv[0])["xpts"]))
                    ci = elinfo(top_cap[0])
                    cap_exposure = {"name": ci["name"], "xpts": ci["xpts"],
                                    "count": top_cap[1], "of": len(chasers)}
                # zagrożenia: ich różnicowi (nie masz), wg xPts
                threats = sorted(({"name": elinfo(e)["name"], "pos": elinfo(e)["pos"],
                                   "team": elinfo(e)["team"], "xpts": elinfo(e)["xpts"],
                                   "owned_by": n, "of": len(chasers)} for e, n in threat_count.items()),
                                 key=lambda t: (-t["owned_by"], -t["xpts"]))[:6]
                # moi chronieni różnicowi: moja XI, której nikt z goniących nie ma
                chaser_all = chaser_full
                protected = sorted(({"name": elinfo(e)["name"], "pos": elinfo(e)["pos"],
                                     "xpts": elinfo(e)["xpts"]} for e in my_xi if e not in chaser_all),
                                   key=lambda d: -d["xpts"])[:6]
                # realne EO w Twojej minilidze (własność + kapitanowanie wśród goniących)
                nch = max(1, len(chasers_rows))
                league_eo = []
                for pl in squad:
                    if pl["on_bench"]:
                        continue
                    eo = 100.0 * (own_all.get(pl["id"], 0) + cap_all.get(pl["id"], 0)) / nch
                    league_eo.append({"name": pl["name"], "pos": pl["pos"], "xpts": pl["xpts"],
                                      "eo": round(eo)})
                league_eo.sort(key=lambda x: -x["eo"])
                eo_template = [x for x in league_eo if x["eo"] >= 60][:6]      # „musisz mieć"
                eo_diff = [x for x in reversed(league_eo) if x["eo"] <= 40][:6]  # Twoja dźwignia
                # rekomendacja pokrycia
                rec = None
                if cap_exposure and cap_exposure["count"] >= max(1, len(chasers) // 2):
                    rec = (f"Największe ryzyko to opaska: {cap_exposure['count']}/{cap_exposure['of']} "
                           f"goniących kapitanuje {cap_exposure['name']} (proj. {cap_exposure['xpts']} xPts). "
                           f"Jeśli go nie masz, rozważ dobranie/skapitanowanie, by zneutralizować skok.")
                elif threats:
                    t0 = threats[0]
                    rec = (f"Najgroźniejszy różnicowy rywali to {t0['name']} "
                           f"({t0['owned_by']}/{t0['of']} goniących, proj. {t0['xpts']} xPts) — "
                           f"pokrycie go zmniejsza wariancję względem pościgu.")
                else:
                    rec = "Brak wyraźnej ekspozycji — Twój skład dobrze pokrywa najbliższych goniących."
                cover = {
                    "league": {"id": lg["id"], "name": lg["name"]},
                    "my_rank": me_row.get("rank"), "my_total": my_total,
                    "my_captain": my_cap["name"] if my_cap else "?",
                    "as_of_gw": cur_gw, "chasers": chasers,
                    "captain_exposure": cap_exposure, "threats": threats,
                    "protected": protected, "recommendation": rec,
                    "eo_template": eo_template, "eo_diff": eo_diff, "eo_n": nch,
                    "leader": me_row.get("rank") == 1,
                }

    trajectory = []
    if team_id:
        hist = get_json(f"{FPL}/entry/{team_id}/history/")
        if hist:
            for h in (hist.get("current") or []):
                trajectory.append({"gw": h.get("event"), "points": h.get("points"),
                                   "total": h.get("total_points"),
                                   "overall_rank": h.get("overall_rank"),
                                   "value": (h.get("value") or 0) / 10.0,
                                   "bank": (h.get("bank") or 0) / 10.0})

    # ── śledzenie skuteczności modelu (trwały log w repo) ─────────────────────
    accuracy = None
    try:
        LOG = ROOT / "model_log.json"
        log = {}
        if LOG.exists():
            try:
                log = json.loads(LOG.read_text(encoding="utf-8"))
            except Exception:
                log = {}
        preds = log.get("gw_preds", {})
        ev_finished = {e["id"]: bool(e.get("finished")) for e in boot["events"]}

        # 1) zapisz prognozę na następny GW (dopóki kolejka nie rozegrana — nadpisuj)
        if lineup and lineup.get("xi") and not ev_finished.get(next_gw, False):
            name_to = {pl["name"]: pl for pl in squad}
            cap_nm2 = next((x["name"] for x in lineup["xi"] if x["is_captain"]), None)
            gp = {}
            for x in lineup["xi"]:
                pl = name_to.get(x["name"])
                if pl:
                    gp[str(pl["id"])] = {"name": pl["name"], "pos": pl["pos"],
                                         "pred": pl["xpts"], "cap": (x["name"] == cap_nm2),
                                         "actual": None}
            preds[str(next_gw)] = {"players": gp, "cap": cap_nm2}

        # 2) uzupełnij realne punkty dla rozegranych już kolejek (backfill)
        es_cache = {}
        def actual_pts(pid, gw):
            if pid not in es_cache:
                es = get_json(f"{FPL}/element-summary/{pid}/") or {}
                m = {}
                for h in es.get("history", []):
                    m.setdefault(h.get("round"), 0)
                    m[h["round"]] += h.get("total_points", 0)  # DGW: sumuj
                es_cache[pid] = m
            return es_cache[pid].get(gw)

        for gw_str, rec in preds.items():
            gw_i = int(gw_str)
            if not ev_finished.get(gw_i, False):
                continue
            for pid, pd in rec.get("players", {}).items():
                if pd.get("actual") is None:
                    ap = actual_pts(int(pid), gw_i)
                    if ap is not None:
                        pd["actual"] = ap

        # 3) policz metryki z rozegranych predykcji
        pairs, per_gw, cap_hits, cap_n = [], [], 0, 0
        for gw_str in sorted(preds, key=lambda x: int(x)):
            rec = preds[gw_str]
            scored = [(pd["pred"], pd["actual"], pd.get("cap"), pd["name"])
                      for pd in rec["players"].values() if pd.get("actual") is not None]
            if not scored:
                continue
            errs = [abs(pr - ac) for pr, ac, _, _ in scored]
            pairs.extend([(pr, ac) for pr, ac, _, _ in scored])
            capd = next(((pr, ac, nm) for pr, ac, c, nm in scored if c), None)
            best_actual = max(ac for _, ac, _, _ in scored)
            if capd is not None:
                cap_n += 1
                if capd[1] >= best_actual:   # kapitan = najlepszy strzelec XI
                    cap_hits += 1
            pred_tot = sum(pr * (2 if c else 1) for pr, ac, c, nm in scored)
            act_tot = sum(ac * (2 if c else 1) for pr, ac, c, nm in scored)
            per_gw.append({"gw": int(gw_str), "pred": round(pred_tot, 1),
                           "actual": round(act_tot, 1), "mae": round(sum(errs) / len(errs), 2),
                           "n": len(scored)})
        n = len(pairs)
        if n:
            mae = round(sum(abs(pr - ac) for pr, ac in pairs) / n, 2)
            bias = round(sum(pr - ac for pr, ac in pairs) / n, 2)
            accuracy = {"n": n, "mae": mae, "bias": bias,
                        "captain_hits": cap_hits, "captain_n": cap_n,
                        "per_gw": per_gw[-10:],
                        "note": "Prognoza vs realne punkty Twojej XI. MAE = średni błąd na zawodnika. "
                                "Bias>0 = model przeszacowuje. Trafność kapitana = jak często typ na opaskę "
                                "był najlepszym strzelcem jedenastki."}
        else:
            accuracy = {"n": 0, "per_gw": [], "note": "Zbieram dane — metryki pojawią się po pierwszej "
                        "rozegranej kolejce od wdrożenia śledzenia."}

        log["gw_preds"] = preds
        log["updated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        sys.stderr.write(f"  ! śledzenie skuteczności pominięte: {e}\n")
        accuracy = None

    # ── predykcje meczów na najbliższą kolejkę (Dixon-Coles) ─────────────────
    match_predictions = []
    for f in sorted(fixtures, key=lambda x: (x.get("event") or 999, x.get("id") or 0)):
        if f.get("event") != next_gw or f.get("finished"):
            continue
        h, a = f.get("team_h"), f.get("team_a")
        if not h or not a:
            continue
        pr = mm["predict"](h, a)
        match_predictions.append({
            "home": tshort.get(h, "?"), "away": tshort.get(a, "?"),
            "lam_h": pr["lam_h"], "lam_a": pr["lam_a"],
            "p_home": pr["p_home"], "p_draw": pr["p_draw"], "p_away": pr["p_away"],
            "cs_home": pr["cs_home"], "cs_away": pr["cs_away"],
            "goals": round(pr["lam_h"] + pr["lam_a"], 1),
        })

    data = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gw": {"current": cur_gw, "next": next_gw, "name": gw_name},
        "entry": {
            "team_id": team_id,
            "name": entry.get("name", ""),
            "player_name": (entry.get("player_first_name", "") + " " + entry.get("player_last_name", "")).strip(),
            "overall_points": entry.get("summary_overall_points"),
            "overall_rank": entry.get("summary_overall_rank"),
            "bank": (bank_override if bank_override is not None else entry.get("last_deadline_bank") or 0) / 10.0 if (bank_override is not None or entry.get("last_deadline_bank") is not None) else None,
            "value": (entry.get("last_deadline_value") or 0) / 10.0 if entry.get("last_deadline_value") is not None else None,
        },
        "squad": squad,
        "ticker": ticker,
        "concentration": concentration,
        "brief": brief,
        "planner": planner,
        "leagues": leagues,
        "leagues_detail": leagues_detail,
        "cover": cover,
        "accuracy": accuracy,
        "match_predictions": match_predictions,
        "match_model": {"fitted": mm["fitted"], "n_matches": mm["n_matches"]},
        "trajectory": trajectory,
        "pool": sim_pool,
        "totals": {"xi_xpts": round(xi_xpts, 1), "captain": cap_name},
        "note": "Terminarz i FDR z oficjalnego API FPL. xPts to model heurystyczny (nie oficjalny).",
    }

    out = ROOT / "data.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ zapisano {out}  ({len(squad)} zawodnikow, {len(ticker)} klubow w tickerze)")


if __name__ == "__main__":
    build()
