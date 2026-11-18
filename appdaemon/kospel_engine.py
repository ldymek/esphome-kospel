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

def resample(series, t0, t1, dt, max_hold=None):
    """Step-hold resample of [(ts, value)] onto a grid t0..t1 (seconds). None where no data yet and,
    with max_hold (s), where the last sample is older than that: a dead sensor or a history query that
    returned less than the window must not become days of a flat-lining constant."""
    out, i, cur, cur_t = [], 0, None, None
    s = sorted(series)
    t = t0
    while t <= t1:
        while i < len(s) and s[i][0] <= t:
            cur, cur_t = s[i][1], s[i][0]; i += 1
        out.append(cur if max_hold is None or (cur_t is not None and t - cur_t <= max_hold) else None)
        t += dt
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
# Trust gate for decisions that SKIP heating (coasting through the price peak). Lesson 2026-10-08..17:
# the app's history query returned only the first 24 h of the 7-day window (HA /history/period/<start>
# without end_time = start + 1 day); step-hold turned the other 6 days into a constant room temperature,
# so 18 of 21 fits had heating_seen=False and tau jumped 3.4 h .. 497 h between refits, yet every one
# was ok() and drove coasting. Re-fitting the same 7-day windows on the full history gives tau 45-108 h
# (38 windows, 10-08..10-17; 45-95 h once heating is in the data) — the bounds below keep that with a
# ~2x margin and reject the artefacts (3.4 h, 10 h; 226-497 h). A fit is trusted only if it saw heating,
# found a positive heating gain, used >= 3 days of real (not held) rows and was made by this fit version.
TAU_TRUST_H = (20.0, 200.0)
TRUST_MIN_ROWS = 432             # 10-min rows = 72 h of usable data
FIT_MAX_HOLD_S = 6 * 3600        # room/outdoor sensors report at least every ~5.3 h (max normal gap seen)
FIT_VERSION = 2                  # fits without the hold limit (fit_v missing) are never trusted

class ThermalModel:
    """dT_in/dt = a*(T_out - T_in) + b*P_co + c   (a: loss rate 1/s, b: heating gain K/s/kW,
    c: internal/solar gains K/s). Fitted by least squares on 10-min resampled history.
    ok() = a fit exists (published model info); trusted() = good enough to decide coasting."""
    def __init__(self, d=None):
        d = d or {}
        self.a, self.b, self.c = d.get("a"), d.get("b"), d.get("c")
        self.n, self.rmse, self.fitted_at = d.get("n", 0), d.get("rmse"), d.get("fitted_at")
        self.heating_seen = d.get("heating_seen", False)
        self.fit_v = d.get("fit_v")

    def to_dict(self):
        return {"a": self.a, "b": self.b, "c": self.c, "n": self.n, "rmse": self.rmse,
                "fitted_at": self.fitted_at, "heating_seen": self.heating_seen, "fit_v": self.fit_v}

    def ok(self): return self.a is not None and self.a > 0 and self.n >= 48
    def tau_h(self): return (1.0 / self.a) / 3600.0 if self.ok() else None

    def trust_issue(self):
        """None if the model may decide to leave hours unheated; otherwise why not (Polish, for notes)."""
        if not self.ok(): return "model termiczny jeszcze bez dopasowania"
        if (self.fit_v or 0) < FIT_VERSION: return "dopasowanie sprzed poprawki danych (czeka na ponowne dopasowanie)"
        if not self.heating_seen or not self.b or self.b <= 0: return "model jeszcze bez danych grzania"
        if self.n < TRUST_MIN_ROWS: return f"za mało danych ({self.n} próbek < {TRUST_MIN_ROWS})"
        tau = self.tau_h()
        if not TAU_TRUST_H[0] <= tau <= TAU_TRUST_H[1]:
            return f"stała czasowa {tau:.0f} h poza wiarygodnym zakresem {TAU_TRUST_H[0]:.0f}-{TAU_TRUST_H[1]:.0f} h"
        return None

    def trusted(self): return self.trust_issue() is None

    def fit(self, tin, tout, pco, t0, t1, dt=600):
        """tin/tout/pco: [(ts,val)] series. Returns True if a usable model was fitted."""
        self.fit_v = None                                # a failed refit is not trusted on stale parameters
        Ti = resample(tin, t0, t1, dt, max_hold=FIT_MAX_HOLD_S); To = resample(tout, t0, t1, dt, max_hold=FIT_MAX_HOLD_S)
        P = resample(pco, t0, t1, dt)                    # power: constant for days is real (no state changes)
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
        self.fit_v = FIT_VERSION
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
    "Oszczędność": {"idle": None, "peak": OCHRONA, "tiers": (0, 1, 3), "tmin_off": 1.5, "cwu_komfort_cap_h": 3,
                    "cwu_peak": None, "cwu_night_off": True, "circ_per_cluster": 1, "circ_exp_cap_h": 1, "circ_day_cap_h": 3},
    "Balans":      {"idle": None, "peak": KOMFORT_MINUS, "tiers": (0, 2, 3), "tmin_off": 1.0, "cwu_komfort_cap_h": 4,
                    "cwu_peak": None, "cwu_night_off": True, "circ_per_cluster": 2, "circ_exp_cap_h": 1, "circ_day_cap_h": 4},
    "Komfort":     {"idle": KOMFORT_MINUS, "peak": KOMFORT, "tiers": (2, 3, 3), "tmin_off": 0.5, "cwu_komfort_cap_h": 6,
                    "cwu_peak": None, "cwu_night_off": False, "circ_per_cluster": 3, "circ_exp_cap_h": 2, "circ_day_cap_h": 5},
}
# Lesson 2026-09-01: a 5 h circulation window through the 17-22 price peak (pump drains the tank
# ~3 K/h) made the heater top the tank up every hour at 1.5-1.7 zl/kWh.
# Lesson 2026-09-02 (worse): "Ochrona in every expensive hour" starved the tank to 20 C, because the
# CWU level 1 means the heater does NOT heat the tank at all and Pstryk flagged 06-19 as expensive.
# Rules now: CWU level 1 only at night (00-05, Oszczednosc) or when nobody is home; expensive hours
# use the heater's economic setpoint (gap); a Komfort charge goes into the last cheaper hour before
# the day's price peak; circulation only on the strongest draw hours, capped; and a TANK FLOOR
# overrides everything (tank < 35 C -> no level 1 for the next hours, < 30 C -> heat now).
# Lesson 2026-09-03: economic upkeep (39 C) all night = a 20 kW burst every ~4 h; 2 h Komfort slots
# add another. Night window 22-05 without heating (tank drifts ~40 -> ~33), Komfort slots 1 h each.
CWU_NIGHT = list(range(22, 24)) + list(range(0, 5))   # default; see night_window()
CWU_FLOOR_ECON, CWU_FLOOR_HEAT = 35.0, 30.0

