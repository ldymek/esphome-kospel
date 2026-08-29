# Installation guide — step by step

This walks a new user from an empty KC868-A6 to the full stack. Parts 1–4 are the core
(heater control in Home Assistant). Parts 5–7 add the AI caretaker. Part 8 is the optional
Z-Wave TRV layer.

> ⚠️ Mains voltage and heating hardware. If you are not comfortable wiring inside a boiler's
> terminal area, ask an electrician. Proceed at your own risk.

## 0. What you need

- Kospel **EKCO.MN3** (optionally with the **C.MG3** mixing/radiator module)
- Kincony **KC868-A6** + 12 V DC supply + USB-C cable (first flash only)
- 2-wire cable for RS485 (twisted pair; a leftover ethernet pair is fine)
- Home Assistant OS/Supervised (for the add-on parts), Python 3.11+ on your workstation
- *(AI, optional)* any machine that runs [Ollama](https://ollama.com) — ~16 GB RAM/VRAM for a
  ~30B-class quantized model
- *(TRV, optional)* Raspberry Pi with Z-Wave stick + zwavejs2mqtt/Z-Wave JS UI in Docker,
  Fibaro FGT-001 heads

## 1. Wiring

1. Power off the heater at the breaker.
2. The C.MI module (if present) connects to the heater's RS485 header. **Disconnect it** —
   two masters on the bus corrupt each other.
   - *Alternative:* route the C.MI's A and B wires through KC868-A6 **relays 5 and 6** (NC/NO of
     your choice). The firmware's "Bus owner: C.MI / ESP" switch then hands the bus over safely
     (ESP silences itself before closing the relays).
3. Connect heater `A` → KC868-A6 `RS485 A`, heater `B` → `RS485 B`. If nothing responds later,
   swap A/B — miswired polarity is the #1 issue.
4. Power the KC868-A6 from its 12 V input.

## 2. Firmware

```bash
git clone https://github.com/ldymek/esphome-kospel.git
cd esphome-kospel/esphome
pip install esphome                       # or use the ESPHome Device Builder add-on later
cp secrets.yaml.example secrets.yaml      # fill in wifi + generate keys as commented
```

Open `gen_master.py` and review the `EDIT ME` markers:

- `manual_ip:` block — static IP/gateway for **your** network (or delete the block for DHCP)
- if you skip the Z-Wave part, nothing else to change — the UDP listener is harmless without a sender

```bash
python3 gen_master.py                     # writes kc868-a6-heater-master.yaml
esphome run kc868-a6-heater-master.yaml   # FIRST flash: pick the USB/serial port when asked
```

Later updates go over the air (`esphome run … --device <ip>`).

**Verify:** `esphome logs kc868-a6-heater-master.yaml` should show Modbus sensor reads
(temperatures) within ~30 s. `Modbus command … timed out` on everything = check A/B polarity
and that the C.MI is off the bus.

> Different Kospel model? The register map targets EKCO.MN3 (slave `0x65`) + C.MG3 (`0x69`).
> Related models often share the map but verify before writing. No C.MG3 at all → its entities
> will just be unavailable; you can delete the `cmg3` sections from the generator.

## 3. Add to Home Assistant

HA discovers the device (Settings → Devices & Services → *ESPHome* → Configure). When asked
for the **encryption key**, paste `api_key` from your `secrets.yaml`. You should see one device
with ~200 entities.

## 4. Home Assistant packages (helpers + energy/cost stack)

1. Enable packages once in `configuration.yaml`:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

2. Copy `homeassistant/packages/kospel_helpers.yaml` and `kospel_energia.yaml` into
   `/config/packages/` (File editor / Samba / SSH add-on). Restart HA.
3. **Energy dashboard:** Settings → Dashboards → Energy → add `sensor.kospel_energia_co` and
   `sensor.kospel_energia_cwu` as grid consumption, each with your electricity price entity.
   The PLN totals (`sensor.kospel_koszt_co_suma` / `_cwu_suma`) never reset and live in
   long-term statistics — a season's bill is the difference between two readings.

### Dashboards

For each JSON in `homeassistant/dashboards/`: open your dashboard → ✏️ edit → ⋮ →
**Raw configuration editor**, and paste the file's `views:[…]` entry into your `views:` list.
`wall_panel.json` is sized for a 1080p landscape tablet; `settings_view.json` is the
Ollama/Pstryk/guardrails/Z-Wave settings tab.

## 5. AI caretaker — Ollama

On the machine that will run the LLM:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma4:26b-a4b-it-qat        # or any capable model you can host
```

Make sure Ollama listens on the LAN (`OLLAMA_HOST=0.0.0.0`), then check from anywhere:
`curl http://<ollama-host>:11434/api/tags`.

