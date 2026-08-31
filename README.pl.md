# esphome-kospel

🇬🇧 [English version](README.md)

**Pełna lokalna kontrola kotła elektrycznego Kospel EKCO.MN3 (+ moduł mieszający C.MG3) po RS485 —
z opcjonalnym „AI-opiekunem" na lokalnym LLM, który steruje ogrzewaniem według dynamicznych cen prądu.**

Projekt powstał, gdy oryginalny moduł internetowy Kospel **C.MI** zaczął umierać. Zamiast go
wymieniać, na szynę RS485 trafił ESP32 (Kincony **KC868-A6**) jako Modbus master, a cała
funkcjonalność C.MI została odtworzona w [ESPHome](https://esphome.io) — i rozszerzona:
harmonogramy sterowane cenami energii, failsafe otwartych okien per pokój, rozliczanie kosztów
z dokładnością wystarczającą do rozliczeń z lokatorami oraz nadzorowany tryb autonomiczny
z twardymi guardrailami.

> ⚠️ **Zastrzeżenie**: ten projekt steruje prawdziwym urządzeniem grzewczym po nieudokumentowanym
> (zreverse-engineerowanym) protokole. Mapa rejestrów pochodzi z lokalnej aplikacji webowej C.MI
> i podsłuchu szyny na EKCO.MN3 + C.MG3. Używasz na własną odpowiedzialność. Nigdy nie uruchamiaj
> jednocześnie ESP-mastera i C.MI na tej samej szynie (firmware ma przekazywanie szyny przez
> przekaźniki, jeśli podłączysz C.MI przez przekaźniki 5/6).

## Co dostajesz

- **Port funkcji C.MI 1:1** — temperatury, moc, ciśnienie/przepływ, tryby pracy, programy
  tygodniowe + edytor programów dziennych (wszystkie 4 harmonogramy: CO / CWU / cyrkulacja /
  C.MG3), krzywe grzewcze, dezynfekcja (anty-legionella), konfiguracja pompy, tryby specjalne
  (party/urlop/turbo), synchronizacja RTC z NTP, dekodowanie błędów — ~150 encji w Home Assistant.
- **Framework pending/confirm dla zapisów** — zmienione ustawienie *trzyma się* w UI, aż kocioł
  je potwierdzi (koniec z odskakiwaniem); nieudane zapisy pokazują się jako czerwony banner
  + powiadomienie.
- **AI-opiekun (opcjonalny)** — aplikacja AppDaemon napędzana lokalnym LLM (Ollama): analizuje
  stan, prognozę pogody, trendy z 6 h i godzinowe ceny ([pstryk.pl](https://pstryk.pl)); zapisuje
  proponowane programy dzienne do nieużywanego **programu 8** kotła (tryb shadow) albo aktywnie
  przełącza na nie tydzień (**Autonomiczny**) z watchdogiem i automatycznym rollbackiem. Uczy się
  rytmu poboru ciepłej wody z detekcji spadków temperatury zasobnika — bez wodomierza.
- **Plan mocy wg cen (opt-in)** — kocioł nie ma natywnego harmonogramu mocy, więc AI wypycha
  kroczący plan 24 h do ESP (drogie godziny 12 kW, typowe 20, tanie 24), a ESP wykonuje go
  z lokalnymi guardami nawet przy leżącym HA: próg komfortu, zasobnik poniżej 35 °C (intensywny
  pobór ciepłej wody w szczycie → pełna moc na odbudowę, potem znów limit) i dezynfekcja zawsze
  wygrywają; przeterminowany plan (>26 h) bezpiecznie wraca do pełnej mocy; Twoje własne
  ustawienie wraca przy wyjściu z autonomii.
- **Stos energetyczno-kosztowy klasy rozliczeniowej** — rozdział mocy CO/CWU → liczniki kWh →
  sumy PLN po dokładnych cenach godzinowych (gotowe pod Energy Dashboard, statystyki trzymane
  bezterminowo).
- **Dashboard na panel ścienny** (tablet 1080p landscape) + karta z poradą o AGD
  („Prąd drogi — nastaw zmywarkę na 13:00, o 60% taniej").
- **Opcjonalnie: integracja TRV Fibaro** — temperatury per pokój i detekcja otwartych okien przez
  Pi z Z-Wave JS; ESP dostaje dane bezpośrednio z Pi po UDP, więc failsafe
  *otwarte okna → wstrzymaj grzanie* działa nawet przy wyłączonym Home Assistant.

## Zrzuty ekranu

Analiza i sterowanie AI-opiekuna na głównym dashboardzie:

![Główny dashboard — analiza AI](docs/img/piec.png)

Widok panelu ściennego (tablet 1080p landscape, bez scrollowania) z doradcą cenowym AGD:

![Panel ścienny](docs/img/panel.png)

Widok ustawień — silnik Ollama, klucz API Pstryk, guardraile autonomii, zdrowie kontrolerów Z-Wave:

![Ustawienia](docs/img/settings.png)

## Sprzęt

| Element | Uwagi |
|---|---|
| Kospel EKCO.MN3 | slave `0x65`; moduł mieszający C.MG3 slave `0x69` |
| Kincony KC868-A6 (ESP32) | wbudowany RS485; 9600 8N1, Modbus RTU, func 0x03/0x10, little-endian na drucie |
| RS485 A/B | do złącza C.MI w kotle; opcjonalnie C.MI przez przekaźniki 5/6 (przekazywanie szyny) |
| *(opcjonalnie)* Raspberry Pi + Z-Wave stick | zwavejs2mqtt/Z-Wave JS UI dla głowic Fibaro FGT-001 |
| *(opcjonalnie)* dowolna maszyna z GPU | Ollama dla AI-opiekuna (model klasy ~30B działa dobrze) |

## Instalacja

**→ Nowy w projekcie? Szczegółowy przewodnik krok po kroku: [docs/INSTALL.pl.md](docs/INSTALL.pl.md)**
(okablowanie, pierwszy flash po USB, pakiety HA, import dashboardów, konfiguracja Ollama +
AppDaemon, opcjonalny Z-Wave, checklist weryfikacyjna, troubleshooting).

W skrócie:

### 1. Firmware (wymagane)

```bash
cd esphome
cp secrets.yaml.example secrets.yaml     # uzupełnij; nigdy nie commituj
# Przejrzyj znaczniki EDIT ME w gen_master.py (statyczne IP, adres feeda UDP)
python3 gen_master.py                    # generuje kc868-a6-heater-master.yaml
esphome run kc868-a6-heater-master.yaml
```

Dodaj urządzenie w Home Assistant (integracja ESPHome, zaszyfrowane API). **Fizycznie odłącz
C.MI** (albo podłącz go przez przekaźniki 5/6 i używaj przełącznika „Bus owner").

### 2. Pakiety Home Assistant (wymagane dla kosztów/AI)

Włącz packages w `configuration.yaml`, potem skopiuj z `homeassistant/packages/`:

- `kospel_helpers.yaml` — input helpery, których oczekuje AI (tryby, progi, adres Ollama)
- `kospel_energia.yaml` — rozdział mocy CO/CWU, liczniki kWh, całki kosztów PLN + liczniki
  dzienne/miesięczne. W Energy Dashboard wskaż `sensor.kospel_energia_co/cwu` ze swoją encją
  ceny godzinowej. Sumy żyją w statystykach długoterminowych → rozliczenie sezonu = koniec
  minus początek.

`homeassistant/dashboards/wall_panel.json` to gotowy widok panelu ściennego (wklej do dashboardu
przez raw editor; mieści się na tablecie 1080p landscape bez scrollowania). Odwołuje się do dwóch
zagregowanych sensorów z opcjonalnego setupu TRV (`sensor.dom_otwarte_okna`,
`sensor.dom_temperatura_min`) — usuń te dwa kafelki albo podepnij własne sensory, jeśli pomijasz
Z-Wave.

### 3. Włączenie AI-opiekuna (opcjonalne)

1. Uruchom [Ollama](https://ollama.com) gdzieś w LAN i pobierz model
   (np. `ollama pull gemma4:26b-a4b-it-qat`).
2. Zainstaluj add-on **AppDaemon**. Skopiuj `appdaemon/kospel_llm.py` i `apps.yaml.example`
   (jako `apps.yaml`) do `/addon_configs/a0d7b954_appdaemon/apps/`.
3. Skonfiguruj aplikację kanonicznie po AppDaemonowemu — argumenty w `apps.yaml` z `!secret`
   (patrz `apps.yaml.example` + `secrets.yaml.example`): `ollama_host` i `pstryk_api_key`.
   Widok **Ustawienia** na dashboardzie daje nadpisania działające od ręki (pole typu password
   `input_text.kospel_pstryk_api_key` i pole hosta Ollama) — wygodne, ale pamiętaj, że stan
   input_text może odczytać każdy zalogowany użytkownik HA; secrets.yaml to bardziej prywatne
   miejsce na klucz. Kolejność rozstrzygania: pole UI > apps.yaml > stary plik `.pstryk-key`.
4. Ustaw `input_text.kospel_llm_host` na URL Ollamy, wybierz model i przechodź
   `input_select.kospel_llm_tryb` po drabince dojrzałości:
   - **Doradca** — tylko tekst analizy, nic nie zapisuje;
   - **Propozycje (shadow)** — dodatkowo zapisuje proponowane programy dzienne do **programu 8**
     (nieaktywnego, dopóki nie wskaże go któryś dzień tygodnia) — pojeźdź tak kilka dni
     i czytaj jego plany;
   - **Autonomiczny** — robi backup przypisań tygodniowych, wskazuje CO/CWU/cyrkulację na
     program 8 i na bieżąco go odświeża. Wyjście z trybu (lub zadziałanie watchdoga) przywraca
     backup automatycznie.

   Guardraile w trybie autonomicznym: alarm kotła (z debounce), minimalna temperatura pokojowa
   (`input_number.kospel_ai_min_pokoj`), okres karencji przy niedostępnym ESP, dzienny budżet
   zapisów — a AI może się wyłącznie *zdegradować*; nigdy samo nie włącza autonomii.

### 4. TRV Fibaro przez Z-Wave (opcjonalne)

Pomiń spokojnie, jeśli nie masz głowic Z-Wave — wszystko powyżej działa bez tego.

1. Na Pi z Z-Wave JS: zainstaluj `zwave-agent/zwave_agent.py` (patrz
   `zwave-agent.service.example`; przeedytuj mapę `TRV` node-id→pokój).
   Agent serwuje `/trv.json` + `/health`, samo-naprawia kontener zwave-js
   (restart/rollback-safe update) i **pushuje** `{okna, min_temp}` do ESP po UDP co 30 s —
   celowo push-po-UDP: HTTP-pull z ESP udowodnił, że zrywa jego połączenie API z HA
   (patrz Lekcje).
2. Skopiuj `homeassistant/packages/kospel_zwave.yaml` (przeedytuj IP Pi) — sensory zdrowia +
   REST commands restart/update; opcjonalnie uruchom `zwave-agent/selfheal_automations.py`,
   który tworzy automatyzacje down→restart / stale→update / cotygodniowy update.

## Mapa Modbus (najważniejsze)

Wszystkie wartości little-endian **na drucie** (byte-swap względem konwencji Modbus). Slave'y:
kocioł `0x65`, C.MG3 `0x69` (odrzuca szerokie scalone odczyty — używaj komend
pojedynczo-rejestrowych).

| Obszar | Rejestry |
|---|---|
| Temperatury (CO zasilanie/powrót, CWU, pokój, zewnętrzna…) | okolice `0x0B3B…0x0B50` |
| Słowa statusu / trybu / błędów | `0x0B51`, `0x0B55` (bit3 = Lato, bit5 = Zima), `0x0B52` |
| Nastawy (CWU eko/komfort, pokój, CO max/ręczne, krzywe) | `0x0B62…0x0B8D` |
| Programy dzienne (5 przedziałów × start/stop/poziom) | baza CO `0x0C1C`, CWU `0x0C9E`, cyrkulacja `0x0D20`, C.MG3 `0x0B90`; program *N* = baza + 15·(N−1) |
| Przypisania tygodniowe (pn..nd) | CO `0x0C94-0x0C9A`, CWU `0x0D16-0x0D1C`, cyrk. `0x0D98-0x0D9E`, C.MG3 `0x0C08-0x0C0E` |
| RTC | `0x0AF6` (7 rej.), zapisywany co godzinę z SNTP |
| Keep-alive | zapis `{0x0000, 0x0100}` do `0x0BAE` co 10 s (heartbeat C.MI) |

Część rejestrów konfiguracyjnych zatrzaskuje się tylko, gdy słowo konfiguracyjne
(`0x0B55`/`0x0B54`) zostanie ponownie zapisane w tej samej serii — generator obsługuje te
„gated" zapisy.

## Lekcje (te drogie)

- **Cykliczne odpytywanie `http_request` w ESPHome potrafi resetować połączenie API.**
  Okresowe 0,5-sekundowe „unavailable" w HA kwantowały się *dokładnie* do okresu odpytywania
  (dowód: zmiana interwału na pierwsze 127 s). Fix: odwrócenie na UDP push. Widzisz kropkowane
  wykresy? Najpierw podejrzewaj każdą cykliczną aktywność sieciową we własnym firmware.
- `post_connect_roaming` i `power_save_mode: light` (default ESP32) powodują zrywki przy słabym
  RSSI u urządzeń stacjonarnych — wyłącz oba.
- API ESPHome przyjmuje **5 równoczesnych połączeń** — wiszące sesje `esphome logs` potrafią
  zagłodzić samego Home Assistanta.
- Sensor alarmu kotła liczony z jeszcze-nieodczytanego rejestru przez chwilę po każdym reboocie
  zwraca śmieci — zabezpiecz NaN i debounce'uj watchdogi, które na nim działają.

## Licencja

MIT — patrz [LICENSE](LICENSE).