def night_window(usage):
    """Hours without DHW heating: from 2 h after the last observed evening draw (>= cluster
    threshold, never before 22:00) until 05:00. Lesson 2026-09-03: a bath at 22:15 met a fixed
    22:00 window and the tank sat at 24 C for 2.5 h."""
    thr = max(1.0, 0.15 * max(usage)) if usage and max(usage) > 0 else 1.0
    last = max([h for h in range(18, 24) if usage and h < len(usage) and usage[h] >= thr] or [19])
    start = max(22, last + 2)
    return [h for h in range(start, 24)] + list(range(0, 5))
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
        nb = [k for k in (i - 1, i + 1) if 0 <= k < len(slots)
              and (slots[k][1] == s[0] or slots[k][0] == s[1])]      # touching neighbours only
        if not nb:
            del slots[i]; continue                                    # never bridge a gap with a warm level
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

def _compress_q(levels_q, max_slots=5, qmin=15):
    """Like _compress but on fixed buckets of `qmin` minutes (96 quarters for a day)."""
    slots = []
    for i, lv in enumerate(levels_q):
        if lv is None: continue
        a, b = i * qmin, (i + 1) * qmin
        if slots and slots[-1][2] == lv and slots[-1][1] == a: slots[-1] = (slots[-1][0], b, lv)
        else: slots.append((a, b, lv))
    while len(slots) > max_slots:
        i = min(range(len(slots)), key=lambda k: slots[k][1] - slots[k][0])
        s = slots[i]
        if s[2] in (OCHRONA, KOMFORT_MINUS): del slots[i]; continue
        nb = [k for k in (i - 1, i + 1) if 0 <= k < len(slots)
              and (slots[k][1] == s[0] or slots[k][0] == s[1])]      # touching neighbours only
        if not nb:
            del slots[i]; continue                                    # never bridge a gap with a warm level
        j = max(nb, key=lambda k: slots[k][1] - slots[k][0])
        a, b = sorted([slots[i], slots[j]])
        slots[min(i, j)] = (a[0], b[1], slots[j][2]); del slots[max(i, j)]
        k = 0
        while k + 1 < len(slots):
            if slots[k][2] == slots[k + 1][2] and slots[k][1] == slots[k + 1][0]:
                slots[k] = (slots[k][0], slots[k + 1][1], slots[k][2]); del slots[k + 1]
            else: k += 1
    return [(s, min(e, 1439), lv) for s, e, lv in slots]

