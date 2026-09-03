# Silnik planowania (v2): LLM, silnik deterministyczny albo oba

Od v2 aplikacja AppDaemon ma **dwa planery** i przełącznik, który decyduje, kto zapisuje programy
dobowe kotła (program 8):

| `input_select.kospel_planer` | Kto planuje | Kto sprawdza |
|---|---|---|
| **LLM** (domyślnie) | model językowy (Ollama), jak w v1 | nikt — aplikacja waliduje tylko JSON |
| **Silnik** | silnik deterministyczny (`kospel_engine.py`) | nikt — czysta matematyka, GPU niepotrzebne |
| **Hybryda (silnik + weryfikacja LLM)** | silnik | LLM audytuje plan i może poprawić poszczególne harmonogramy |

Niezależnie od trybu plan silnika jest **zawsze liczony i publikowany** do
`sensor.kospel_plan_silnika` (atrybuty `CO`, `CWU`, `Cyrkulacja`, `uzasadnienie`, `plan_mocy`,
`weryfikacja_llm`), więc możesz porównać go z tym, co zrobił LLM, zanim powierzysz mu kocioł.
Zasady autonomii się nie zmieniły: nic nie trafia do „żywych” map tygodniowych, dopóki tryb AI nie jest
*Autonomiczny*, a włączyć go musi człowiek.

## Co silnik wie

- **Ceny** — godzinowe ceny zakupu z Pstryk z flagami tanio/drogo oraz kontekst bezwzględny.
- **Profil poboru ciepłej wody** — bezlicznikowy detektor poborów (spadki temperatury zasobnika, gdy
  kocioł nie grzeje) prowadzi histogram poborów per dzień tygodnia w `dhw_usage.json`. Silnik grupuje
  pobory w klastry i ładuje zasobnik w najtańszej nie-drogiej godzinie do 5 h *przed* klastrem (z małą
  karą za odległość, żeby nie grzać o 02:00 na prysznic o 07:00, jeśli 05:00 jest niemal tak samo tanie).
  Okna cyrkulacji trafiają na klastry.
- **Model termiczny budynku** — dopasowywany codziennie (03:30) z 7 dni historii:
  `dT_in/dt = a·(T_out − T_in) + b·P_CO + c`. Z niego silnik wyprowadza stałą czasową, ile godzin dom
  „przejedzie” przez drogi blok bez grzania, nie spadając bardziej niż pozwala preferencja, oraz ile trwa
  dogrzanie przed tym blokiem. Dopóki nie ma ≥48 czystych próbek, raportuje `stan: uczenie`, a plan
  wraca do stałych okien komfortu.
- **Model zasobnika** — szybkość nagrzewania (K/h na kW) i straty postojowe z historii temperatury
  zasobnika; zasila też diagnostykę *degradacji* (kamień / zużycie grzałki objawia się wolniejszym
  nagrzewaniem).
- **Preferencja** — `input_select.kospel_preferencja`: *Oszczędność* (w drogich godzinach tylko
  Ochrona, dopuszczalny spadek 1,5 °C), *Balans* (Komfort− w szczytach, 1,0 °C), *Komfort* (Komfort−
  poza oknami, Komfort w oknach, 0,5 °C). Ta sama preferencja wybiera progi limitu mocy wysyłane do ESP
  (godzina droga / normalna / tania → 12 / 20 / 24 kW lub niżej).
