# Przewodnik instalacji — krok po kroku

🇬🇧 [English version](INSTALL.md)

Ten przewodnik prowadzi nowego użytkownika od pustego KC868-A6 do pełnego stosu. Części 1–4 to
rdzeń (sterowanie kotłem w Home Assistant). Części 5–7 dodają AI-opiekuna. Część 8 to opcjonalna
warstwa TRV po Z-Wave.

> ⚠️ Napięcie sieciowe i urządzenie grzewcze. Jeśli nie czujesz się pewnie przy okablowaniu
> w strefie przyłączy kotła, poproś elektryka. Robisz to na własną odpowiedzialność.

## 0. Czego potrzebujesz

- Kospel **EKCO.MN3** (opcjonalnie z modułem mieszającym/grzejnikowym **C.MG3**)
- Kincony **KC868-A6** + zasilacz 12 V DC + kabel USB-C (tylko do pierwszego flasha)
- 2-żyłowy przewód do RS485 (skrętka; para z kabla ethernet w zupełności wystarczy)
- Home Assistant OS/Supervised (dla części z add-onami), Python 3.11+ na stacji roboczej
- *(AI, opcjonalnie)* dowolna maszyna z [Ollama](https://ollama.com) — ~16 GB RAM/VRAM dla
  skwantyzowanego modelu klasy ~30B
- *(TRV, opcjonalnie)* Raspberry Pi z Z-Wave stickiem + zwavejs2mqtt/Z-Wave JS UI w Dockerze,
  głowice Fibaro FGT-001

## 1. Okablowanie

1. Wyłącz kocioł bezpiecznikiem.
2. Moduł C.MI (jeśli jest) siedzi na złączu RS485 kotła. **Odłącz go** — dwa mastery na szynie
   psują sobie nawzajem ramki.
   - *Alternatywa:* poprowadź żyły A i B modułu C.MI przez **przekaźniki 5 i 6** KC868-A6
     (NC/NO wedle uznania). Firmware'owy przełącznik „Bus owner: C.MI / ESP" przekazuje wtedy
     szynę bezpiecznie (ESP najpierw milknie, potem zamykają się przekaźniki).
3. Połącz `A` kotła → `RS485 A` na KC868-A6, `B` kotła → `RS485 B`. Jeśli później nic nie
   odpowiada — zamień A/B; odwrócona polaryzacja to problem numer 1.
4. Zasil KC868-A6 z wejścia 12 V.

## 2. Firmware

```bash
git clone https://github.com/ldymek/esphome-kospel.git
cd esphome-kospel/esphome
pip install esphome                       # albo później add-on ESPHome Device Builder
cp secrets.yaml.example secrets.yaml      # uzupełnij wifi + wygeneruj klucze wg komentarzy
```

Otwórz `gen_master.py` i przejrzyj znaczniki `EDIT ME`:

- blok `manual_ip:` — statyczne IP/gateway dla **Twojej** sieci (albo usuń blok, żeby użyć DHCP)
- jeśli pomijasz część Z-Wave, nic więcej nie zmieniasz — listener UDP bez nadawcy jest nieszkodliwy

```bash
python3 gen_master.py                     # zapisuje kc868-a6-heater-master.yaml
esphome run kc868-a6-heater-master.yaml   # PIERWSZY flash: wybierz port USB/serial, gdy zapyta
```

Kolejne aktualizacje idą już OTA (`esphome run … --device <ip>`).

**Weryfikacja:** `esphome logs kc868-a6-heater-master.yaml` powinno pokazać odczyty Modbus
(temperatury) w ciągu ~30 s. `Modbus command … timed out` na wszystkim = sprawdź polaryzację A/B
i czy C.MI na pewno zszedł z szyny.

> Inny model Kospela? Mapa rejestrów jest dla EKCO.MN3 (slave `0x65`) + C.MG3 (`0x69`).
> Pokrewne modele często dzielą mapę, ale zweryfikuj odczytami, zanim zaczniesz pisać. Brak
> C.MG3 → jego encje będą po prostu unavailable; możesz wyciąć sekcje `cmg3` z generatora.

## 3. Dodanie do Home Assistant

HA wykrywa urządzenie samo (Ustawienia → Urządzenia i usługi → *ESPHome* → Konfiguruj). Gdy
zapyta o **encryption key**, wklej `api_key` ze swojego `secrets.yaml`. Powinno pojawić się
jedno urządzenie z ~200 encjami.

## 4. Pakiety Home Assistant (helpery + stos energia/koszty)

1. Włącz packages jednorazowo w `configuration.yaml`:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

2. Skopiuj `homeassistant/packages/kospel_helpers.yaml` i `kospel_energia.yaml` do
   `/config/packages/` (add-on File editor / Samba / SSH). Zrestartuj HA.
3. **Energy Dashboard:** Ustawienia → Dashboardy → Energia → dodaj `sensor.kospel_energia_co`
   i `sensor.kospel_energia_cwu` jako pobór z sieci, każdy ze swoją encją ceny prądu.
   Sumy PLN (`sensor.kospel_koszt_co_suma` / `_cwu_suma`) nigdy się nie zerują i żyją
   w statystykach długoterminowych — rachunek za sezon to różnica dwóch odczytów.

### Dashboardy

Dla każdego JSON-a z `homeassistant/dashboards/`: otwórz swój dashboard → ✏️ edytuj → ⋮ →
**Edytor konfiguracji raw** i wklej wpis `views:[…]` z pliku do swojej listy `views:`.
`wall_panel.json` jest skrojony pod tablet 1080p landscape; `settings_view.json` to zakładka
ustawień Ollama/Pstryk/guardraile/Z-Wave.

## 5. AI-opiekun — Ollama

Na maszynie, która będzie kręcić LLM-a:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma4:26b-a4b-it-qat        # albo dowolny sensowny model, który udźwigniesz
```

Upewnij się, że Ollama nasłuchuje w LAN (`OLLAMA_HOST=0.0.0.0`), potem sprawdź skądkolwiek:
`curl http://<host-ollamy>:11434/api/tags`.

## 6. AI-opiekun — aplikacja AppDaemon

1. Zainstaluj add-on **AppDaemon** (Ustawienia → Dodatki → sklep). Uruchom go raz.
2. Jego konfiguracja mieszka w `/addon_configs/a0d7b954_appdaemon/` (dostępne przez add-ony
   Samba/SSH — poziom *wyżej* niż `/config`).
3. Skopiuj do `…/apps/`: `appdaemon/kospel_llm.py` oraz `apps.yaml` i `secrets.yaml` zrobione
   z plików `.example` (host Ollamy; klucz API Pstryka z pstryk.pl → Integracje → API, jeśli
   masz taryfę dynamiczną — bez klucza AI też działa, tylko bez sterowania cenami).
4. Zrestartuj add-on. W ciągu minuty powinien pojawić się `sensor.kospel_cena_zakupu_teraz`
   (jeśli klucz ustawiony), a po naciśnięciu **AI — uruchom teraz** — `sensor.kospel_llm_analiza`.

### Włączanie AI — drabinka dojrzałości

`input_select.kospel_llm_tryb` (na widoku Ustawienia):

1. **Doradca** — tylko tekst analizy; nic nie zapisuje. Od tego zacznij.
2. **Propozycje (shadow)** — dodatkowo zapisuje proponowane programy dzienne do **programu 8**
   kotła, który jest *nieaktywny*, dopóki nie wskaże go któryś dzień tygodnia. Poczytaj jego
   plany przez kilka dni (karta „Propozycja harmonogramu AI").
3. **Autonomiczny** — robi backup przypisań tygodniowych, wskazuje CO/CWU/cyrkulację na
   program 8 i odświeża go planami świadomymi cen, pogody i Waszych zwyczajów. Wyjście z trybu —
   albo zadziałanie watchdoga (alarm kotła, pokój poniżej `kospel_ai_min_pokoj`, ESP offline
   >5 min) — przywraca backup automatycznie. AI nigdy samo nie włączy autonomii z powrotem.

## 7. Opcjonalnie: TRV Z-Wave (Fibaro FGT-001)

Pomiń bez żalu — wszystko powyżej działa bez tego.

1. Na Pi z Z-Wave JS (zwavejs2mqtt w Dockerze, WS na `:3000`):

   ```bash
   sudo apt install python3-websockets
   sudo mkdir -p /opt/zwave-agent
   sudo cp zwave-agent/zwave_agent.py /opt/zwave-agent/
   # najpierw przeedytuj mapę TRV = {node_id: "pokój"} pod swoje node'y!
   sudo cp zwave-agent/zwave-agent.service.example /etc/systemd/system/zwave-agent.service
   # przeedytuj service: ZW_AGENT_TOKEN (długi losowy), ZW_ESP_UDP (IP Twojego ESP:8902),
   # ZW_COMPOSE_DIR jeśli kontener jest zarządzany docker-compose
   sudo systemctl enable --now zwave-agent
   curl http://localhost:8901/health     # powinien zwrócić JSON z driver_version
   ```

   Agent karmi teraz ESP po UDP co 30 s (failsafe otwartych okien działa nawet przy leżącym HA)
   i wystawia health + chronione tokenem endpointy restart / rollback-safe update.
2. W HA: dodaj `zwave_agent_token: "<ten token>"` do `/config/secrets.yaml`, skopiuj
   `homeassistant/packages/kospel_zwave.yaml` do `/config/packages/` i podmień placeholdery
   `ZWAVE_PI_*_IP`. Zrestartuj HA.
3. Automatyzacje samo-naprawy (kontener down → restart+powiadomienie, HA zgubił Z-Wave →
   update, cotygodniowy update): utwórz long-lived token HA (Twój profil → Bezpieczeństwo),
   zapisz go jako `.ha-token` obok `zwave-agent/selfheal_automations.py`, przeedytuj URL HA
   w skrypcie i odpal go raz `python3`.

## 8. Checklist weryfikacyjna

- [ ] `sensor.…heater_co_inlet_temp` pokazuje sensowną temperaturę i się aktualizuje
- [ ] Zmieniona nastawa trzyma wartość i potwierdza się w ~30 s (bez odskoku);
      `sensor.…zapisy_nieudane` pozostaje pusty
- [ ] Przełącznik sezonu (Lato/Zima) działa z dashboardu
- [ ] Energy Dashboard pokazuje kWh + PLN po kilku godzinach grzania
- [ ] *(AI)* Tryb Doradca produkuje analizę; tryb shadow wypełnia program 8
      (wczytaj go w edytorze harmonogramów: harmonogram CO, program 8, **Wczytaj**)
- [ ] *(TRV)* sensor `TRV → ESP` na widoku Ustawienia pokazuje świeżą temperaturę

## Troubleshooting

| Objaw | Prawdopodobna przyczyna |
|---|---|
| Wszystkie odczyty Modbus timeoutują | zamienione A/B, C.MI wciąż na szynie, zły baud (musi być 9600 8N1) |
| Odczyty OK, zapisy się cofają | rejestr wymaga „bramki" słowem konfiguracyjnym — używaj dostarczonych kontrolek, nie surowych zapisów; sprawdź `Zapisy nieudane` |
| Encje cyklicznie mrugają `unavailable` | nie dodawaj cyklicznych polli `http_request` do firmware (patrz Lekcje w README); roaming/power-save WiFi mają być wyłączone (w tym configu są) |
| Encje C.MG3 unavailable | brak C.MG3 na szynie — zignoruj albo wytnij `cmg3` z generatora |
| AI: warning `no Pstryk API key` | ustaw klucz (pole na widoku Ustawienia, `!secret` w apps.yaml albo plik `.pstryk-key`) |
| AI: analiza się nie pojawia | sprawdź log add-onu AppDaemon; zweryfikuj `curl <ollama>/api/tags` z sieci hosta HA |
