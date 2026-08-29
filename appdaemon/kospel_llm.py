"""Kospel LLM maintainer — AppDaemon app (runs INSIDE Home Assistant OS).

Port of bin/kospel_llm.py (previously a systemd daemon on the GPU box). Same behaviour:
 - refreshes Pstryk hourly prices every 15 min -> sensor.kospel_cena_zakupu_teraz
 - runs the LLM (Ollama on the GPU box) on the "Uruchom teraz" button and every interwal_h hours
 - posts status/summary/full analysis back to HA; restores API-pushed sensors after HA restarts
 - modes: Doradca (opis) / Propozycje (shadow, writes NOTHING to the heater) / Autonomiczny (future)

Deploy: /addon_configs/a0d7b954_appdaemon/apps/{kospel_llm.py,apps.yaml,.pstryk-key}
"""
import appdaemon.plugins.hass.hassapi as hass
import json, urllib.request, datetime, time, os

APPDIR = os.path.dirname(os.path.abspath(__file__))
PSTRYK_KEY_FILE = os.path.join(APPDIR, ".pstryk-key")
CACHE = os.path.join(APPDIR, "last_analysis.json")
PSTRYK_URL = ("https://api.pstryk.pl/integrations/meter-data/unified-metrics/"
              "?metrics=pricing&resolution=hour&window_start={s}&window_end={e}")

SYSTEM = (
 "Jesteś asystentem sterowania elektrycznym kotłem CO Kospel EKCO.MN3 w polskim domu. "
 "Kocioł jest sterowany przez ESP po RS485; moc maksymalna to 12/16/20/24 kW, sterowanie krzywą grzewczą. "
 "Cel: komfort cieplny przy najniższym koszcie energii (taryfa dynamiczna pstryk.pl). "
 "Bezpieczeństwo najważniejsze: nigdy nie proponuj wyłączenia ochrony przeciwmrozowej ani dezynfekcji. "
 "Dom ma termostaty Fibaro (TRV) w pokojach — dane per pokój w snapshot (temp/cel/okno/grzeje). "
 "Otwarte okna (>= progu) automatycznie wstrzymują CO (tryb Lato) i wracają po zamknięciu. "
 "Kocioł ma czujnik pokojowy tylko w sypialni — do oceny domu używaj średniej i najzimniejszego pokoju. "
 "Odpowiadaj zwięźle, po polsku, konkretnie. Nie zmyślaj danych, których nie podano.")