## 6. AI caretaker — AppDaemon app

1. Install the **AppDaemon** add-on (Settings → Add-ons → store). Start it once.
2. Its config lives in `/addon_configs/a0d7b954_appdaemon/` (reachable via the Samba/SSH
   add-ons — one level *above* `/config`).
3. Copy into `…/apps/`: `appdaemon/kospel_llm.py`, plus `apps.yaml` and `secrets.yaml` made
   from the two `.example` files (Ollama host; Pstryk API key from pstryk.pl → Integracje → API
   if you have dynamic prices — without it the AI still works, just without price steering).
4. Restart the add-on. Within a minute you should see `sensor.kospel_cena_zakupu_teraz`
   (if a key is set) and, after pressing **AI — uruchom teraz**, `sensor.kospel_llm_analiza`.

### Turning the AI on — the maturity ladder

`input_select.kospel_llm_tryb` (on the Settings view):

1. **Doradca** — analysis text only; writes nothing. Run this first.
2. **Propozycje (shadow)** — also writes proposed daily programs into the heater's
   **program 8**, which is *inactive* until a weekday points at it. Read its plans for a few
   days ("Propozycja harmonogramu AI" card).
3. **Autonomiczny** — backs up your weekly program assignments, points CO/CWU/circulation at
   program 8 and keeps refreshing it with price/weather/usage-aware plans. Leaving the mode —
   or any watchdog trip (boiler alarm, room below `kospel_ai_min_pokoj`, ESP offline >5 min) —
   restores your backup automatically. The AI can never re-enable autonomy by itself.

## 7. Optional: Z-Wave TRVs (Fibaro FGT-001)

Skip freely — everything above works without this.

1. On the Z-Wave Pi (zwavejs2mqtt in Docker, WS on `:3000`):

   ```bash
   sudo apt install python3-websockets
   sudo mkdir -p /opt/zwave-agent
   sudo cp zwave-agent/zwave_agent.py /opt/zwave-agent/
   # edit the TRV = {node_id: "room"} map to your nodes first!
   sudo cp zwave-agent/zwave-agent.service.example /etc/systemd/system/zwave-agent.service
   # edit the service: ZW_AGENT_TOKEN (long random), ZW_ESP_UDP (your ESP IP:8902),
   # ZW_COMPOSE_DIR if the container is compose-managed
   sudo systemctl enable --now zwave-agent
   curl http://localhost:8901/health     # should return JSON with driver_version
   ```

   The agent now feeds the ESP by UDP every 30 s (window-open failsafe works even with HA down)
   and exposes health + token-guarded restart/rollback-safe-update endpoints.
2. In HA: add `zwave_agent_token: "<the token>"` to `/config/secrets.yaml`, copy
   `homeassistant/packages/kospel_zwave.yaml` to `/config/packages/` and replace the
   `ZWAVE_PI_*_IP` placeholders. Restart HA.
3. Self-healing automations (container down → restart+notify, HA lost Z-Wave → update, weekly
   update): create a long-lived HA token (your profile → Security), save it as `.ha-token`
   next to `zwave-agent/selfheal_automations.py`, edit the HA URL in the script, run it once
   with `python3`.

## 8. Verification checklist

- [ ] `sensor.…heater_co_inlet_temp` shows a plausible temperature and updates
- [ ] Changing a setpoint holds its value and confirms within ~30 s (no snap-back);
      `sensor.…zapisy_nieudane` stays empty
- [ ] Season switch (Lato/Zima) works from the dashboard
- [ ] Energy dashboard shows kWh + PLN after a few hours of heating
- [ ] *(AI)* Doradca mode produces an analysis; shadow mode fills program 8
      (load it in the schedule editor: harmonogram CO, program 8, **Wczytaj**)
- [ ] *(TRV)* `TRV → ESP` sensor on the Settings view shows a fresh temperature

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| All Modbus reads time out | A/B swapped, C.MI still on the bus, wrong baud (must be 9600 8N1) |
| Reads OK, writes revert | Register needs the config-word gate — use the provided controls, not raw writes; check `Zapisy nieudane` |
| Entities blip `unavailable` periodically | Don't add periodic `http_request` polls to the firmware (see Lessons in README); check WiFi roaming/power-save are off (they are in this config) |
| C.MG3 entities unavailable | No C.MG3 on the bus — ignore or strip `cmg3` from the generator |
| AI: `no Pstryk API key` warning | Set the key (Settings view field, apps.yaml `!secret`, or `.pstryk-key` file) |
| AI: analysis never appears | Check AppDaemon add-on log; verify `curl <ollama>/api/tags` from the HA host network |
