import os
import requests
from datetime import datetime, timezone
import logging

# Zentraler Logger für dieses Modul
logger = logging.getLogger("spectra_uploader.ha_calendar")


def _get_token() -> str:
    """
    HAOS Add-on token handling:
    - SUPERVISOR_TOKEN is the standard when homeassistant_api: true
    - HOMEASSISTANT_API_TOKEN optional override
    """
    token = (
        os.environ.get("HOMEASSISTANT_API_TOKEN")
        or os.environ.get("SUPERVISOR_TOKEN")
        or os.environ.get("HASSIO_TOKEN")
    )
    if not token:
        logger.error("Kein HA Token gefunden. Erwartet SUPERVISOR_TOKEN.")
        raise RuntimeError(
            "Kein HA Token gefunden. Erwartet SUPERVISOR_TOKEN. "
            "Prüfe config.yaml: homeassistant_api: true und Add-on neu bauen."
        )
    return token


def _ha_base_url() -> str:
    """Best practice: use supervisor proxy: http://supervisor/core"""
    base = os.environ.get("HOMEASSISTANT_API_URL") or "http://supervisor/core"
    return base.rstrip("/")


def _ha_headers() -> dict:
    return {"Authorization": f"Bearer {_get_token()}"}


def _parse_dt(dt) -> datetime:
    """Akzeptiert datetime (naiv/tz-aware) oder ISO string."""
    if isinstance(dt, datetime):
        return dt
    if isinstance(dt, str):
        s = dt.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    raise TypeError(f"Unsupported datetime type: {type(dt)}")


def _to_rfc3339_utc(dt) -> str:
    """RFC3339 UTC Format für die HA Calendar API."""
    dt = _parse_dt(dt)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    dt_utc = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt_utc.isoformat().replace("+00:00", "Z")


def _color_name_to_rgb(name: str):
    """Map config color names to Spectra6 RGB tuples."""
    if not name:
        return None
    n = str(name).strip().lower()

    # Strict Spectra6 palette values
    palette = {
        "black": (0, 0, 0),
        "blue": (0, 0, 255),
        "green": (41, 204, 20),
        "red": (255, 0, 0),
        "yellow": (255, 255, 0),
        "white": (255, 255, 255),
    }
    return palette.get(n)


def _fetch_calendar_events_single(entity_id: str, start, end):
    """Lade Events für einen einzelnen Kalender über den Supervisor Proxy."""
    base = _ha_base_url()
    url = f"{base}/api/calendars/{entity_id}"

    params = {
        "start": _to_rfc3339_utc(start),
        "end": _to_rfc3339_utc(end),
    }

    logger.debug("HA Calendar GET: entity_id=%s url=%s params=%s", entity_id, url, params)

    r = requests.get(url, headers=_ha_headers(), params=params, timeout=15)
    r.raise_for_status()  # Wirft einen HTTPError bei 4xx oder 5xx
    
    return r.json()


def fetch_calendar_events(entity_id, start, end):
    """Fetch calendar events mit robuster Fehlerbehandlung."""
    # Existing behavior: single calendar
    if isinstance(entity_id, str):
        try:
            return _fetch_calendar_events_single(entity_id, start, end)
        except Exception:
            logger.exception("Fehler beim Abrufen des Einzel-Kalenders %s", entity_id)
            return []

    # New compatibility: list of calendars
    if isinstance(entity_id, list):
        combined = []
        for src in entity_id:
            if isinstance(src, dict):
                eid = (src.get("entity_id") or "").strip()
                cal_color = _color_name_to_rgb(src.get("color"))
            else:
                eid = str(src).strip()
                cal_color = None

            if not eid:
                continue

            # Robustheit: Fehler bei einem Kalender führen nicht zum Abbruch
            try:
                events = _fetch_calendar_events_single(eid, start, end) or []

                # render.py checks "_cal_color"
                if cal_color:
                    for ev in events:
                        if isinstance(ev, dict) and "_cal_color" not in ev:
                            ev["_cal_color"] = cal_color

                combined.extend(events)
            except Exception:
                logger.exception("Fehler beim Abrufen des Kalenders %s – überspringe.", eid)
                continue
                
        logger.info("Insgesamt %d Termine aus %d konfigurierten Kalendern geladen.", len(combined), len(entity_id))
        return combined

    logger.error("Ungültiger Typ für entity_id: %s", type(entity_id))
    raise TypeError(f"Unsupported entity_id type: {type(entity_id)}")