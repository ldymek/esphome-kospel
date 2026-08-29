"""Self-healing automations for the two Z-Wave controllers (agents on the Pis)."""
import json, urllib.request
HA = "http://192.168.1.205:8123"   # EDIT ME: your HA URL
TOK = open(".ha-token").read().strip()   # long-lived HA token, chmod 600, never commit

def put(aid, body):
    req = urllib.request.Request(HA + "/api/config/automation/config/" + aid,
        data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + TOK, "Content-Type": "application/json"})
    print(aid, "->", json.load(urllib.request.urlopen(req, timeout=15)))

def down_restart(name, health, restart_cmd, notif):
    return {
        "alias": f"Z-Wave: {name} kontroler down -> restart",
        "description": "Kontener zwave-js nie działa (agent żyje) -> restart + powiadomienie z powodem.",
        "trigger": [{"platform": "state", "entity_id": health, "to": "off", "for": {"minutes": 2}}],
        "action": [
            {"service": restart_cmd, "response_variable": "r"},
            {"service": "persistent_notification.create", "data": {
                "notification_id": notif,
                "title": f"Z-Wave {name}: restart kontenera",
                "message": ("Kontroler {n} był offline -> restart {res}.\nPowód: {reason}").replace("{n}", name).replace(
                    "{res}", "{{ 'OK' if (r.content|from_json).ok else 'NIEUDANY' }}").replace(
                    "{reason}", "{{ (r.content|from_json).fail_reason | default(state_attr('" + health + "','error')) | default('n/d') }}")}},
        ],
        "mode": "single",
    }

def unreachable(name, health, notif):
    return {
        "alias": f"Z-Wave: {name} kontroler nieosiągalny",
        "description": "Agent/Pi nieosiągalny (nie da się naprawić zdalnie) -> tylko powiadomienie.",
        "trigger": [{"platform": "state", "entity_id": health, "to": "unavailable", "for": {"minutes": 5}}],
        "action": [{"service": "persistent_notification.create", "data": {
            "notification_id": notif,
            "title": f"Z-Wave {name}: BRAK ŁĄCZNOŚCI",
            "message": f"Agent na kontrolerze {name} nie odpowiada 5 min — prawdopodobnie Pi/host down. Wymaga ręcznej kontroli."}}],
        "mode": "single",
    }

# HOME: container up but HA lost Z-Wave (climate.kuchnia unavailable) => likely version incompatibility -> update
home_incompat = {
    "alias": "Z-Wave: HOME wersja zbyt stara -> auto-update",
    "description": "Kontener działa, ale HA straciło Z-Wave (encje niedostępne) -> aktualizacja obrazu.",
    "trigger": [{"platform": "state", "entity_id": "climate.kuchnia", "to": "unavailable", "for": {"minutes": 10}}],
    "condition": [{"condition": "state", "entity_id": "sensor.zwave_home_health", "state": "on"}],
    "action": [
        {"service": "persistent_notification.create", "data": {
            "notification_id": "zwave_home_update", "title": "Z-Wave HOME: auto-update",
            "message": "HA straciło Z-Wave mimo działającego kontenera — aktualizuję obraz zwave-js (HOME)."}},
        {"service": "rest_command.zwave_update_home", "response_variable": "r"},
        {"service": "persistent_notification.create", "data": {
            "notification_id": "zwave_home_update",
            "title": "Z-Wave HOME: wynik aktualizacji",
            "message": "{{ (r.content|from_json) }}"}},
    ],
    "mode": "single",
}

# Weekly proactive update of both (Sun 04:00) — prevents 'too old' before HA upgrades hit
weekly = {
    "alias": "Z-Wave: cotygodniowa aktualizacja obrazów",
    "description": "Niedziela 04:00 — rollback-safe update obu kontrolerów (agent pilnuje arm/v7 + health).",
    "trigger": [{"platform": "time", "at": "04:00:00"}],
    "condition": [{"condition": "time", "weekday": ["sun"]}],
    "action": [
        {"service": "rest_command.zwave_update_home", "response_variable": "rh"},
        {"service": "persistent_notification.create", "data": {
            "notification_id": "zwave_weekly", "title": "Z-Wave update HOME",
            "message": "{{ (rh.content|from_json) }}"}},
        {"delay": {"minutes": 3}},
        {"service": "rest_command.zwave_update_garage", "response_variable": "rg"},
        {"service": "persistent_notification.create", "data": {
            "notification_id": "zwave_weekly_g", "title": "Z-Wave update GARAGE",
            "message": "{{ (rg.content|from_json) }}"}},
    ],
    "mode": "single",
}

put("kospel_zwave_home_down", down_restart("HOME", "sensor.zwave_home_health", "rest_command.zwave_restart_home", "zwave_home_restart"))
put("kospel_zwave_garage_down", down_restart("GARAGE", "sensor.zwave_garage_health", "rest_command.zwave_restart_garage", "zwave_garage_restart"))
put("kospel_zwave_home_unreach", unreachable("HOME", "sensor.zwave_home_health", "zwave_home_unreach"))
put("kospel_zwave_garage_unreach", unreachable("GARAGE", "sensor.zwave_garage_health", "zwave_garage_unreach"))
put("kospel_zwave_home_incompat", home_incompat)
put("kospel_zwave_weekly_update", weekly)
print("done")
