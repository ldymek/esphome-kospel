"""kospel_engine — deterministic planning & analytics engine for the Kospel AI caretaker.

Pure Python (no AppDaemon / numpy): thermal RC model, tank model, price+usage planner,
savings counterfactual, degradation detectors, backtest. The AppDaemon app (kospel_llm.py)
feeds it data and decides — via input_select.kospel_planer — whether the LLM, this engine,
or both (engine plans, LLM verifies) produce the heater programs.

Slot format everywhere: list of (start_min, stop_min, level) with level 1=Ochrona 2=Komfort
3=Komfort- 4=Komfort+ (the heater's daily-program semantics); max 5 slots per program.
"""
import math, time

OCHRONA, KOMFORT, KOMFORT_MINUS, KOMFORT_PLUS = 1, 2, 3, 4
LEVEL_NAME = {1: "Ochrona", 2: "Komfort", 3: "Komfort-", 4: "Komfort+"}

# ----------------------------------------------------------------- small numerics
def _solve3(A, b):
    """Gaussian elimination for a 3x3 system; returns None if singular."""
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    n = 3
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-12: return None
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1): M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]

def resample(series, t0, t1, dt):
    """Step-hold resample of [(ts, value)] onto a grid t0..t1 (seconds). None where no data yet."""
    out, i, cur = [], 0, None
    s = sorted(series)
    t = t0
    while t <= t1:
        while i < len(s) and s[i][0] <= t:
            cur = s[i][1]; i += 1
        out.append(cur); t += dt
    return out

def slope_per_day(series):
    """Least-squares slope (units/day) of [(ts, value)]; None if <5 points."""
    if len(series) < 5: return None
    xs = [(t - series[0][0]) / 86400.0 for t, _ in series]; ys = [v for _, v in series]
    n = len(xs); mx = sum(xs) / n; my = sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den < 1e-9: return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den

# ----------------------------------------------------------------- thermal model
class ThermalModel:
    """dT_in/dt = a*(T_out - T_in) + b*P_co + c   (a: loss rate 1/s, b: heating gain K/s/kW,
    c: internal/solar gains K/s). Fitted by least squares on 10-min resampled history."""
    def __init__(self, d=None):
        d = d or {}
        self.a, self.b, self.c = d.get("a"), d.get("b"), d.get("c")
        self.n, self.rmse, self.fitted_at = d.get("n", 0), d.get("rmse"), d.get("fitted_at")
        self.heating_seen = d.get("heating_seen", False)

    def to_dict(self):
        return {"a": self.a, "b": self.b, "c": self.c, "n": self.n, "rmse": self.rmse,
                "fitted_at": self.fitted_at, "heating_seen": self.heating_seen}

    def ok(self): return self.a is not None and self.a > 0 and self.n >= 48
    def tau_h(self): return (1.0 / self.a) / 3600.0 if self.ok() else None

    def fit(self, tin, tout, pco, t0, t1, dt=600):
        """tin/tout/pco: [(ts,val)] series. Returns True if a usable model was fitted."""
        Ti = resample(tin, t0, t1, dt); To = resample(tout, t0, t1, dt); P = resample(pco, t0, t1, dt)
        rows = []
        for k in range(len(Ti) - 1):
            if None in (Ti[k], Ti[k + 1], To[k]): continue
            p = P[k] if P[k] is not None else 0.0
            dT = (Ti[k + 1] - Ti[k]) / dt
            if abs(dT) > 0.01: continue                 # >36 K/h = sensor glitch / window
            rows.append((To[k] - Ti[k], p, dT))
        self.n = len(rows)
        self.heating_seen = sum(1 for r in rows if r[1] > 0.5) >= 12
        if self.n < 48: return False
        # normal equations for [a, b, c]
        A = [[0.0] * 3 for _ in range(3)]; B = [0.0] * 3
        for x1, x2, y in rows:
            X = (x1, x2, 1.0)
            for i in range(3):
                B[i] += X[i] * y
                for j in range(3): A[i][j] += X[i] * X[j]
        if not self.heating_seen:                        # no heating in window: fit a, c only
            sol2 = _solve3([[A[0][0], A[0][2], 0], [A[2][0], A[2][2], 0], [0, 0, 1]], [B[0], B[2], 0])
            if not sol2: return False
            a, c = sol2[0], sol2[1]; b = self.b if self.b else None
        else:
            sol = _solve3(A, B)
            if not sol: return False
            a, b, c = sol
            if b < 0: b = 0.0
        if a <= 0 or a > 1.0 / 600: return False         # tau must be > 10 min and positive
        self.a, self.b, self.c = a, b, c
        err = [(y - (a * x1 + (b or 0) * x2 + c)) ** 2 for x1, x2, y in rows]
        self.rmse = math.sqrt(sum(err) / len(err)) * 3600  # K/h
        self.fitted_at = time.time()
        return True

    def coast_hours(self, tin0, tout, tmin):
        """Hours until the room cools from tin0 to tmin with heating OFF (24 if it never does)."""
        if not self.ok(): return None
        teq = tout + (self.c or 0) / self.a
        if tin0 <= tmin: return 0.0
        if teq >= tmin: return 24.0
        return min(24.0, -math.log((tmin - teq) / (tin0 - teq)) / self.a / 3600.0)

    def preheat_hours(self, tin0, ttarget, tout, p_kw):
        """Hours of heating at p_kw to lift the room from tin0 to ttarget; None if unreachable."""
        if not self.ok() or not self.b: return None
        teq = tout + ((self.c or 0) + self.b * p_kw) / self.a
        if teq <= ttarget or tin0 >= ttarget: return 0.0 if tin0 >= ttarget else None
        return -math.log((ttarget - teq) / (tin0 - teq)) / self.a / 3600.0