- **Obecność** — gdy wszystkie encje `person.*` (albo lista `persons:` w `apps.yaml`) są poza domem
  ≥30 min, plan przechodzi w eco: CO na *Ochrona*, jedno ładowanie CWU o 06:00, bez cyrkulacji.
  Opcjonalna encja `calendar:` — zdarzenia *urlop*/*wakacje* wymuszają to samo.
- **Sprzężenie komfortu** — dwa skrypty *Za zimno* / *Za ciepło* (wrzuć je na panel ścienny)
  przesuwają bias (±0,5 °C za naciśnięcie, zanik ×0,9 dziennie), który przesuwa poziomy silnika; prompt
  LLM też dostaje je jako kontekst.

## Wyniki

Każdy program ma najwyżej **5 przedziałów** (limit harmonogramu Kospel); przerwy oznaczają poziom
ekonomiczny. Poziomy są własne kotła: Ochrona (1), Komfort (2), Komfort− (3), Komfort+ (4). Przykład
(Balans, wtorek, drogi blok 17–22):

```
CO:   05:00-06:00 Komfort+ · 15:00-16:00 Komfort+ · 16:00-21:00 Komfort- · 21:00-22:00 Komfort
CWU:  02:00-03:00 Komfort · 12:00-13:00 Komfort · 15:00-16:00 Komfort
Cyrk: 06:00-08:00 · 17:00-21:00
```

`uzasadnienie` wypisuje, dlaczego podjęto każdą decyzję (który klaster poboru, który blok cenowy, czy dom
ma „przejechać” na bezwładności). Plany są walidowane (`validate_slots`) przed jakimkolwiek zapisem:
przedziały muszą być uporządkowane, nienachodzące, ≤5 na program i z poprawnymi poziomami.

## Twarde reguły (stosowane do każdego planera)

Ukształtowały je dwie lekcje. 2026-09-01 pięciogodzinne okno cyrkulacji w szczycie cen 17–22 (pompa
chłodzi zasobnik ok. 3 K/h) sprawiło, że kocioł dogrzewał zasobnik co godzinę po 1,5–1,7 zł/kWh.
Pierwsza poprawka — „Ochrona w każdej drogiej godzinie" — była gorsza: 2026-09-02 Pstryk oznaczył jako
drogie 06:00–19:00, a w harmonogramie CWU **poziom 1 oznacza, że kocioł w ogóle nie grzeje
zasobnika**, więc zasobnik wystygł do 20 °C. Od tego czasu `kospel_engine.enforce_rules()` przechodzi
po wyniku aktywnego planera (LLM, silnik albo poprawka LLM) przed jakimkolwiek zapisem, a te same
reguły trafiają do promptu LLM razem z dzisiejszym szczytem cen (`rules_hint`):

| Reguła | Oszczędność | Balans | Komfort |
|---|---|---|---|
| Poziom 1 CWU (brak grzania) | wymuszony 22:00–05:00 (bez poboru) | wymuszony 22:00–05:00 (bez poboru) | nigdy (tylko tryb „nikogo w domu") |
| CWU w godzinach drogich | podtrzymanie ekonomiczne (przerwa) | tak samo | tak samo |
| Ładowanie zasobnika przed szczytem cen | ostatnia tańsza godzina przed nim | tak samo | tak samo |
| Przedziały Komfort | po 1 h, 3 h/dobę | po 1 h, 4 h/dobę | po 1 h, 6 h/dobę |
| Cyrkulacja na klaster poboru | 1 h | 2 h | 3 h |
| Cyrkulacja w godzinach drogich (łącznie) | 1 h | 1 h | 2 h |
| Cyrkulacja na dobę (łącznie) | 3 h | 4 h | 5 h |

**Próg zasobnika** nadpisuje każdy plan: zasobnik poniżej 35 °C przy spodziewanym poborze w najbliższych
czterech godzinach usuwa z nich poziom 1, poniżej 30 °C wymusza przedział Komfort w bieżącej godzinie
niezależnie od ceny. Lekcja 2026-09-03: podtrzymanie ekonomiczne 39 °C przez całą noc to impuls 20 kW co
~4 h, a 2-godzinny przedział Komfort dodawał kolejny — stąd okno nocne i przedziały 1 h (zasobnik 200 l
ładuje się w ~10 min). Osobny samonaprawczy monitor
(`cwu_floor_tick`, co 20 s, najwyżej jeden zapis na 45 min) nakłada reguły na program faktycznie
zapisany w kotle i przepisuje tylko program CWU, jeśli coś się zmienia, poza dziennym budżetem zapisów;
zimny zasobnik dodatkowo wysyła powiadomienie. Budżet Komfort CWU to 3 / 4 / 6 h na dobę
(Oszczędność / Balans / Komfort), zostają godziny obsługujące nadchodzący pobór.
„Szczyt cen" to ciągły blok wokół maksimum dnia (≥85 % maksimum, najwyżej 5 h), a nie flaga Pstryk
`is_expensive`, która może objąć większość dnia; flaga steruje już tylko limitem mocy w ESP, a wszystkie
poziomy programów (CO, CWU, cyrkulacja) używają bloku szczytu. Harmonogramy CWU i cyrkulacji znają tylko poziomy 1 i 2: schemat LLM oferuje wyłącznie te, a strażnik
zamienia zabłąkany Komfort+ na Komfort, a Komfort− na przerwę ekonomiczną. Przy skracaniu cyrkulacji
zostają godziny o najsilniejszym zaobserwowanym poborze. Korekty są logowane i publikowane w atrybucie `korekty_regul`
sensora harmonogramu, a `zrodlo` dostaje dopisek „+ reguły".

## Weryfikacja hybrydowa

W trybie *Hybryda* LLM dostaje plan silnika plus ceny, prognozę, klastry poboru i stan modeli, i musi
odpowiedzieć ścisłym schematem JSON: `zatwierdzam` (bool), `uwagi` (lista uwag), `poprawki`
(opcjonalne poprawione listy przedziałów per harmonogram). Poprawki są ponownie walidowane; jeśli JSON
jest niepoprawny, plan silnika idzie bez zmian, a uwaga trafia do logu. Sensory harmonogramów mają
atrybut `zrodlo`, więc widać, kto jest autorem aktywnego programu (`Silnik`, `LLM`,
`Hybryda: silnik (LLM zatwierdził)`, `Hybryda: poprawka LLM`).

## Oszczędności, diagnostyka, backtest

- `sensor.kospel_oszczednosci` (codziennie 00:15) — wczorajsze kWh i koszt z pakietu energii vs dwa
  scenariusze porównawcze: te same kWh po średniej cenie dnia i po taryfie płaskiej
  (`input_number.kospel_taryfa_plaska`). Sumy kroczące z 7 dni są w atrybutach.
- **Podsumowanie tygodnia** — w poniedziałek o 08:00 trwałe powiadomienie `kospel_tydzien` z kosztem
  tygodnia, oszczędnością, poborami CWU i stanem modeli.
- `sensor.kospel_diagnostyka` (codziennie 04:00) — trend ciśnienia w instalacji (< −0,03 bar/dzień
  albo < 0,8 bar sygnalizuje wyciek / problem z naczyniem wzbiorczym), degradacja szybkości nagrzewania
  zasobnika (> 20 % wolniej niż baza sygnalizuje kamień) i sensowność dopasowania modeli.
- **Backtest** — przełącznik `input_boolean.kospel_backtest_run`: odtwarza wczorajsze rzeczywiste kWh
  na planie silnika dla tego dnia i publikuje `sensor.kospel_backtest` (koszt rzeczywisty vs koszt, gdyby
  trzymać się rozmieszczenia mocy/CWU wg silnika). Pomaga zdecydować, czy przejść z LLM na
  Silnik/Hybrydę.

## Magazyn ciepła (tylko z zaworem mieszającym)

`input_boolean.kospel_zawor_mieszajacy` + `input_number.kospel_cwu_magazyn_temp` (45–65 °C):
w najtańszej godzinie dnia aplikacja podnosi nastawę komfortu CWU do temperatury magazynu i potem ją
przywraca (także przy wyjściu z autonomii). **Włącz tylko, jeśli jest zamontowany termostatyczny zawór
mieszający** — 60 °C+ w kranie grozi poparzeniem.

## Pliki i stan

- `appdaemon/kospel_engine.py` — silnik (czysty Python, bez importów HA; testowalny offline).
- `engine.json` w katalogu apps — dopasowane modele, bias, ostatni plan.
- Używane helpery: patrz `homeassistant/packages/kospel_helpers.yaml` (`kospel_planer`,
  `kospel_preferencja`, `kospel_zawor_mieszajacy`, `kospel_cwu_magazyn_temp`, `kospel_taryfa_plaska`,
  `kospel_backtest_run`).