def _to_quarters(slots):
    q = [None] * 96
    for a, b, lv in slots:
        for i in range(max(0, a // 15), min(96, (b + 14) // 15)): q[i] = lv
    return q

# ---- slot budget for CWU / circulation (never through _compress/_compress_q: those merge a short slot into
# its neighbour ADOPTING the neighbour's level — right for CO, fatal for the tank). Lesson 2026-10-09/12: night
# Ochrona 00-0x + the 23:00 Ochrona tail + 1 h charges = 6 slots; the merge turned the 04:00 charge into Ochrona
# (no heating before the 06:00 showers) or deleted the morning charge, while the notes said it was added.
def _runs(levels, qmin):
    """Per-bucket levels (None = gap) -> [(start_min, stop_min, level)] of equal consecutive buckets."""
    slots = []
    for i, lv in enumerate(levels):
        if lv is None: continue
        a, b = i * qmin, (i + 1) * qmin
        if slots and slots[-1][2] == lv and slots[-1][1] == a: slots[-1] = (slots[-1][0], b, lv)
        else: slots.append((a, b, lv))
    return slots

def _span(s): return f"{s[0]//60:02d}:{s[0]%60:02d}-{min(s[1], 1439)//60:02d}:{min(s[1], 1439)%60:02d}"

def _cwu_needs(usage, pk=None):
    """What a CWU Komfort charge is for: each draw cluster (a charge in the 4 h before it or in its first
    hour — the enforce_rules (b2) window) and the pre-peak charge (the 3 h before the price peak).
    -> [(label, start_min, stop_min)] windows."""
    needs = [(f"pobór {hs:02d}-{he + 1:02d}", max(0, hs - 4) * 60, (hs + 1) * 60) for hs, he in usage_clusters(usage)]
    if pk: needs.append((f"szczyt cen {pk[0]:02d}-{pk[1]:02d}", max(0, pk[0] - 3) * 60, pk[0] * 60))
    return needs

def _charge(s): return s[2] in (KOMFORT, KOMFORT_PLUS)
def _serves(s, need): return _charge(s) and s[0] < need[2] and s[1] > need[1]

def _fit_cwu(slots, max_slots=5, usage=None, pk=None, now_hour=None, tank_temp=None):
    """CWU slots over the heater's budget -> (slots, notes). Only DROPS slots, never merges:
    1) Ochrona first, shortest first (the 23:00 tail before the 00-05 block): a dropped Ochrona is a gap =
       the heater's economic 39 °C upkeep — a cost, never a cold tank;
    2) only if the charges alone exceed the budget: the charge of least value (a tank-floor charge NOW is
       kept; then charges that are the only one serving a draw cluster / the pre-peak charge; then the
       stronger coming draws). Lost coverage is reported, never hidden."""
    slots = sorted(slots)
    if len(slots) <= max_slots: return [(a, min(b, 1439), lv) for a, b, lv in slots], []
    needs = _cwu_needs(usage, pk)
    before = {n for n in needs if any(_serves(s, n) for s in slots)}
    dropped = []
    def value(k):
        s, rest = slots[k], slots[:k] + slots[k + 1:]
        floor_now = (now_hour is not None and tank_temp is not None and tank_temp < CWU_FLOOR_HEAT
                     and s[0] <= now_hour * 60 < s[1])
        sole = sum(1 for n in needs if _serves(s, n) and not any(_serves(t, n) for t in rest))
        h1 = (min(s[1], 1440) - 1) // 60
        nxt = sum((usage[x] if x < len(usage) else 0.0) for x in range(h1 + 1, min(24, h1 + 4))) if usage else 0.0
        return (floor_now, sole, nxt, -s[0])
    while len(slots) > max_slots:
        cool = [k for k, s in enumerate(slots) if not _charge(s)]
        k = (min(cool, key=lambda k: (slots[k][1] - slots[k][0], -slots[k][0])) if cool
             else min(range(len(slots)), key=value))
        dropped.append(slots.pop(k))
    notes = []
    och = [_span(s) for s in sorted(dropped) if not _charge(s)]
    chg = [_span(s) for s in sorted(dropped) if _charge(s)]
    if och: notes.append(f"CWU: limit {max_slots} przedziałów sterownika -> pominięta Ochrona {', '.join(och)} "
                         f"(w tych godzinach podtrzymanie ekonomiczne zamiast braku grzania; ładowania zostają)")
    if chg: notes.append(f"CWU: limit {max_slots} przedziałów -> usunięte ładowanie {', '.join(chg)} (najmniej potrzebne)")
    lost = [n[0] for n in needs if n in before and not any(_serves(s, n) for s in slots)]
    if lost: notes.append(f"CWU: UWAGA — przez limit przedziałów bez ładowania: {', '.join(lost)}")
    return [(a, min(b, 1439), lv) for a, b, lv in slots], notes

def _fit_circ(slots, max_slots=4, usage=None):
    """Circulation over budget -> (slots, notes): drop the slot with the weakest observed draws (draw weight
    summed over its quarters), ties -> the shorter, then the later. Circulation is comfort only (hot water
    at the tap sooner); a dropped slot cools the tank less, it never starves it."""
    slots = sorted(slots)
    def weight(s): return sum((usage[i // 4] if usage and i // 4 < len(usage) else 0.0)
                              for i in range(s[0] // 15, min(96, (s[1] + 14) // 15)))
    dropped = []
    while len(slots) > max_slots:
        k = min(range(len(slots)), key=lambda k: (weight(slots[k]), slots[k][1] - slots[k][0], -slots[k][0]))
        dropped.append(slots.pop(k))
    notes = [f"Cyrkulacja: limit {max_slots} przedziałów -> pominięte {', '.join(_span(s) for s in sorted(dropped))} (najsłabszy pobór)"] if dropped else []
    return [(a, min(b, 1439), lv) for a, b, lv in slots], notes

def _price_fns(hours):
    """(price, est): price(h) = the published price or None; est(h) = the price used to CHOOSE a charge hour.
    A missing price is not 'very expensive': in the rolling day it is an hour already past (tomorrow's, until
    the day-ahead prices are out) — valuing it at 9 zł moved the morning charge to 'now' on every morning
    re-plan (2026-10-09/10: charges at 05:01 and again 06:3x). est = the caller's hours[h]['price_est'] if
    supplied, else the median of the known prices (1.0 with no prices at all)."""
    def price(h): return (hours[h] or {}).get("price") if hours and h < len(hours) else None
    kp = sorted(p for p in (price(h) for h in range(24)) if p is not None)
    med = ((kp[len(kp) // 2] if len(kp) % 2 else (kp[len(kp) // 2 - 1] + kp[len(kp) // 2]) / 2) if kp else 1.0)
    def est(h):
        p = price(h)
        if p is not None: return p
        e = (hours[h] or {}).get("price_est") if hours and h < len(hours) else None
        return e if e is not None else med
    return price, est

def _rolling_now(hours):
    """'Now' of a rolling-day price list (kospel_llm.hours_from_prices): until the day-ahead prices are
    published, the hours already past have no price, so an unknown prefix 0..k-1 followed by known k..23
    means now = k. Any other pattern (all known, gaps) -> None."""
    if not hours or len(hours) < 24: return None
    known = [bool(hours[h] and hours[h].get("price") is not None) for h in range(24)]
    if all(known) or not any(known): return None
    k = known.index(True)
    return k if k > 0 and all(known[k:]) else None

def peak_block(hours, max_len=5, rel=0.85):
    """Contiguous block of hours around the day's max price (>= rel*max), <= max_len hours."""
    known = [h for h in range(24) if hours and h < len(hours) and hours[h] and hours[h].get("price") is not None]
    if not known: return None
    hm = max(known, key=lambda h: hours[h]["price"]); pm = hours[hm]["price"]
    a = b = hm
    while a - 1 in known and hours[a - 1]["price"] >= rel * pm and b - a + 1 < max_len: a -= 1
    while b + 1 in known and hours[b + 1]["price"] >= rel * pm and b - a + 1 < max_len: b += 1
    return (a, b + 1)

def enforce_rules(key, slots, hours, pref="Balans", usage=None, tank_temp=None, now_hour=None, away=False):
    """Programmatic guard applied to ANY planner's output (LLM, engine, hybrid amendment) before it
    is written to the heater. Returns (slots, notes). Rules (by preference):
    CWU  - expensive hours are Ochrona (no hourly top-ups in the price peak); a Komfort charge that
           the planner put INSIDE an expensive hour is kept only if it is the plan's only charge.
    Cyrk - circulation in expensive hours capped (circ_exp_cap_h), whole day capped (circ_day_cap_h);
           when trimming, the hours with the strongest observed draws are kept."""
    P = PREF.get(pref, PREF["Balans"])
    notes = []
    if not slots or key not in ("CWU", "Cyrkulacja"): return slots, notes
    price, est = _price_fns(hours)
    pk = peak_block(hours)
    peak = set(range(pk[0], pk[1])) if pk else set()
    def exp(h): return h in peak
    q = _to_quarters(slots)
    if key == "CWU":
        # (0) the DHW timetable knows only 1 (no heating) and 2 (comfort charge); 3/4 have no meaning
        bad = sorted({i // 4 for i in range(96) if q[i] in (KOMFORT_MINUS, KOMFORT_PLUS)})
        for i in range(96):
            if q[i] == KOMFORT_PLUS: q[i] = KOMFORT
            elif q[i] == KOMFORT_MINUS: q[i] = None
        if bad: notes.append(f"CWU: poziomy 3/4 nie istnieją dla CWU -> Komfort- usunięty (ekonomicznie), Komfort+ = Komfort (godziny {bad[0]:02d}-{bad[-1]+1:02d})")
        # (a) level 1 (= tank NOT heated) only at night / when away; elsewhere -> economic gap
        night = night_window(usage)
        off_ok = set(night) if P["cwu_night_off"] else set()
        if away: off_ok = set(range(24))
        thr = max(1.0, 0.15 * max(usage)) if usage and max(usage) > 0 else 1.0   # same threshold as usage_clusters
        def draws(h): return (usage[h] if usage and h < len(usage) else 0.0) >= thr
        moved = sorted({i // 4 for i in range(96) if q[i] == OCHRONA and (i // 4) not in off_ok})
        for i in range(96):
            if q[i] == OCHRONA and (i // 4) not in off_ok: q[i] = None
        if moved:
            notes.append(f"CWU: poziom Ochrona (brak grzania) usunięty z godzin {moved[0]:02d}-{moved[-1]+1:02d} -> ekonomicznie (zasobnik podtrzymywany)")
        # (b) a Komfort charge in the last cheaper hour before the day's price peak (if still ahead)
        if pk and not away:
            a, b = pk
            pre = [h for h in range(max(0, a - 3), a) if (price(h) is not None) and price(h) < 0.9 * price(a)]
            future_ok = now_hour is None or a - 1 > now_hour
            if pre and future_ok and not any(q[i] in (KOMFORT, KOMFORT_PLUS) for i in range(pre[0] * 4, a * 4)):
                best = min(pre, key=lambda h: (price(h), -h))
                for i in range(best * 4, best * 4 + 4): q[i] = KOMFORT
                notes.append(f"CWU: dodano ładowanie o {best:02d}:00 przed szczytem cen {a:02d}-{b:02d}")
        # (b2) coverage: every observed draw cluster needs a Komfort charge in the 4 h before it (or in its
        #      first hour); planners (LLM amendments especially) tend to strip charges to "save" money
        if usage and not away:
            for hs, he in usage_clusters(usage):
                win = [h for h in range(max(0, hs - 4), hs + 1)]
                if any(q[i] in (KOMFORT, KOMFORT_PLUS) for h in win for i in range(h * 4, h * 4 + 4)): continue
                if now_hour is not None and hs < now_hour: continue          # already in the past today
                cands = [h for h in win if h not in peak and h != hs] or [h for h in win if h != hs] or win
                if now_hour is not None and hs > now_hour:
                    # a NEW charge for a draw still ahead today: an hour already past cannot serve it (in the
                    # rolling day it is tomorrow's hour, possibly with tomorrow's cheap price)
                    cands = [h for h in cands if h >= now_hour] or [h for h in win if h >= now_hour] or cands
                best = min(cands, key=lambda h: (est(h) + 0.03 * (hs - h), -h))
                for i in range(best * 4, best * 4 + 4): q[i] = KOMFORT
                notes.append(f"CWU: dodano ładowanie o {best:02d}:00 przed poborem {hs:02d}-{he+1:02d}")
        # (c) Komfort budget: the tank is charged for at most cwu_komfort_cap_h hours a day; keep the
        #     hours that serve the coming draws (usage in the next 3 h), the pre-peak charge, and 'now'
        komf_h = sorted({i // 4 for i in range(96) if q[i] in (KOMFORT, KOMFORT_PLUS)})
        cap = P["cwu_komfort_cap_h"]
        if len(komf_h) > cap:
            def score(h):
                s = sum((usage[k] if usage and k < len(usage) else 0.0) for k in range(h + 1, min(24, h + 4)))
                if pk and h == pk[0] - 1: s += 50
                if now_hour is not None and h == now_hour and tank_temp is not None and tank_temp < CWU_FLOOR_HEAT: s += 100
                return (s, -h)
            keep = set(sorted(komf_h, key=score, reverse=True)[:cap])
            for h in komf_h:
                if h not in keep:
                    for i in range(h * 4, h * 4 + 4):
                        if q[i] in (KOMFORT, KOMFORT_PLUS): q[i] = None
            notes.append(f"CWU: Komfort ograniczony z {len(komf_h)} h do {cap} h/dobę (zostają godziny przed poborem; reszta podtrzymanie ekonomiczne)")
        # (c2) Komfort runs are 1 h each (a 200 l tank charges in ~10 min at 20 kW); keep the hour
        #      closest to the following draws (= the last hour of the run)
        runs, i = [], 0
        while i < 96:
            if q[i] in (KOMFORT, KOMFORT_PLUS):
                j = i
                while j + 1 < 96 and q[j + 1] == q[i]: j += 1
                runs.append((i, j)); i = j + 1
            else: i += 1
        trimmed = False
        for a_, b_ in runs:
            if b_ - a_ + 1 > 4:
                keep_from = (b_ // 4) * 4                       # last full hour of the run
                for k in range(a_, keep_from):
                    q[k] = None
                trimmed = True
        if trimmed: notes.append("CWU: przedziały Komfort skrócone do 1 h (zasobnik ładuje się w ~10 min; dłuższy przedział = podtrzymywanie 45 °C)")
        # (c3) night window without heating (Oszczędność/Balans): economic gaps at night -> level 1,
        #      unless draws are observed in that hour or the following one
        if off_ok and not away:
            added = []
            for h in off_ok:
                if all(q[i] is None for i in range(h * 4, h * 4 + 4)) and not draws(h) and not draws((h + 1) % 24):
                    for i in range(h * 4, h * 4 + 4): q[i] = OCHRONA
                    added.append(h)
            if added: notes.append(f"CWU: noc {night[0]:02d}-05 bez grzania (2 h po ostatnim wieczornym poborze; zasobnik trzyma ciepło do porannego ładowania)")
        # (d) tank floor: cold tank overrides any plan (the economic floor only when draws are ahead)
        if tank_temp is not None and now_hour is not None:
            ahead = any(draws(h % 24) for h in range(now_hour, now_hour + 4))
            if tank_temp < CWU_FLOOR_ECON and (ahead or now_hour not in off_ok):
                for h in range(now_hour, min(24, now_hour + 4)):
                    for i in range(h * 4, h * 4 + 4):
                        if q[i] == OCHRONA: q[i] = None
                notes.append(f"CWU: zasobnik {tank_temp:.0f} °C < {CWU_FLOOR_ECON:.0f} -> najbliższe godziny bez Ochrony (podtrzymanie ekonomiczne)")
            if tank_temp < CWU_FLOOR_HEAT:
                for i in range(now_hour * 4, min(96, now_hour * 4 + 4)): q[i] = KOMFORT
                notes.append(f"CWU: zasobnik {tank_temp:.0f} °C < {CWU_FLOOR_HEAT:.0f} -> grzanie TERAZ ({now_hour:02d}:00) niezależnie od ceny")
        out, fit_notes = _fit_cwu(_runs(q, 15), 5, usage, pk, now_hour, tank_temp)
        return out, notes + fit_notes
    if key == "Cyrkulacja":
        for i in range(96):
            if q[i] is not None and q[i] != KOMFORT: q[i] = KOMFORT   # circulation slots are on/off; level is always 2
        exp_hours = [h for h in range(24) if exp(h)]
        if not exp_hours:
            out, fit_notes = _fit_circ(_runs(q, 15), 4, usage)
            return out, notes + fit_notes
        on = [i for i in range(96) if q[i] is not None]
        if not on: return slots, notes
        def weight(i):  # prefer quarters in hours with observed draws
            return (usage[i // 4] if usage and i // 4 < len(usage) else 0.0)
        on_exp = [i for i in on if exp(i // 4)]
        cap_exp = int(P["circ_exp_cap_h"] * 4)
        if len(on_exp) > cap_exp:
            keep = set(sorted(on_exp, key=lambda i: (-weight(i), i))[:cap_exp])
            for i in on_exp:
                if i not in keep: q[i] = None
            notes.append(f"Cyrkulacja: w drogich godzinach skrócona z {len(on_exp)/4:g} h do {cap_exp/4:g} h (krążenie chłodzi zasobnik ~3 K/h)")
        on = [i for i in range(96) if q[i] is not None]
        cap_day = int(P["circ_day_cap_h"] * 4)
        if len(on) > cap_day:
            keep = set(sorted(on, key=lambda i: (-weight(i), i))[:cap_day])
            for i in on:
                if i not in keep: q[i] = None
            notes.append(f"Cyrkulacja: łącznie skrócona do {cap_day/4:g} h/dobę")
        out, fit_notes = _fit_circ(_runs(q, 15), 4, usage)
        return out, notes + fit_notes
    return slots, notes

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

def directive_hours(d):
    """Local hours a user directive covers: od..do (do=24 or od==do -> whole day; od>do wraps midnight)."""
    od, do = int(d.get("od", 0)) % 24, int(d.get("do", 24))
    if do >= 24: do = 0
    if od == do: return list(range(24))
    return list(range(od, do)) if od < do else list(range(od, 24)) + list(range(0, do))

_WARMTH = {OCHRONA: 0, None: 1, KOMFORT_MINUS: 2, KOMFORT: 3, KOMFORT_PLUS: 4}   # CO level ranked by warmth
_BY_WARMTH = {v: k for k, v in _WARMTH.items()}

def plan(hours, usage, thermal, tank, pref="Balans", away=False, bias=None,
         tin=None, tout=None, battery=False, comfort_windows=None, directive=None, now_hour=None):
    """hours: 24 dicts {price, cheap, exp[, price_est]} indexed by LOCAL hour (rolling day, missing -> None).
    directive: a user's free-text instruction already interpreted by the LLM and validated by the app:
    {komfort -2..+2, od, do, tekst} -> CO levels in those hours are forced warmer/cooler (see below).
    now_hour: current local hour for a live plan (None: inferred from the rolling-day price gap, or none
    for a backtest day).
    Returns dict: CO/CWU/Cyrkulacja slots, power_plan[24], rationale[], model_used."""
    P = PREF.get(pref, PREF["Balans"])
    cw = comfort_windows or DEFAULT_COMFORT_WINDOWS
    bias = bias or [0.0] * 24
    notes = []
    price, est = _price_fns(hours)                                   # est: missing price -> estimate (charge placement)
    now = now_hour if now_hour is not None else _rolling_now(hours)
    def exp_flag(h): return bool((hours[h] or {}).get("exp"))       # Pstryk flag: power-plan tiers only
    pk = peak_block(hours)
    peak = set(range(pk[0], pk[1])) if pk else set()
    def exp(h): return h in peak                                     # programme levels: the day's real peak (<=5 h)
    def cheap(h): return bool((hours[h] or {}).get("cheap"))
    known = [h for h in range(24) if price(h) is not None]
    if pk: notes.append(f"Szczyt cen {pk[0]:02d}:00-{pk[1]:02d}:00 (blok wokół maksimum; flaga 'drogo' Pstryk steruje tylko limitem mocy)")

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
                # coasting (block unheated) only on a TRUSTED model; otherwise the block keeps the
                # conservative P['peak'] level set above (Komfort- in Balans)
                coast, why = None, (thermal.trust_issue() if thermal else "brak modelu termicznego")
                if why is None and (tin is None or tout is None): why = "brak temperatury wewnątrz/na zewnątrz"
                if why is None:
                    coast = thermal.coast_hours(tin + 0.8, tout, tin - P["tmin_off"])
                if coast is None:
                    if pref != "Oszczędność": co[pre] = KOMFORT_PLUS
                    notes.append(f"{pre:02d}:00 " + ("Komfort+ (rozgrzanie); " if pref != "Oszczędność" else "")
                                 + f"blok {h:02d}-{h+blk:02d} bez coastingu ({why}) -> {LEVEL_NAME[P['peak']]}")
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
        # user directive ("jest mi zimno", "oszczędzaj do wieczora"): +1 = at least Komfort, +2 = Komfort+,
        # -1 = at most Komfort-, -2 = Ochrona, in the directive's hours. Price is deliberately ignored here —
        # the user said so; the cost is reported back to them in the reply.
        k = int((directive or {}).get("komfort", 0) or 0)
        if directive and k:
            hs = directive_hours(directive)
            floor_rank = {1: 3, 2: 4}.get(k); cap_rank = {-1: 2, -2: 0}.get(k)
            for h in hs:
                r = _WARMTH[co[h]]
                if floor_rank is not None and r < floor_rank: co[h] = _BY_WARMTH[floor_rank]
                if cap_rank is not None and r > cap_rank: co[h] = _BY_WARMTH[cap_rank]
            span = f"{hs[0]:02d}:00-{(hs[-1] + 1) % 24:02d}:00" if len(hs) < 24 else "cały dzień"
            notes.append(f"Polecenie użytkownika „{str(directive.get('tekst', ''))[:60]}”: "
                         f"{'cieplej' if k > 0 else 'chłodniej'} ({k:+d}) {span} — ceny pominięte w tych godzinach.")
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
            if now is not None and hs <= now:
                # draw already under way today: its charge is for TOMORROW's occurrence, in the hours before
                # it — never 'now' (2026-10-09/10: the 06:05 re-plan charged at 06:00 again after 05:01)
                win = [h for h in win if h < now] or win
            cands = [h for h in win if not exp(h)]
            if not cands:
                cands = win; notes.append(f"CWU: przed poborem {hs:02d}-{he+1:02d} tylko drogie godziny -> najtańsza z nich")
            best = min(cands, key=lambda h: est(h) - (0.05 if cheap(h) else 0) + 0.03 * (hs - h))  # later = less standing loss
            dur_h = 1
            if tank:
                dur_h = max(1, int(math.ceil(tank.minutes_to_heat(38.0, 48.0, 20.0) / 60.0)))
            for k in range(best, min(24, best + dur_h)): cwu[k] = KOMFORT
            notes.append(f"CWU: nagrzewanie o {best:02d}:00 przed poborem {hs:02d}-{he+1:02d}"
                         + (f" ({price(best)} zł/kWh)" if price(best) is not None else f" (cena jeszcze nieznana, szacunek {est(best):.2f} zł/kWh)"))
    else:
        for h in (6, 17): cwu[h] = KOMFORT
        notes.append("CWU: brak profilu poboru -> domyślne 06:00 i 17:00")
    # expensive hours: Ochrona instead of the economic setpoint (no hourly top-ups in the peak);
    # make sure a Komfort charge sits in the last non-expensive hours before each peak block that
    # overlaps (or is followed within 2 h by) a draw cluster.
    prepeak_hour = None
    if not away:
        if pk:
            a, b = pk
            pre = [k for k in range(max(0, a - 3), a) if price(k) is not None and price(k) < 0.9 * price(a)]
            if pre and not any(cwu[k] == KOMFORT for k in pre):
                best = min(pre, key=lambda k: (price(k), -k))
                cwu[best] = KOMFORT
                notes.append(f"CWU: ładowanie o {best:02d}:00 przed szczytem cen {a:02d}-{b:02d} ({price(best)} zł/kWh); w szczycie tylko podtrzymanie ekonomiczne")
            existing = [k for k in range(max(0, a - 3), a) if cwu[k] == KOMFORT]
            if existing: prepeak_hour = max(existing)   # the charge closest to the peak may be boosted (see app)
        if P["cwu_night_off"]:
            night = night_window(usage)
            for h in night:
                if cwu[h] is None and not any(hs - 1 <= h <= he for hs, he in cl): cwu[h] = OCHRONA
            notes.append(f"CWU: noc {night[0]:02d}-05 bez grzania (2 h po ostatnim wieczornym poborze) — zasobnik trzyma ciepło do porannego ładowania")
    battery_hour = None
    if battery and known:
        hb = min(known, key=lambda h: price(h))
        cwu[hb] = KOMFORT; battery_hour = hb
        notes.append(f"Magazyn ciepła: dogrzanie zasobnika w najtańszej godzinie {hb:02d}:00 ({price(hb)} zł/kWh)")
    if away:
        cwu = [None] * 24; cwu[6] = KOMFORT
        notes.append("Nikogo w domu -> CWU tylko jedno podtrzymanie o 06:00.")
    cwu_slots, fit_notes = _fit_cwu(_runs(cwu, 60), 5, usage, pk if not away else None)
    notes += fit_notes

    # ---- circulation ----
    circ = [None] * 24
    if not away:
        if cl:
            for hs, he in cl:
                hrs = list(range(hs, min(24, he + 1)))
                n_exp = sum(1 for k in hrs if exp(k))
                cap = P["circ_per_cluster"] if n_exp == 0 else min(P["circ_per_cluster"], P["circ_exp_cap_h"])
                top = sorted(hrs, key=lambda k: (-(usage[k] if k < len(usage) else 0), k))[:cap]
                for k in top: circ[k] = KOMFORT
            while sum(1 for k in range(24) if circ[k]) > P["circ_day_cap_h"]:   # whole-day cap
                weakest = min((k for k in range(24) if circ[k]), key=lambda k: (usage[k] if k < len(usage) else 0, -k))
                circ[weakest] = None
            tot = sum(1 for k in range(24) if circ[k])
            notes.append(f"Cyrkulacja: {tot} h/dobę tylko w godzinach najsilniejszego poboru (krążenie chłodzi zasobnik ~3 K/h" + (", w drogich godzinach max %g h)" % P["circ_exp_cap_h"] if any(exp(k) for k in range(24)) else ")"))
        else:
            for k in (6, 7, 18, 19): circ[k] = KOMFORT
    circ_slots, fit_notes = _fit_circ(_runs(circ, 60), 4, usage)
    notes += fit_notes

    # ---- power plan ----
    t_exp, t_norm, t_cheap = P["tiers"]
    power = []
    for h in range(24):
        if price(h) is None: power.append(t_norm)
        elif exp_flag(h) or exp(h): power.append(t_exp)
        elif cheap(h): power.append(t_cheap)
        else: power.append(t_norm)
    return {"CO": co_slots, "CWU": cwu_slots, "Cyrkulacja": circ_slots, "power_plan": power,
            "rationale": notes, "pref": pref, "away": away, "battery_hour": battery_hour, "prepeak_hour": prepeak_hour,
            "model": {"thermal_ok": bool(thermal and thermal.ok()), "tau_h": thermal.tau_h() if thermal and thermal.ok() else None,
                      "thermal_trusted": bool(thermal and thermal.trusted()),
                      "thermal_note": (thermal.trust_issue() if thermal else "brak modelu termicznego"),
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

# ----------------------------------------------------------------- season steering
# The boiler's own outdoor cut-off (co_outside_off_temp, 12.3 C here) still blocks CO above it even in
# winter mode, so erring towards winter is cheap: winter only *allows* heating. The decision is
# therefore quick towards winter and slow towards summer, with dwell times against shoulder-season flapping.
SEASON_WINTER_FC_MARGIN = 0.5    # forecast min must fall this far below the cut-off
SEASON_SUMMER_MEAN_MARGIN = 3.0  # 24 h outdoor mean this far above the cut-off
SEASON_SUMMER_FC_MARGIN = 1.5    # and the whole next-24 h forecast above cut-off + this
SEASON_DWELL_TO_WINTER_H = 6     # after a switch to summer
SEASON_DWELL_TO_SUMMER_H = 48    # after a switch to winter

def season_decision(season, tout_now, tout_mean_24h, fc_temps, room_mean, room_target, cutoff,
                    hours_since_change=None):
    """season: 'summer' | 'winter'. fc_temps: forecast temperatures for the next ~24 h.
    Returns (target_season or None, reason, facts)."""
    fc = [x for x in (fc_temps or []) if x is not None]
    fc_min = min(fc) if fc else None
    facts = {"sezon": season, "zewn": tout_now, "zewn_srednia_24h": tout_mean_24h,
             "prognoza_min_24h": fc_min, "dom": room_mean, "cel_dom": room_target, "prog_wylaczenia_co": cutoff,
             "godz_od_zmiany": round(hours_since_change, 1) if hours_since_change is not None else None}
    if cutoff is None or season not in ("summer", "winter"):
        return None, "brak danych (próg wyłączenia CO / sezon)", facts
    if season == "summer":
        if hours_since_change is not None and hours_since_change < SEASON_DWELL_TO_WINTER_H:
            return None, f"lato — blokada zmiany jeszcze {SEASON_DWELL_TO_WINTER_H - hours_since_change:.1f} h", facts
        if fc_min is not None and fc_min < cutoff - SEASON_WINTER_FC_MARGIN:
            return "winter", (f"prognoza spada do {fc_min:.1f} °C, poniżej progu wyłączenia CO {cutoff:.1f} °C "
                              f"— kocioł może być potrzebny w nocy"), facts
        if (tout_now is not None and tout_now < cutoff and room_mean is not None and room_target is not None
                and room_mean < room_target - 1.0):
            return "winter", f"na zewnątrz {tout_now:.1f} °C i dom {room_mean:.1f} °C < cel {room_target:.1f} − 1 °C", facts
        return None, "lato — ogrzewanie niepotrzebne", facts
    # winter -> summer: only when reliably warm
    if hours_since_change is not None and hours_since_change < SEASON_DWELL_TO_SUMMER_H:
        return None, f"zima — blokada zmiany jeszcze {SEASON_DWELL_TO_SUMMER_H - hours_since_change:.1f} h", facts
    warm_mean = tout_mean_24h is not None and tout_mean_24h > cutoff + SEASON_SUMMER_MEAN_MARGIN
    warm_fc = fc_min is not None and fc_min > cutoff + SEASON_SUMMER_FC_MARGIN
    house_ok = room_mean is None or room_target is None or room_mean >= room_target - 0.5
    if warm_mean and warm_fc and house_ok:
        return "summer", (f"średnia 24 h {tout_mean_24h:.1f} °C i prognoza min {fc_min:.1f} °C wyraźnie powyżej "
                          f"progu {cutoff:.1f} °C — ogrzewanie zbędne"), facts
    return None, "zima — ogrzewanie może być potrzebne", facts