# ----------------------------------------------------------------- tank model
class TankModel:
    """DHW tank: heat-up rate (K/h per kW while heating) and standing loss (K/h idle)."""
    def __init__(self, d=None):
        d = d or {}
        self.rate_per_kw = d.get("rate_per_kw", 3.0)   # defaults for a ~120 L tank
        self.loss_kh = d.get("loss_kh", 1.0)
        self.n = d.get("n", 0); self.fitted_at = d.get("fitted_at")
        self.baseline_rate = d.get("baseline_rate")     # first good fit, for degradation checks

    def to_dict(self):
        return {"rate_per_kw": self.rate_per_kw, "loss_kh": self.loss_kh, "n": self.n,
                "fitted_at": self.fitted_at, "baseline_rate": self.baseline_rate}

    def fit(self, dhw, pcwu, t0, t1, dt=300):
        T = resample(dhw, t0, t1, dt); P = resample(pcwu, t0, t1, dt)
        heat, idle = [], []
        for k in range(len(T) - 1):
            if None in (T[k], T[k + 1]): continue
            p = P[k] if P[k] is not None else 0.0
            dTh = (T[k + 1] - T[k]) / dt * 3600.0
            if p >= 2.0 and dTh > 0.5: heat.append(dTh / p)
            elif p < 0.2 and -6.0 < dTh < 0: idle.append(-dTh)   # exclude draws (fast drops)
        self.n = len(heat)
        if len(heat) >= 10:
            heat.sort(); self.rate_per_kw = heat[len(heat) // 2]
            if self.baseline_rate is None: self.baseline_rate = self.rate_per_kw
        if len(idle) >= 20:
            idle.sort(); self.loss_kh = idle[len(idle) // 2]
        self.fitted_at = time.time()
        return len(heat) >= 10

    def minutes_to_heat(self, t0, target, p_kw):
        if target <= t0: return 0
        return int(math.ceil((target - t0) / max(self.rate_per_kw * p_kw, 1.0) * 60))

    def degradation_pct(self):
        if not self.baseline_rate or not self.n: return None
        return round((1 - self.rate_per_kw / self.baseline_rate) * 100, 1)

# ----------------------------------------------------------------- planner
PREF = {   # idle = outside comfort windows (None -> heater economic setpoint, costs no slot);
           # peak = expensive hour inside a comfort window when the building can NOT coast;
           # tiers = power-plan index (exp, normal, cheap); tmin_off = tolerated drop below setpoint
    "Oszczędność": {"idle": None, "peak": OCHRONA, "tiers": (0, 1, 3), "tmin_off": 1.5},
    "Balans":      {"idle": None, "peak": KOMFORT_MINUS, "tiers": (0, 2, 3), "tmin_off": 1.0},
    "Komfort":     {"idle": KOMFORT_MINUS, "peak": KOMFORT, "tiers": (2, 3, 3), "tmin_off": 0.5},
}
DEFAULT_COMFORT_WINDOWS = [(6, 9), (16, 22)]

def _compress(levels_by_hour, max_slots=5):
    """24 hourly levels (None = outside programme -> heater's economic setpoint) -> <=max_slots
    (start_min, stop_min, level). Over budget: drop the shortest cool slot (Ochrona/Komfort-) to a
    gap, else merge the shortest slot into its longer neighbour ADOPTING THE NEIGHBOUR'S level —
    a merge may never produce a warmer span than either part (no Komfort+ through a price peak)."""
    slots = []
    for h, lv in enumerate(levels_by_hour):
        if lv is None: continue
        if slots and slots[-1][2] == lv and slots[-1][1] == h * 60:
            slots[-1] = (slots[-1][0], (h + 1) * 60, lv)
        else:
            slots.append((h * 60, (h + 1) * 60, lv))
    while len(slots) > max_slots:
        i = min(range(len(slots)), key=lambda k: slots[k][1] - slots[k][0])
        s = slots[i]
        if s[2] in (OCHRONA, KOMFORT_MINUS):
            del slots[i]; continue
        nb = [k for k in (i - 1, i + 1) if 0 <= k < len(slots)]
        j = max(nb, key=lambda k: slots[k][1] - slots[k][0])
        a, b = sorted([slots[i], slots[j]])
        slots[min(i, j)] = (a[0], b[1], slots[j][2]); del slots[max(i, j)]
        # re-merge equal neighbours
        k = 0
        while k + 1 < len(slots):
            if slots[k][2] == slots[k + 1][2] and slots[k][1] == slots[k + 1][0]:
                slots[k] = (slots[k][0], slots[k + 1][1], slots[k][2]); del slots[k + 1]
            else: k += 1
    return [(s, min(e, 1439), lv) for s, e, lv in slots]

def human(slots):
    return [f"{a//60:02d}:{a%60:02d}-{b//60:02d}:{b%60:02d} {LEVEL_NAME[l]}" for a, b, l in slots]

def usage_clusters(usage, frac=0.15):
    """Hours with meaningful hot-water draws grouped into consecutive clusters [(h_start, h_end_incl)]."""
    mx = max(usage) if usage else 0
    if mx <= 0: return []
    hot = [h for h in range(24) if usage[h] >= max(1.0, frac * mx)]
    cl = []
    for h in hot:
        if cl and h == cl[-1][1] + 1: cl[-1] = (cl[-1][0], h)
        else: cl.append((h, h))
    return cl

def plan(hours, usage, thermal, tank, pref="Balans", away=False, bias=None,
         tin=None, tout=None, battery=False, comfort_windows=None):
    """hours: 24 dicts {price, cheap, exp} indexed by LOCAL hour (rolling day, missing -> None).
    Returns dict: CO/CWU/Cyrkulacja slots, power_plan[24], rationale[], model_used."""
    P = PREF.get(pref, PREF["Balans"])
    cw = comfort_windows or DEFAULT_COMFORT_WINDOWS
    bias = bias or [0.0] * 24
    notes = []
    def price(h): return (hours[h] or {}).get("price")
    def exp(h): return bool((hours[h] or {}).get("exp"))
    def cheap(h): return bool((hours[h] or {}).get("cheap"))
    known = [h for h in range(24) if price(h) is not None]

    # ---- CO ----
    if away:
        co = [OCHRONA] * 24
        notes.append("Nikogo w domu -> CO na Ochronie (ochrona przeciwmrozowa budynku działa niezależnie).")
    else:
        co = []
        for h in range(24):
            in_comfort = any(a <= h < b for a, b in cw)
            lv = KOMFORT if in_comfort else P["idle"]
            if exp(h) and in_comfort: lv = P["peak"]
            co.append(lv)
        # preheat: cheap (or non-expensive) hour right before an expensive block -> Komfort+
        # if the building can coast through the block (thermal model) or model unknown & pref != Oszczędność
        for h in range(24):
            if exp(h) and (h == 0 or not exp(h - 1)):
                blk = 1
                while h + blk < 24 and exp(h + blk): blk += 1
                pre = h - 1
                if pre < 0 or exp(pre): continue
                coast = None
                if thermal and thermal.ok() and tin is not None and tout is not None:
                    coast = thermal.coast_hours(tin + 0.8, tout, tin - P["tmin_off"])
                if coast is None:
                    if pref != "Oszczędność": co[pre] = KOMFORT_PLUS; notes.append(f"{pre:02d}:00 Komfort+ (rozgrzanie przed drogim blokiem {h:02d}-{h+blk:02d}; model termiczny jeszcze bez danych grzania)")
                elif coast >= blk:
                    co[pre] = KOMFORT_PLUS
                    for k in range(h, h + blk): co[k] = None    # coast: heater economic floor, no slot
                    notes.append(f"{pre:02d}:00 Komfort+ -> budynek utrzyma komfort przez {blk} h drogiego bloku bez grzania (stała czasowa {thermal.tau_h():.0f} h)")
                else:
                    co[pre] = KOMFORT_PLUS
                    notes.append(f"{pre:02d}:00 Komfort+; blok {h:02d}-{h+blk:02d} za długi na coasting ({coast:.1f} h) -> {LEVEL_NAME[P['peak']]}")
        # override learning: persistent 'za zimno' (bias>0.5) warms a band, 'za ciepło' cools it
        for h in range(24):
            if bias[h] > 0.5 and co[h] in (None, OCHRONA, KOMFORT_MINUS): co[h] = KOMFORT
            if bias[h] < -0.5 and co[h] in (KOMFORT_PLUS, KOMFORT): co[h] = KOMFORT_MINUS
        if any(abs(b) > 0.5 for b in bias): notes.append("Uwzględniono nauczone preferencje (za zimno/za ciepło).")
    co_slots = _compress(co)

    # ---- CWU ----
    cl = usage_clusters(usage)
    cwu = [None] * 24
    if cl:
        for (hs, he) in cl:
            if any(cwu[k] == KOMFORT for k in range(max(0, hs - 6), hs + 1)):
                notes.append(f"CWU: pobór {hs:02d}-{he+1:02d} pokryty wcześniejszym nagrzaniem (zasobnik trzyma ciepło)")
                continue
            win = list(range(max(0, hs - 5), hs + 1))
            cands = [h for h in win if not exp(h)]
            if not cands:
                cands = win; notes.append(f"CWU: przed poborem {hs:02d}-{he+1:02d} tylko drogie godziny -> najtańsza z nich")
            best = min(cands, key=lambda h: (price(h) if price(h) is not None else 9) - (0.05 if cheap(h) else 0) + 0.03 * (hs - h))  # later = less standing loss
            dur_h = 1
            if tank:
                dur_h = max(1, int(math.ceil(tank.minutes_to_heat(38.0, 48.0, 20.0) / 60.0)))
            for k in range(best, min(24, best + dur_h)): cwu[k] = KOMFORT
            notes.append(f"CWU: nagrzewanie o {best:02d}:00 przed poborem {hs:02d}-{he+1:02d}" + (f" ({price(best)} zł/kWh)" if price(best) is not None else ""))
    else:
        for h in (6, 17): cwu[h] = KOMFORT
        notes.append("CWU: brak profilu poboru -> domyślne 06:00 i 17:00")
    battery_hour = None
    if battery and known:
        hb = min(known, key=lambda h: price(h))
        cwu[hb] = KOMFORT; battery_hour = hb
        notes.append(f"Magazyn ciepła: dogrzanie zasobnika w najtańszej godzinie {hb:02d}:00 ({price(hb)} zł/kWh)")
    if away:
        cwu = [None] * 24; cwu[6] = KOMFORT
        notes.append("Nikogo w domu -> CWU tylko jedno podtrzymanie o 06:00.")
    cwu_slots = _compress(cwu)

    # ---- circulation ----
    circ = [None] * 24
    if not away:
        if cl:
            for hs, he in cl:
                for k in range(hs, min(24, he + 1)): circ[k] = KOMFORT
            notes.append("Cyrkulacja tylko w godzinach realnego poboru.")
        else:
            for k in list(range(6, 9)) + list(range(17, 23)): circ[k] = KOMFORT
    circ_slots = _compress(circ, max_slots=4)

    # ---- power plan ----
    t_exp, t_norm, t_cheap = P["tiers"]
    power = []
    for h in range(24):
        if price(h) is None: power.append(t_norm)
        elif exp(h): power.append(t_exp)
        elif cheap(h): power.append(t_cheap)
        else: power.append(t_norm)
    return {"CO": co_slots, "CWU": cwu_slots, "Cyrkulacja": circ_slots, "power_plan": power,
            "rationale": notes, "pref": pref, "away": away, "battery_hour": battery_hour,
            "model": {"thermal_ok": bool(thermal and thermal.ok()), "tau_h": thermal.tau_h() if thermal and thermal.ok() else None,
                      "tank_rate_per_kw": tank.rate_per_kw if tank else None}}

def validate_slots(slots):
    """Same hard rules as the LLM path: 0<=a<b<=1439, level 1..4, sorted, non-overlapping, <=5."""
    clean, last = [], -1
    for s in sorted(slots, key=lambda x: x[0]):
        try: a, b, v = int(s[0]), int(s[1]), int(s[2])
        except Exception: continue
        if not (0 <= a < b <= 1439 and 1 <= v <= 4 and a >= last): continue
        clean.append((a, b, v)); last = b
        if len(clean) == 5: break
    return clean

# ----------------------------------------------------------------- savings / backtest
def hourly_kwh_from_total(total_series, day_start, tz_off_s):
    """[(ts, kWh_total)] -> 24 hourly deltas for the local day starting at day_start (utc secs)."""
    vals = resample(total_series, day_start, day_start + 24 * 3600, 3600)
    out = []
    for k in range(24):
        a, b = vals[k], vals[k + 1] if k + 1 < len(vals) else None
        out.append(round(max(0.0, b - a), 3) if (a is not None and b is not None) else 0.0)
    return out

def counterfactual(kwh, prices, flat):
    """Actual hourly cost vs 'same kWh at the day's mean price' vs flat tariff."""
    pairs = [(k, p) for k, p in zip(kwh, prices) if p is not None]
    if not pairs: return None
    tot = sum(k for k, _ in pairs)
    mean_p = sum(p for _, p in pairs) / len(pairs)
    actual = sum(k * p for k, p in pairs)
    return {"kwh": round(tot, 2), "koszt": round(actual, 2), "koszt_srednia": round(tot * mean_p, 2),
            "koszt_taryfa_plaska": round(tot * flat, 2),
            "oszczednosc_vs_srednia": round(tot * mean_p - actual, 2),
            "oszczednosc_vs_plaska": round(tot * flat - actual, 2),
            "srednia_cena": round(mean_p, 3),
            "efektywna_cena": round(actual / tot, 3) if tot > 0 else None}

def backtest(plan_out, kwh_cwu, kwh_co, prices):
    """Estimate: move yesterday's CWU kWh into the planned CWU windows and CO kWh into hours with
    level>=Komfort (weighted equally), price them, compare with actual. A rough what-if, labelled so."""
    def hours_of(slots, min_level):
        hs = set()
        for a, b, l in slots:
            if l >= min_level or l == KOMFORT:
                for h in range(a // 60, min(24, (b + 59) // 60)): hs.add(h)
        return sorted(hs)
    def redistribute(kwh, hs):
        tot = sum(kwh)
        if not hs or tot == 0: return kwh
        out = [0.0] * 24
        for h in hs: out[h] = tot / len(hs)
        return out
    cwu_h = hours_of(plan_out["CWU"], KOMFORT)
    co_h = [h for a, b, l in plan_out["CO"] if l in (KOMFORT, KOMFORT_PLUS) for h in range(a // 60, min(24, (b + 59) // 60))] or list(range(24))
    sim = [x + y for x, y in zip(redistribute(kwh_cwu, cwu_h), redistribute(kwh_co, sorted(set(co_h))))]
    act = [x + y for x, y in zip(kwh_cwu, kwh_co)]
    c_act = sum(k * p for k, p in zip(act, prices) if p is not None)
    c_sim = sum(k * p for k, p in zip(sim, prices) if p is not None)
    return {"koszt_rzeczywisty": round(c_act, 2), "koszt_wg_planu_silnika": round(c_sim, 2),
            "roznica": round(c_act - c_sim, 2), "uwaga": "szacunek: to samo zużycie kWh przesunięte w okna planu"}