class KospelLLM(hass.Hass):

    def initialize(self):
        self.last_run = 0.0
        self.last_price = 0.0
        self.busy = False
        self.dhw_samples = []        # (ts, temp) ring, ~12 min, sampled every 60 s
        self.dhw_last_sample = 0.0
        self.dhw_last_publish = 0.0
        self.run_every(self.tick, "now+10", 20)
        self.log("Kospel LLM maintainer (AppDaemon) initialized")

    # ---------- low-level ----------
    def stt(self, eid, default="?"):
        v = self.get_state(eid)
        return default if v is None else str(v)

    def http_json(self, url, body=None, headers=None, timeout=30):
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=h,
                                     method="POST" if body is not None else "GET")
        return json.load(urllib.request.urlopen(req, timeout=timeout))

    def ollama_chat(self, host, model, system, user, schema=None, thinking=False, temp=0.2, npredict=700):
        body = {"model": model, "stream": False, "think": bool(thinking),
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "options": {"temperature": temp, "num_ctx": 8192, "num_predict": npredict, "repeat_penalty": 1.15}}
        if schema: body["format"] = schema
        t = time.time()
        r = self.http_json(host.rstrip("/") + "/api/chat", body, timeout=240)
        return r["message"]["content"], time.time() - t, r.get("eval_count", 0)

    # ---------- supervisor REST (forecast + history; no long-lived token needed) ----------
    def sup_json(self, path, body=None, timeout=15):
        tok = os.environ.get("SUPERVISOR_TOKEN", "")
        return self.http_json("http://supervisor/core/api" + path, body,
                              headers={"Authorization": "Bearer " + tok}, timeout=timeout)

    def fetch_forecast(self, weather_eid, hours=18):
        """Hourly forecast via weather.get_forecasts (compact lines for the prompt)."""
        if not weather_eid: return None
        try:
            r = self.sup_json("/services/weather/get_forecasts?return_response",
                              {"entity_id": weather_eid, "type": "hourly"})
            fc = r.get("service_response", {}).get(weather_eid, {}).get("forecast", [])
            out = []
            for f in fc[:hours]:
                t = str(f.get("datetime", ""))[11:16]
                out.append(f"{t}: {f.get('temperature','?')}°C {f.get('condition','')}"
                           + (f" opady {f.get('precipitation')}mm" if f.get("precipitation") else ""))
            return out or None
        except Exception as ex:
            self.log(f"forecast fetch err: {type(ex).__name__} {str(ex)[:100]}", level="WARNING")
            return None

    def fetch_history(self, hours=6):
        """Trend of key sensors over the last N hours: 'first->last (min..max)'."""
        eids = {"CWU_C": "sensor.kc868_heater_heater_dhw_temp",
                "pokoj_C": "sensor.kc868_heater_heater_room_temp",
                "zewnatrz_C": "sensor.kc868_heater_heater_outside_temp",
                "CO_zasilanie_C": "sensor.kc868_heater_heater_co_inlet_temp",
                "moc_kW": "sensor.kc868_heater_heater_power_now",
                "energia_kWh": "sensor.kc868_heater_kospel_energia"}
        start = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(hours=hours)).isoformat()
        try:
            r = self.sup_json(f"/history/period/{start}?filter_entity_id="
                              + ",".join(eids.values()) + "&minimal_response&no_attributes", timeout=25)
        except Exception as ex:
            self.log(f"history fetch err: {type(ex).__name__} {str(ex)[:100]}", level="WARNING")
            return None
        by_eid = {}
        for series in (r or []):
            if not series: continue
            eid = series[0].get("entity_id")
            vals = []
            for s in series:
                try: vals.append(float(s.get("state")))
                except (TypeError, ValueError): pass
            if eid and vals: by_eid[eid] = vals
        trend = {}
        for name, eid in eids.items():
            v = by_eid.get(eid)
            if v:
                if name == "energia_kWh":
                    trend[f"zuzycie_{hours}h_kWh"] = round(v[-1] - v[0], 2)
                else:
                    trend[name] = f"{v[0]:.1f}->{v[-1]:.1f} (min {min(v):.1f}, max {max(v):.1f})"
        return trend or None

    # ---------- prices ----------
    def pstryk_key(self):
        """Key resolution order: input_text helper (runtime override) > apps.yaml arg
        `pstryk_api_key` (canonical AppDaemon config, supports !secret) > .pstryk-key file."""
        k = self.get_state("input_text.kospel_pstryk_api_key")
        if k and str(k).strip() and str(k) not in ("unknown", "unavailable"):
            return str(k).strip()
        k = self.args.get("pstryk_api_key")
        if k and str(k).strip():
            return str(k).strip()
        if os.path.exists(PSTRYK_KEY_FILE):
            return open(PSTRYK_KEY_FILE).read().strip()
        return None

    def fetch_prices(self):
        key = self.pstryk_key()
        if not key:
            self.log("no Pstryk API key (input_text.kospel_pstryk_api_key or .pstryk-key)", level="WARNING")
            return None
        now = datetime.datetime.now(datetime.timezone.utc)
        s = now.strftime("%Y-%m-%dT00:00:00Z")
        e = (now + datetime.timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z")
        try:
            d = self.http_json(PSTRYK_URL.format(s=s, e=e), headers={"Authorization": key}, timeout=20)
        except Exception as ex:
            self.log(f"pstryk fetch error: {type(ex).__name__} {str(ex)[:120]}", level="WARNING"); return None
        curve, now_price = [], None
        now_cheap = now_exp = False
        for f in d.get("frames", []):
            p = f.get("metrics", {}).get("pricing", {})
            full = p.get("full_price")
            if full is None: continue
            try:
                fs = datetime.datetime.fromisoformat(f["start"].replace("Z", "+00:00"))
                fe = datetime.datetime.fromisoformat(f["end"].replace("Z", "+00:00"))
            except Exception: continue
            loc = fs.astimezone()
            row = {"t_local": loc.strftime("%a %H:%M"), "iso": f["start"], "full": round(full, 3),
                   "cheap": bool(p.get("is_cheap")), "exp": bool(p.get("is_expensive"))}
            curve.append(row)
            if fs <= now < fe:
                now_price = round(full, 3)
                now_cheap, now_exp = row["cheap"], row["exp"]
        if not curve: return None
        fut = [r for r in curve if datetime.datetime.fromisoformat(r["iso"].replace("Z", "+00:00"))
               >= now - datetime.timedelta(hours=1)][:24]
        cheapest = sorted(fut, key=lambda r: r["full"])[:3]
        priciest = sorted(fut, key=lambda r: -r["full"])[:3]
        return {"now": now_price, "curve": fut, "all": curve,
                "cheap_now": now_cheap, "exp_now": now_exp,
                "cheapest": [r["t_local"] for r in cheapest],
                "priciest": [r["t_local"] for r in priciest]}

    def publish_price_sensor(self, prices):
        if not prices or prices.get("now") is None: return
        attrs = {"unit_of_measurement": "PLN/kWh", "device_class": "monetary",
                 "friendly_name": "Kospel cena zakupu (teraz)", "icon": "mdi:cash-clock",
                 "najtansze_godziny": prices["cheapest"], "najdrozsze_godziny": prices["priciest"],
                 "tanio_teraz": prices.get("cheap_now", False), "drogo_teraz": prices.get("exp_now", False),
                 "krzywa_24h": [f"{r['t_local']} {r['full']}" + (" c" if r["cheap"] else "") + (" e" if r["exp"] else "")
                                for r in prices["curve"]]}
        self.set_state("sensor.kospel_cena_zakupu_teraz", state=prices["now"], attributes=attrs)
        self.publish_appliance_advice(prices)

    def publish_appliance_advice(self, prices):
        """Deterministic wall-panel advice: run the dishwasher/washer NOW or wait for the best
        upcoming 2 h price window. Refreshed with every price fetch (15 min), no LLM involved."""
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        fut = [r for r in prices["curve"]
               if _dt.datetime.fromisoformat(r["iso"].replace("Z", "+00:00")) + _dt.timedelta(hours=1) > now]
        if len(fut) < 3 or prices.get("now") is None: return
        best_i = min(range(len(fut) - 1), key=lambda i: fut[i]["full"] + fut[i + 1]["full"])
        best_avg = round((fut[best_i]["full"] + fut[best_i + 1]["full"]) / 2, 3)
        best_start = _dt.datetime.fromisoformat(fut[best_i]["iso"].replace("Z", "+00:00")).astimezone()
        worst = max(fut[:12], key=lambda r: r["full"])
        cur = float(prices["now"])
        save_pct = round((cur - best_avg) / cur * 100) if cur > 0 else 0
        today = _dt.datetime.now().astimezone().date()
        def kiedy(dt_loc):
            d = dt_loc.date()
            pre = "dziś " if d == today else ("jutro " if d == today + _dt.timedelta(days=1) else "")
            return pre + dt_loc.strftime("%H:%M")
        t = kiedy(best_start)
        worst_loc = _dt.datetime.fromisoformat(worst["iso"].replace("Z", "+00:00")).astimezone()
        unikaj = f"{kiedy(worst_loc)} ({worst['full']:.2f} zł/kWh)"
        if best_i == 0 or save_pct <= 8:
            state = "Prąd tani — pierz i zmywaj teraz"
            rada = f"Prąd jest teraz tani ({cur:.2f} zł/kWh) — śmiało włączaj zmywarkę i pralkę."
        elif prices.get("exp_now"):
            state = f"Prąd drogi — poczekaj do {t}"
            rada = (f"Prąd jest teraz DROGI ({cur:.2f} zł/kWh). Zmywarkę i pralkę nastaw "
                    f"na {t} — o {save_pct}% taniej.")
        else:
            state = f"Najtańszy prąd {t}"
            rada = (f"Prąd kosztuje teraz {cur:.2f} zł/kWh. Zmywarkę i pralkę najlepiej "
                    f"nastawić na {t} — o {save_pct}% taniej.")
        self.set_state("sensor.kospel_rada_urzadzenia", state=state[:255],
                       attributes={"friendly_name": "Prąd — kiedy włączać AGD", "icon": "mdi:dishwasher",
                                   "rada": rada, "najlepsze_okno_2h": t,
                                   "srednia_okna": best_avg, "oszczednosc_pct": max(save_pct, 0),
                                   "unikaj": unikaj})

    def publish_analysis(self, status, text, mode, model):
        self.set_state("sensor.kospel_llm_analiza", state=status[:255],
                       attributes={"analiza": text, "tryb": mode, "model": model,
                                   "friendly_name": "Kospel asystent — analiza", "icon": "mdi:robot-outline"})

    # ---------- snapshot + LLM ----------
    def gather(self, prices):
        p = "sensor.kc868_heater_"
        price_ok = bool(prices and prices.get("now") is not None)
        weather = None
        try:
            wd = self.get_state("weather")
            if wd: weather = sorted(wd.keys())[0]
        except Exception: pass
        snap = {
            "tryb_sezonu": self.stt("select.kc868_heater_heater_mode"),
            "tryb_pracy_opis": self.stt(p + "heater_operating_mode"),
            "co_zasilanie_C": self.stt(p + "heater_co_inlet_temp"),
            "co_powrot_C": self.stt(p + "heater_co_outlet_temp"),
            "cwu_C": self.stt(p + "heater_dhw_temp"),
            "pokoj_C": self.stt(p + "heater_room_temp"),
            "zewnatrz_C": self.stt(p + "heater_outside_temp"),
            "moc_teraz_kW": self.stt(p + "heater_power_now"),
            "moc_maks": self.stt("select.kc868_heater_moc_maksymalna"),
            "krzywa_kotla": self.stt(p + "heater_co_heating_curve_number"),
            "krzywa_cmg3": self.stt(p + "c_mg3_heating_curve_number"),
            "alarm": self.stt("binary_sensor.kc868_heater_heater_alarm"),
            "blad": self.stt(p + "heater_error"),
            "grzanie_CO": self.stt("binary_sensor.kc868_heater_heater_co_demand"),
            "grzanie_CWU": self.stt("binary_sensor.kc868_heater_heater_dhw_demand"),
            "koszt_CO_dzis_PLN": self.stt("sensor.kospel_koszt_co_dzisiaj"),
            "koszt_CWU_dzis_PLN": self.stt("sensor.kospel_koszt_cwu_dzisiaj"),
            "cena_zakupu_teraz_PLN_kWh": prices["now"] if price_ok else "BRAK DANYCH",
            "ceny_najtansze_godziny": prices["cheapest"] if price_ok else None,
            "ceny_najdrozsze_godziny": prices["priciest"] if price_ok else None,
            "pogoda": self.stt(weather) if weather else "?",
            "prognoza_temp": self.get_state(weather, attribute="temperature") if weather else None,
        }
        # per-room Fibaro TRVs (temp/cel/okno/heat-demand/battery) + house aggregates
        rooms = {}
        for r in ["kuchnia", "sypialnia", "velux", "balkon", "homeoffice", "wykusz"]:
            rooms[r] = {
                "temp": self.get_state(f"climate.{r}", attribute="current_temperature"),
                "cel": self.get_state(f"climate.{r}", attribute="temperature"),
                "tryb": self.stt(f"climate.{r}"),
                "okno_otwarte": self.stt(f"sensor.{r}_open_window_detected") == "True",
                "grzeje": self.stt(f"sensor.{r}_provide_heat") == "True",
                "bateria_pct": self.stt(f"sensor.{r}_battery_level"),
            }
        snap["pokoje_TRV"] = rooms
        snap["dom_srednia_C"] = self.stt("sensor.dom_temperatura_srednia")
        snap["dom_najzimniejszy_C"] = self.stt("sensor.dom_temperatura_min")
        snap["otwarte_okna"] = self.stt("sensor.dom_otwarte_okna")
        snap["wietrzenie_auto_wstrzymalo_CO"] = self.stt("input_boolean.kospel_wietrzenie_auto")
        return snap, price_ok

    def run_advisor(self, cfg, snap):
        user = ("Stan kotła teraz:\n" + json.dumps(snap, ensure_ascii=False, indent=1) +
                "\n\nWyjaśnij w 3-5 zdaniach po polsku: czy kocioł pracuje sensownie, co zwraca uwagę, "
                "i jedną konkretną rekomendację. Jeśli brak cen prądu, powiedz to i nie planuj kosztowo.")
        return self.ollama_chat(cfg["host"], cfg["model"], SYSTEM, user,
                                thinking=cfg["thinking"], temp=cfg["temp"], npredict=600)

    def run_planner(self, cfg, snap, price_ok, prices):
        if not price_ok:
            text, dt, n = self.run_advisor(cfg, snap)
            return ("⏸ Brak cen prądu — opis stanu zamiast propozycji.\n\n" + text, dt, n)
        curve = "\n".join(
            f"{r['t_local']}: {r['full']} zł/kWh" + (" [tanio]" if r["cheap"] else "") + (" [drogo]" if r["exp"] else "")
            for r in prices["curve"])
        user = ("Stan kotła:\n" + json.dumps(snap, ensure_ascii=False) +
                "\n\nCeny zakupu energii (najbliższe godziny):\n" + curve +
                "\n\nZaproponuj plan sterowania: w których godzinach grzać mocniej (wyższa moc / wcześniejsze "
                "nagrzanie CO i CWU, magazynowanie ciepła w TANICH godzinach), a kiedy ograniczyć moc (DROGIE "
                "godziny), utrzymując komfort ~21-22°C. Podaj 3-5 konkretnych kroków z godzinami i szacowany "
                "kierunek kosztu. Uwzględnij prognozę pogody i trend z ostatnich 6 h, jeśli podane. "
                "Na końcu dodaj sekcję 'Sugerowane nastawy' (temperatury/krzywa — TYLKO tekstowo, "
                "użytkownik zastosuje je ręcznie). Niczego poza tym nie zapisujesz do kotła.")
        text, dt, n = self.ollama_chat(cfg["host"], cfg["model"], SYSTEM, user,
                                       thinking=cfg["thinking"], temp=cfg["temp"], npredict=800)
        return text.strip(), dt, n

    # ================= GUARDRAILS + SUPERVISED AUTONOMY =================
    # Autonomiczny mode: weekly CO map -> program 8 (backup kept; restored on exit/watchdog).
    # The AI NEVER enables autonomy itself — only the user flips the tryb select; the app
    # can only DOWNGRADE back to shadow. Anti-freeze/disinfection/season are never touched.
    DAYS = ["poniedzialek", "wtorek", "sroda", "czwartek", "piatek", "sobota", "niedziela"]
    AUTON_WEEKLY = ["program_co", "program_cwu", "program_cyrkulacji"]   # timetables autonomy manages (-> program 8)
    MAX_CONTENT_WRITES_PER_DAY = 12

    def auton_file(self): return os.path.join(APPDIR, "autonomy.json")

    def load_auton(self):
        try: return json.load(open(self.auton_file()))
        except Exception: return {"active": False}

    def save_auton(self, a):
        try: json.dump(a, open(self.auton_file(), "w"))
        except Exception as e: self.log(f"autonomy save err: {e}", level="ERROR")

    def publish_autonomy(self, a, note=""):
        self.set_state("sensor.kospel_ai_autonomia",
                       state="aktywna" if a.get("active") else "nieaktywna",
                       attributes={"friendly_name": "Autonomia AI", "icon": "mdi:shield-check",
                                   "kopia_tygodnia": a.get("backup"), "od": a.get("since"),
                                   "notatka": note})
        # keep the schedule sensors' status labels truthful (they're stamped at write time and
        # went stale when autonomy toggled between schedule runs)
        live = a.get("active", False)
        status_txt = "AKTYWNY (autonomia)" if live else "NIEAKTYWNY"
        for tt in self.TIMETABLES:
            cur = self.get_state(tt["sensor"], attribute="all")
            if not cur or not (cur.get("attributes") or {}).get("przedzialy"): continue
            attrs = dict(cur["attributes"])
            key = attrs.get("zapisano_do", "")
            attrs["zapisano_do"] = key.split(" (")[0] + f" ({status_txt})" if key else status_txt
            attrs["aktywacja"] = ("steruje kotłem (Autonomiczny)" if live else
                                  f"ustaw dzień na 8 w Programy {tt['key']} lub tryb Autonomiczny")
            self.set_state(tt["sensor"], state=cur["state"], attributes=attrs)

    def notify_auton(self, msg):
        self.call_service("persistent_notification/create", notification_id="kospel_autonomia",
                          title="Autonomia AI kotła", message=msg)
        self.log("AUTONOMY: " + msg)

    def guardrails_ok(self):
        """Preconditions for ANY autonomous write. Returns a list of failures (empty = OK)."""
        fails = []
        if self.stt("binary_sensor.kc868_heater_heater_alarm") != "off": fails.append("alarm kotła")
        if self.stt("sensor.kc868_heater_heater_error") != "OK": fails.append("błąd kotła")
        if self.get_state("sensor.kospel_cena_zakupu_teraz") is None: fails.append("brak cen energii")
        try: float(self.stt("sensor.kc868_heater_heater_room_temp"))
        except (TypeError, ValueError): fails.append("czujnik pokojowy niedostępny")
        return fails

    def rate_limit_ok(self):
        a = self.load_auton()
        today = time.strftime("%Y-%m-%d")
        w = a.get("writes", {})
        return w.get("date") != today or w.get("count", 0) < self.MAX_CONTENT_WRITES_PER_DAY

    def count_write(self):
        a = self.load_auton()
        today = time.strftime("%Y-%m-%d")
        w = a.get("writes", {})
        a["writes"] = {"date": today, "count": (w.get("count", 0) + 1) if w.get("date") == today else 1}
        self.save_auton(a)

    def engage_autonomy(self):
        a = self.load_auton()
        if a.get("active"): return
        fails = self.guardrails_ok()
        if fails:
            self.notify_auton("Nie włączam autonomii: " + ", ".join(fails)
                              + ". Wracam do trybu Propozycje.")
            self.call_service("input_select/select_option",
                              entity_id="input_select.kospel_llm_tryb", option="Propozycje (shadow)")
            return
        backup = {}
        for wk in self.AUTON_WEEKLY:
            for d in self.DAYS:
                try:
                    v = int(float(self.stt(f"number.kc868_heater_{wk}_{d}")))
                except (TypeError, ValueError):
                    v = -1
                if not 1 <= v <= 8:
                    self.notify_auton(f"Nie włączam autonomii: nie mogę odczytać {wk} ({d}).")
                    self.call_service("input_select/select_option",
                                      entity_id="input_select.kospel_llm_tryb", option="Propozycje (shadow)")
                    return
                backup[f"{wk}_{d}"] = v
        for wk in self.AUTON_WEEKLY:
            for d in self.DAYS:
                self.call_service("number/set_value",
                                  entity_id=f"number.kc868_heater_{wk}_{d}", value=self.AI_PROG_NR)
                time.sleep(0.4)   # a 21-call burst loses tail commands (observed: circ days never armed)
        self.run_in(self.verify_engage, 75, attempt=1)
        a.update({"active": True, "backup": backup, "since": time.strftime("%Y-%m-%d %H:%M")})
        self.save_auton(a)
        self.publish_autonomy(a, "tygodnie CO + CWU + cyrkulacja wskazują program 8 (AI)")
        self.notify_auton("WŁĄCZONA — tygodnie CO, CWU i cyrkulacji przełączone na program 8 (AI). "
                          "Wyjście z trybu Autonomiczny przywraca poprzednie ustawienia.")

    def verify_engage(self, kwargs):
        """Post-engage audit: any weekly day not on program 8 gets re-written (max 3 rounds)."""
        a = self.load_auton()
        if not a.get("active"): return
        missing = []
        for wk in self.AUTON_WEEKLY:
            for d in self.DAYS:
                try: v = int(float(self.stt(f"number.kc868_heater_{wk}_{d}")))
                except (TypeError, ValueError): v = -1
                if v != self.AI_PROG_NR:
                    missing.append(f"{wk}_{d}")
        if not missing:
            self.log("AUTONOMY: verify OK — wszystkie dni na programie 8"); return
        att = kwargs.get("attempt", 1)
        if att > 3:
            self.notify_auton(f"Nie udało się przestawić na program 8 mimo 3 prób: {', '.join(missing)}")
            return
        self.log(f"AUTONOMY: verify attempt {att} — dopisuję: {missing}")
        for key in missing:
            self.call_service("number/set_value",
                              entity_id=f"number.kc868_heater_{key}", value=self.AI_PROG_NR)
            time.sleep(0.4)
        self.run_in(self.verify_engage, 75, attempt=att + 1)

    def disengage_autonomy(self, reason):
        a = self.load_auton()
        if not a.get("active"): return
        for key, v in (a.get("backup") or {}).items():
            self.call_service("number/set_value",
                              entity_id=f"number.kc868_heater_{key}", value=v)
        a["active"] = False
        self.save_auton(a)
        self.publish_autonomy(a, f"wyłączona: {reason}")
        self.notify_auton(f"WYŁĄCZONA ({reason}) — przywrócono poprzedni tydzień CO: {a.get('backup')}.")

    def autonomy_tick(self, mode):
        """Called every tick: engage/disengage on mode change + safety watchdog."""
        # HA-restart guard: an unavailable/unknown mode select is NOT a user action (this
        # disengaged autonomy as 'wyłączona przez użytkownika' during every HA restart).
        if mode not in ("Doradca (tylko opis)", "Propozycje (shadow)", "Autonomiczny"):
            return
        a = self.load_auton()
        if self.get_state("sensor.kospel_ai_autonomia") is None:
            self.publish_autonomy(a)   # ensure the status sensor always exists (startup / HA restart)
        if mode == "Autonomiczny" and not a.get("active"):
            self.engage_autonomy()
        elif mode != "Autonomiczny" and a.get("active"):
            self.disengage_autonomy("wyłączona przez użytkownika")
        elif a.get("active"):
            alarm = self.stt("binary_sensor.kc868_heater_heater_alarm")
            floor = float(self.stt("input_number.kospel_ai_min_pokoj", "19") or 19)
            try: room = float(self.stt("sensor.kc868_heater_heater_room_temp"))
            except (TypeError, ValueError): room = None
            # 'unavailable' is NOT an alarm — it's the ESP rebooting (e.g. an OTA flash). Real
            # alarm ("on") disengages immediately; unavailability only after a 5-min grace, when
            # autonomy genuinely can't supervise the heater any more.
            if alarm not in ("on", "off"):
                if not a.get("esp_down_since"):
                    a["esp_down_since"] = time.time(); self.save_auton(a)
                elif time.time() - a["esp_down_since"] > 300:
                    self.disengage_autonomy("watchdog: kocioł/ESP niedostępny > 5 min")
                    self.call_service("input_select/select_option",
                                      entity_id="input_select.kospel_llm_tryb", option="Propozycje (shadow)")
                return
            if a.get("esp_down_since"):
                a["esp_down_since"] = None; self.save_auton(a)
            # debounce: a real boiler alarm persists; a 1-2 tick 'on' right after an ESP reboot
            # (sensor init glitch) must not kill autonomy
            if alarm == "on":
                if not a.get("alarm_since"):
                    a["alarm_since"] = time.time(); self.save_auton(a)
                elif time.time() - a["alarm_since"] > 90:
                    self.disengage_autonomy("watchdog: alarm kotła")
                    self.call_service("input_select/select_option",
                                      entity_id="input_select.kospel_llm_tryb", option="Propozycje (shadow)")
            elif a.get("alarm_since"):
                a["alarm_since"] = None; self.save_auton(a)
            elif room is not None and room < floor:
                self.disengage_autonomy(f"watchdog: pokój {room:.1f}°C < próg {floor:.1f}°C")
                self.call_service("input_select/select_option",
                                  entity_id="input_select.kospel_llm_tryb", option="Propozycje (shadow)")

    # ---------- AI schedule proposal -> write to CO program 8 ----------
    AI_PROG_NR = 8
    AI_PROG_BASE = 3100 + 15 * (AI_PROG_NR - 1)   # heater CO daily-program base 0x0C1C=3100

    # daily-program bases: CO 0x0C1C=3100, CWU 0x0C9E=3230; program 8 slot = base + 15*7
    TIMETABLES = [
        {"key": "CO", "base": 3100, "sensor": "sensor.kospel_ai_harmonogram",
         "levels": "1=Ochrona(najchlodniej) 2=Komfort 3=Komfort- 4=Komfort+ (temp pokojowa); poza przedzialami=ekonomiczna",
         "goal": ("Komfort (2-3) rano ~6-9 i wieczorem ~16-22; dogrzej/magazynuj cieplo (4) w NAJTANSZYCH "
                  "godzinach; Ochrona (1) w NAJDROZSZYCH i w nocy.")},
        {"key": "CWU", "base": 3230, "sensor": "sensor.kospel_ai_harmonogram_cwu",
         "levels": "dla cieplej wody (CWU): 2=grzej zasobnik do komfortu, 1=ekonomicznie (podtrzymanie)",
         "goal": ("Nagrzej zasobnik CWU (2) w 2-3 NAJTANSZYCH godzinach doby oraz tuz przed porannym (~6:00) "
                  "i wieczornym (~18:00) uzyciem; reszta doby ekonomicznie (1).")},
        {"key": "Cyrkulacja", "base": 3360, "sensor": "sensor.kospel_ai_harmonogram_cyrk",
         "levels": ("dla pompy CYRKULACJI CWU: w przedziale pompa krazy (ciepla woda od reki w lazience), "
                    "poza przedzialami stoi (zero strat ciepla w rurach). Poziom zawsze 2."),
         "goal": ("Wybierz 2-4 KROTKIE okna krazenia tylko gdy domownicy realnie uzywaja cieplej wody: "
                  "rano ~5:45-8:30 i wieczorem ~17:30-22:30 (ew. krotko w poludnie). NIGDY w nocy ani gdy "
                  "dom pusty — kazda godzina krazenia to realne straty ciepla z rur, ktore kociol musi "
                  "doplacic. Okna zgraj z harmonogramem CWU tak, by zasobnik byl juz nagrzany.")},
    ]

    def propose_schedule(self, cfg, prices, forecast):
        """Design daily programs for CO + CWU (numbers-only JSON, hard-validated) and write each to
        its program-8 slot. Weekly assignment untouched here -> inactive unless the user/autonomy points a day at 8."""
        if not self.rate_limit_ok():
            self.log("schedule write skipped: daily rate limit reached", level="WARNING"); return None
        schema = {"type": "object", "properties": {"slots": {"type": "array", "maxItems": 5,
                  "items": {"type": "object", "properties": {
                      "start_min": {"type": "integer", "minimum": 0, "maximum": 1439},
                      "stop_min": {"type": "integer", "minimum": 1, "maximum": 1439},
                      "level": {"type": "integer", "enum": [1, 2, 3, 4]}},
                      "required": ["start_min", "stop_min", "level"]}}}, "required": ["slots"]}
        live = self.load_auton().get("active", False)
        curve = "\n".join(f"{r['t_local']}: {r['full']} zl/kWh" + (" TANIO" if r["cheap"] else "")
                          + (" DROGO" if r["exp"] else "") for r in prices["curve"])
        fc = ("\nPrognoza pogody (godzinowa):\n" + "\n".join(forecast)) if forecast else ""
        lvl = {1: "Ochrona", 2: "Komfort", 3: "Komfort-", 4: "Komfort+"}
        out = {}
        usage = self.dhw_usage_hint()
        for tt in self.TIMETABLES:
            live_note = (f"UWAGA: ten program jest AKTYWNY i steruje kotlem ({tt['key']}). Zachowaj komfort "
                         "w typowych godzinach uzytkowania, oszczedzaj tylko gdy drogo.\n") if live else ""
            user = (live_note + f"Zaprojektuj DZIENNY program {tt['key']} (max 5 przedzialow czasowych).\n"
                    "Poziomy: " + tt["levels"] + "\n" + tt["goal"] + "\n"
                    "Minuty od polnocy (0-1439), start_min < stop_min, rosnaco i bez nakladania.\n\n"
                    "Ceny energii:\n" + curve + fc
                    + (usage if tt["key"] in ("CWU", "Cyrkulacja") else ""))
            raw, dt, n = self.ollama_chat(cfg["host"], cfg["model"],
                                          "You design heating schedules. Output ONLY JSON per schema.",
                                          user, schema=schema, thinking=False, temp=0.1, npredict=400)
            try:
                slots = json.loads(raw).get("slots", [])
            except Exception:
                self.log(f"{tt['key']} schedule JSON parse failed", level="WARNING"); continue
            clean, last_stop = [], -1
            for s in sorted(slots, key=lambda x: x.get("start_min", 0)):
                try:
                    a, b, v = int(s["start_min"]), int(s["stop_min"]), int(s["level"])
                except Exception:
                    continue
                if not (0 <= a < b <= 1439 and 1 <= v <= 4 and a >= last_stop):
                    continue
                clean.append((a, b, v)); last_stop = b
                if len(clean) == 5:
                    break
            if not clean:
                self.log(f"{tt['key']} proposal had no valid slots", level="WARNING"); continue
            base = tt["base"] + 15 * (self.AI_PROG_NR - 1)
            starts = [c[0] for c in clean] + [65535] * (5 - len(clean))
            stops = [c[1] for c in clean] + [65535] * (5 - len(clean))
            idxs = [c[2] for c in clean] + [65535] * (5 - len(clean))
            self.call_service("esphome/kc868_heater_set_daily_program_heater",
                              base=base, starts=starts, stops=stops, idxs=idxs)
            self.count_write()
            human = [f"{a//60:02d}:{a%60:02d}-{b//60:02d}:{b%60:02d} {lvl[v]}" for a, b, v in clean]
            status_txt = "AKTYWNY (autonomia)" if live else "NIEAKTYWNY"
            self.set_state(tt["sensor"], state=time.strftime("%Y-%m-%d %H:%M"),
                           attributes={"friendly_name": f"Program AI {tt['key']} (8)",
                                       "icon": "mdi:calendar-star", "przedzialy": human,
                                       "zapisano_do": f"{tt['key']} program {self.AI_PROG_NR} ({status_txt})",
                                       "aktywacja": ("steruje kotłem (Autonomiczny)" if live else
                                                     f"ustaw dzień na 8 w Programy {tt['key']} lub tryb Autonomiczny")})
            self.log(f"AI schedule -> {tt['key']} program {self.AI_PROG_NR} [{status_txt}]: {human}")
            out[tt["key"]] = human
        return out or None

    def run_once(self):
        cfg = {
            "host": self.stt("input_text.kospel_llm_host",
                             self.args.get("ollama_host", "http://192.168.1.21:11434")),
            "model": self.stt("input_select.kospel_llm_model", "gemma4:26b-a4b-it-qat"),
            "mode": self.stt("input_select.kospel_llm_tryb", "Doradca (tylko opis)"),
            "thinking": self.stt("input_boolean.kospel_llm_thinking") == "on",
            "temp": float(self.stt("input_number.kospel_llm_temperatura", "0.2") or 0.2),
        }
        prices = self.fetch_prices()
        self.publish_price_sensor(prices)
        snap, price_ok = self.gather(prices)
        # enrich with real forecast + last-6h trend (read-only)
        weather = None
        try:
            wd = self.get_state("weather")
            if wd: weather = sorted(wd.keys())[0]
        except Exception: pass
        forecast = self.fetch_forecast(weather)
        if forecast: snap["prognoza_godzinowa"] = forecast[:12]
        trend = self.fetch_history(6)
        if trend: snap["trend_6h"] = trend
        try:
            if cfg["mode"].startswith("Doradca"):
                text, dt, n = self.run_advisor(cfg, snap)
            else:
                text, dt, n = self.run_planner(cfg, snap, price_ok, prices)
                if price_ok:  # schedule writes to program 8 (inactive sandbox, or LIVE in Autonomiczny)
                    progs = self.propose_schedule(cfg, prices, forecast)
                    if progs:
                        live = self.load_auton().get("active", False)
                        tag = "AKTYWNE — sterują kotłem" if live else "NIEAKTYWNE (sandbox program 8)"
                        text += f"\n\n📅 **Harmonogramy AI ({tag}):**"
                        for key, human in progs.items():
                            text += f"\n_{key}:_ " + "; ".join(human)
                        text += ("\n_Aktywne w trybie Autonomiczny; wyjście przywraca poprzednie tygodnie._"
                                 if live else "\n_Aktywacja: tryb Autonomiczny lub ustaw dzień na 8._")
            text = text.strip()
            status = f"OK · {cfg['model']} · {dt:.0f}s · {n} tok · {time.strftime('%Y-%m-%d %H:%M')}"
            summary = text.replace("\n", " ")[:250]
            self.call_service("input_text/set_value", entity_id="input_text.kospel_llm_status", value=status[:255])
            self.call_service("input_text/set_value", entity_id="input_text.kospel_llm_last_summary", value=summary)
            self.call_service("persistent_notification/create", notification_id="kospel_llm",
                              title=f"Asystent AI kotła ({cfg['mode']})", message=text)
            self.publish_analysis(status, text, cfg["mode"], cfg["model"])
            try:
                json.dump({"status": status, "text": text, "mode": cfg["mode"], "model": cfg["model"]},
                          open(CACHE, "w"))
            except Exception: pass
            self.log(status)
            return True
        except Exception as e:
            err = f"BŁĄD: {type(e).__name__}: {str(e)[:180]}"
            self.call_service("input_text/set_value", entity_id="input_text.kospel_llm_status", value=err[:255])
            self.log(err, level="ERROR")
            return False

    # ---------- scheduler ----------
    # ---------- meterless DHW usage profile (tank-temp draw detection) ----------
    # With circulation off the tank loses ~1-2 C/h standing; a hot-water draw pulls cold feed
    # into the tank and drops it >1 C within minutes. A fast drop while the heater is NOT
    # heating DHW = someone used hot water. Draw-minutes accumulate into an hour-of-day
    # profile (EWMA over days) that the LLM uses to place CWU heating + circulation windows.
    def dhw_usage_file(self): return os.path.join(APPDIR, "dhw_usage.json")

    def dhw_usage_tick(self):
        now = time.time()
        if now - self.dhw_last_sample < 60: return
        self.dhw_last_sample = now
        try: temp = float(self.stt("sensor.kc868_heater_heater_dhw_temp"))
        except (TypeError, ValueError): return
        self.dhw_samples.append((now, temp))
        self.dhw_samples = [(t, v) for t, v in self.dhw_samples if now - t <= 720]
        old = [(t, v) for t, v in self.dhw_samples if 480 <= now - t <= 720]
        if not old: return
        drop = old[0][1] - temp          # fall over the last ~8-12 min
        heating = self.stt("binary_sensor.kc868_heater_heater_dhw_demand") == "on"
        # >55C = disinfection/overheat cooldown zone, where standing losses alone are fast
        is_draw = (not heating) and drop >= 1.2 and (temp < 55.0 or drop >= 2.5)
        u = self.dhw_load()
        today = time.strftime("%Y-%m-%d")
        if u.get("date") != today:
            if u.get("date"):    # fold finished day: long-term EWMA + that weekday's EWMA
                dow = (datetime.datetime.strptime(u["date"], "%Y-%m-%d").weekday())
                u["profile"] = [round(0.7*p + 0.3*d, 1) for p, d in zip(u["profile"], u["today"])]
                u["by_dow"][dow] = [round(0.5*p + 0.5*d, 1) for p, d in zip(u["by_dow"][dow], u["today"])]
                u.setdefault("last_days", []).append(u["today"])
                u["last_days"] = u["last_days"][-7:]
            u["date"] = today; u["today"] = [0]*24
        if is_draw:
            u["today"][int(time.strftime("%H"))] += 1
        json.dump(u, open(self.dhw_usage_file(), "w"))
        if now - self.dhw_last_publish >= 300:
            self.dhw_last_publish = now
            top = sorted(range(24), key=lambda h: -(u["profile"][h] + u["today"][h]))[:5]
            top = [f"{h:02d}:00" for h in top if u["profile"][h] + u["today"][h] > 0]
            # NB: state as string — AppDaemon drops falsy state values (int 0 -> HTTP 400 "No state")
            self.set_state("sensor.kospel_cwu_profil", state=str(sum(u["today"])),
                           attributes={"friendly_name": "Profil poboru CWU (bez licznika)",
                                       "icon": "mdi:chart-bar", "unit_of_measurement": "min",
                                       "dzis_minuty_wg_godzin": u["today"],
                                       "profil_dlugoterminowy": u["profile"],
                                       "profil_wg_dnia_tygodnia": u["by_dow"],
                                       "najczestsze_godziny": top,
                                       "metoda": "spadek temp. zasobnika >1.2°C/10 min bez grzania CWU"})
            self.dhw_drift_check(u)

    def dhw_load(self):
        u = {}
        try: u = json.load(open(self.dhw_usage_file()))
        except Exception: pass
        if len(u.get("today", [])) != 24: u["today"] = [0]*24
        if len(u.get("profile", [])) != 24: u["profile"] = [0.0]*24
        if len(u.get("by_dow", [])) != 7 or any(len(r) != 24 for r in u.get("by_dow", [])):
            u["by_dow"] = [[0.0]*24 for _ in range(7)]
        u.setdefault("date", ""); u.setdefault("last_days", []); u.setdefault("drift_notified", "")
        return u

    def dhw_drift_check(self, u):
        """Rhythm change detector: mean of the last 7 days vs the long-term profile. On a clear
        divergence, force one schedule re-proposal (max once/day) + notify."""
        days = u.get("last_days", [])
        if len(days) < 3 or sum(u["profile"]) < 5: return
        recent = [round(sum(d[h] for d in days)/len(days), 2) for h in range(24)]
        dist = sum(abs(a - b) for a, b in zip(recent, u["profile"]))
        scale = max(sum(u["profile"]), 1)
        today = time.strftime("%Y-%m-%d")
        if dist / scale > 0.6 and u.get("drift_notified") != today:
            u["drift_notified"] = today
            json.dump(u, open(self.dhw_usage_file(), "w"))
            self.notify_auton("Wykryto zmianę rytmu poboru CWU (odchylenie od profilu) — "
                              "przeprogramowuję harmonogramy CWU i cyrkulacji.")
            self.call_service("input_boolean/turn_on", entity_id="input_boolean.kospel_llm_run_now")

    def dhw_usage_hint(self):
        u = self.dhw_load()
        mix = [round(p + t, 1) for p, t in zip(u["profile"], u["today"])]
        if sum(mix) < 3: return ""   # not enough observations yet
        rows = [f"{h:02d}:00 -> {m} min" for h, m in enumerate(mix) if m >= 1]
        dnames = ["poniedzialek","wtorek","sroda","czwartek","piatek","sobota","niedziela"]
        dow = datetime.datetime.now().weekday()
        dowrow = u["by_dow"][dow]
        dowtxt = ""
        if sum(dowrow) >= 2:   # weekday-specific rhythm known -> program serves TODAY
            rows_d = [f"{h:02d}:00 -> {m} min" for h, m in enumerate(dowrow) if m >= 0.5]
            dowtxt = (f"\nProfil dla DZISIEJSZEGO dnia tygodnia ({dnames[dow]}) — najwazniejszy:\n"
                      + "\n".join(rows_d))
        return ("\n\nZAOBSERWOWANY profil poboru cieplej wody (minuty poboru wg godziny, wykryte ze "
                "spadkow temperatury zasobnika — to REALNE zwyczaje domownikow, priorytet nad "
                "typowymi zalozeniami):\n" + "\n".join(rows) + dowtxt)

    def tick(self, kwargs):
        if self.busy: return
        self.busy = True
        try:
            try:
                self.dhw_usage_tick()   # isolated: a profiling bug must never block price/watchdog
            except Exception as e:
                self.log(f"dhw_usage error: {type(e).__name__} {str(e)[:120]}", level="WARNING")
            now = time.time()
            if now - self.last_price >= 900 or self.get_state("sensor.kospel_cena_zakupu_teraz") is None:
                self.publish_price_sensor(self.fetch_prices())
                self.last_price = now
            if self.get_state("sensor.kospel_llm_analiza") is None:
                try:
                    la = json.load(open(CACHE))
                    self.publish_analysis(la["status"], la["text"], la["mode"], la["model"])
                    self.log("analysis sensor restored after HA restart")
                except Exception: pass
            enabled = self.stt("input_boolean.kospel_llm_enable") == "on"
            run_now = self.stt("input_boolean.kospel_llm_run_now") == "on"
            interval_h = float(self.stt("input_number.kospel_llm_interwal_h", "6") or 6)
            self.autonomy_tick(self.stt("input_select.kospel_llm_tryb"))
            if run_now:
                self.call_service("input_boolean/turn_off", entity_id="input_boolean.kospel_llm_run_now")
                self.run_once(); self.last_run = now
            elif enabled and (now - self.last_run) >= interval_h * 3600:
                self.run_once(); self.last_run = now
        except Exception as e:
            self.log(f"tick error: {type(e).__name__} {str(e)[:150]}", level="ERROR")
        finally:
            self.busy = False
