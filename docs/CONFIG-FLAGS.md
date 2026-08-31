# Config flags & season — the `0x0B55` mode word

The heater keeps its season and most subsystem-enable flags in a single 16-bit **mode/config
word at `0x0B55`**. Getting these right matters — a wrong write here can disable the DHW tank,
clear building anti-frost, or strand the season. This page documents what each bit does, the
**one ordering rule that isn't obvious**, and how to recover safely.

## Bit map (`0x0B55`, verified on EKCO.MN3)

| Bit | Value | Meaning |
|----:|------:|---------|
| 0 | 1 | Turbo — DHW priority |
| 1 | 2 | Room regulator: 1 = internal, 0 = external |
| 2 | 4 | DHW thermostat: 1 = internal, 0 = external |
| **3** | **8** | **Summer (Lato)** |
| **4** | **16** | **DHW tank enabled** |
| **5** | **32** | **Winter (Zima)** |
| 8 | 256 | Pump: auto |
| 12 | 4096 | Building anti-frost |
| 13 | 8192 | Pump venting |
| 14 | 16384 | CO regulation by curve (1 = curve, 0 = fixed) |
| 15 | 32768 | Room-temp control |

Season: summer = bit3 set / bit5 clear; winter = bit5 set / bit3 clear; both clear = off.
A typical running config (summer, tank, anti-frost, curve, pump-auto, room-control) is
`0xD118 = 53528`.

## ⚠️ The ordering rule: enable the tank *before* selecting summer

**Summer mode is only valid when the DHW tank is enabled (bit4).** If the tank is off, the heater
**rejects a summer write** and re-asserts winter/off on its next control cycle — a bit-write to
`0x0B55` bit3 alone silently reverts. (The C.MI's own web UI even *hides* the summer option while
the tank is disabled.)

Correct order to restore/enter summer:

1. Enable the tank: `switch.…_zasobnik_cwu_wlaczony` → on (sets bit4). Verify it sticks.
2. Then select summer: `select.…_heater_mode` → `summer` (sets bit3, clears bit5). Now it holds.

The ESP's config-flag switches use read-modify-write on `0x0B55`, so each toggles one bit while
preserving the rest — you don't need to reconstruct the whole word.

## Which bits stick, which don't

- **Config-enable bits** (tank, anti-frost, pump-auto, room-temp control, int/ext) — writable and
  persistent via the RMW switches.
- **Season bits** (3/5) — writable **only when their precondition holds** (tank on for summer);
  otherwise the heater's control loop reverts them. This is the "ACK then overwrite" behaviour.
- Some *service* config values (e.g. room hysteresis, boiler supply temp) latch only when the
  owning subsystem's flag is written in the **same transaction** — the firmware's "gated" writes
  handle those.

## Recovering a scrambled mode word

If `0x0B55` gets into a bad state (e.g. tank/anti-frost cleared, season stuck), you don't have to
guess the original value:

1. **Read the original from history.** The `mode_word` sensor logs `0x0B55` continuously —
   pull its value from Home Assistant history *before* the bad change:
   ```
   sensor.kc868_heater_heater_mode_word   # e.g. 53528 = 0xD118
   ```
   Decode the bits (table above) to see exactly what each flag was.
2. **Restore in the right order** with the RMW switches: tank first, then any other enables
   (anti-frost, pump-auto, room-control), then the season select. Re-read `0x0B55` after each and
   confirm it converges to the original value.
3. Every write to this word takes up to ~30 s to reflect (heater poll cycle) — read back, don't
   assume.

## Why not just raw-write the whole word?

You can, but it's fragile: the season bits won't take if their precondition is unmet, and you'd
have to reconstruct all the other bits exactly. The per-bit RMW switches + the tank-before-summer
order are the reliable path, and they're what the ESP firmware exposes as normal HA entities.
