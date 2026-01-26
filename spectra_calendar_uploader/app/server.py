import io
import json
import os
import secrets
import string
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file
from PIL import Image

# Local imports (must work under Gunicorn package import and direct dev execution)
try:
    # Preferred when imported as package: gunicorn "app.server:app"
    from .ha_calendar import fetch_calendar_events
    from .render import render_composite
    from .dithering import dither_to_spectra6_palette
    from .spectra6_encode import rgb_to_spectra6_codes_packed_4bit
    from .esp_upload import upload_bin_multipart
except ImportError:
    # Fallback for direct execution: python app/server.py (dev only)
    from ha_calendar import fetch_calendar_events
    from render import render_composite
    from dithering import dither_to_spectra6_palette
    from spectra6_encode import rgb_to_spectra6_codes_packed_4bit
    from esp_upload import upload_bin_multipart

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"

STATIC_OUT_DIR = DATA_DIR / "out"
STATIC_OUT_DIR.mkdir(parents=True, exist_ok=True)

PREVIEW_PATH = STATIC_OUT_DIR / "preview.png"

app = Flask(
    __name__,
    template_folder=str(APP_DIR / "templates"),
    static_folder=str(APP_DIR / "static"),
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _load_addon_options() -> dict:
    if OPTIONS_PATH.exists():
        with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _ensure_secret_path(opts: dict) -> str:
    secret_path = (opts.get("secret_path") or "").strip()
    if secret_path:
        return secret_path

    alphabet = string.ascii_letters + string.digits + "-_"
    secret_path = "".join(secrets.choice(alphabet) for _ in range(43))

    opts["secret_path"] = secret_path
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(OPTIONS_PATH, "w", encoding="utf-8") as f:
        json.dump(opts, f, indent=2, ensure_ascii=False)

    print("=" * 80)
    print("IMPORTANT: Generated secret_path on first run (copy this for your NFC tag):")
    print(f"  {secret_path}")
    print("NFC URL (LAN, no HA login):")
    print("  http://<HA-IP>:8088/<secret_path>/")
    print("=" * 80)
    return secret_path


def _get_base_path() -> str:
    """
    Ingress-friendly base path.
    HA sets X-Ingress-Path when opened via Ingress.
    For direct LAN access it's empty.
    """
    p = (request.headers.get("X-Ingress-Path") or "").rstrip("/")
    return p


def _wants_html_response() -> bool:
    """
    Decide if we should render HTML (Web GUI) or return JSON (API clients).
    - Browsers typically send Accept: text/html
    - Apple Shortcuts / curl often prefer application/json or */*
    """
    accept = (request.headers.get("Accept") or "").lower()
    if "text/html" in accept:
        return True
    return False


def _get_uploaded_file_from_request():
    """
    Web form uses name="image".
    Some API clients use name="file".
    We accept both without changing templates.
    """
    if not request.files:
        return None

    if "file" in request.files:
        return request.files["file"]
    if "image" in request.files:
        return request.files["image"]

    # fallback: accept the only file if exactly one present
    try:
        return next(iter(request.files.values()))
    except Exception:
        return None


def _parse_calendar_sources(opts: dict) -> list[dict]:
    sources = opts.get("calendar_sources") or []
    out = []
    for s in sources:
        if isinstance(s, str):
            out.append({"entity_id": s})
        elif isinstance(s, dict):
            out.append(s)
    return out


def _color_name_to_rgb(name: str | None):
    if not name:
        return None
    n = str(name).strip().lower()
    palette = {
        "black": (0, 0, 0),
        "blue": (0, 0, 255),
        "green": (41, 204, 20),
        "red": (255, 0, 0),
        "yellow": (255, 255, 0),
        "white": (255, 255, 255),
    }
    return palette.get(n)


def _parse_event_start_dt(ev: dict) -> datetime | None:
    s = ev.get("start")
    if not s:
        return None
    if isinstance(s, str):
        try:
            ss = s.replace("Z", "+00:00")
            return datetime.fromisoformat(ss)
        except Exception:
            try:
                if len(s) >= 10:
                    return datetime.fromisoformat(s[:10])
            except Exception:
                return None
    return None


def _try_upload_to_targets(bin_payload: bytes, targets: list[dict]) -> tuple[bool, str]:
    last_err = ""
    for t in targets:
        base_url = (t.get("base_url") or "").rstrip("/")
        if not base_url:
            continue
        timeout_s = int(t.get("timeout_s") or 20)
        try:
            upload_bin_multipart(
                base_url=base_url,
                payload=bin_payload,
                field_name="data",
                filename="image_data.bin",
                content_type="application/octet-stream",
                timeout_s=timeout_s,
            )
            return True, f"Uploaded to {base_url}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
    return False, last_err or "No targets configured"


# ---------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------
def _process_and_upload(img: Image.Image, opts: dict):
    out_w = int(opts.get("out_width", 1200))
    out_h = int(opts.get("out_height", 1600))
    top_ratio = float(opts.get("top_ratio", 0.66))

    days_to_show = int(opts.get("days_to_show", 7))
    show_titles = bool(opts.get("show_titles", True))

    max_lines_per_day = int(opts.get("max_lines_per_day", 10))
    max_lines_per_event = int(opts.get("max_lines_per_event", 3))

    timegrid_start_hour = int(opts.get("timegrid_start_hour", 7))
    timegrid_end_hour = int(opts.get("timegrid_end_hour", 20))
    timegrid_step_minutes = int(opts.get("timegrid_step_minutes", 60))

    rotate_180 = bool(opts.get("rotate_180", False))

    cal_sources = _parse_calendar_sources(opts)
    entity_ids = [c["entity_id"] for c in cal_sources if c.get("entity_id")]

    # Fetch events
    start_dt = datetime.now()
    end_dt = start_dt + timedelta(days=days_to_show)

    events = []
    for eid in entity_ids:
        evs = fetch_calendar_events(eid, start_dt, end_dt) or []
        for e in evs:
            if isinstance(e, dict):
                e["entity_id"] = eid
        events.extend(evs)

    cal_color_map = {c["entity_id"]: _color_name_to_rgb(c.get("color")) for c in cal_sources if c.get("entity_id")}

    # Group events per day for render_composite
    events_by_day = [[] for _ in range(days_to_show)]
    start_date = start_dt.date()

    for ev in events:
        if not isinstance(ev, dict):
            continue

        eid = ev.get("entity_id")
        if eid and eid in cal_color_map and cal_color_map[eid]:
            ev["_cal_color"] = cal_color_map[eid]

        ev_start = _parse_event_start_dt(ev)
        if ev_start is None:
            continue

        day_idx = (ev_start.date() - start_date).days
        if 0 <= day_idx < days_to_show:
            events_by_day[day_idx].append(ev)

    composite = render_composite(
        photo=img,
        out_w=out_w,
        out_h=out_h,
        top_ratio=top_ratio,
        events_by_day=events_by_day,
        days_to_show=days_to_show,
        show_titles=show_titles,
        max_lines_per_day=max_lines_per_day,
        max_lines_per_event=max_lines_per_event,
        timegrid_start_hour=timegrid_start_hour,
        timegrid_end_hour=timegrid_end_hour,
        timegrid_step_minutes=timegrid_step_minutes,
        # Backwards compatibility if render.py still checks these:
        events=events,
        calendar_colors={k: v for k, v in cal_color_map.items() if v},
    )

    if rotate_180:
        composite = composite.rotate(180, expand=False)

    # IMPORTANT for disk usage:
    # We only keep ONE preview file and overwrite it each upload.
    composite.save(PREVIEW_PATH, format="PNG")

    # Dither + encode
    dithered = dither_to_spectra6_palette(composite)
    bin_payload = rgb_to_spectra6_codes_packed_4bit(dithered)

    # Upload
    targets = opts.get("esp_targets") or []
    ok, msg = _try_upload_to_targets(bin_payload, targets)
    if not ok:
        raise RuntimeError(f"Upload failed to all esp_targets. Last error: {msg}")

    return {"status": msg}


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.get("/")
def index():
    opts = _load_addon_options()
    secret = _ensure_secret_path(opts)
    return redirect(f"/{secret}/")


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/static_preview")
def static_preview():
    if not PREVIEW_PATH.exists():
        return jsonify({"error": "no preview yet"}), 404
    return send_file(str(PREVIEW_PATH), mimetype="image/png")


@app.get("/<secret>/")
def public_secret_page(secret: str):
    opts = _load_addon_options()
    actual = _ensure_secret_path(opts)
    if secret != actual:
        return ("Not Found", 404)

    base_path = _get_base_path()
    return render_template("index.html", secret=actual, base_path=base_path)


@app.post("/<secret>/")
def public_secret_upload(secret: str):
    opts = _load_addon_options()
    actual = _ensure_secret_path(opts)
    if secret != actual:
        return ("Not Found", 404)

    f = _get_uploaded_file_from_request()
    if f is None:
        return jsonify(
            {
                "ok": False,
                "error": "no file",
                "expected_fields": ["file", "image"],
                "received_fields": list(request.files.keys()),
            }
        ), 400

    if not f.filename:
        return jsonify({"ok": False, "error": "empty filename"}), 400

    try:
        img = Image.open(f.stream).convert("RGB")
    except Exception as e:
        return jsonify({"ok": False, "error": f"invalid image: {e}"}), 400

    try:
        result = _process_and_upload(img, opts)

        # Browser: show done page (with links)
        if _wants_html_response():
            base_path = _get_base_path()
            preview_url = f"{base_path}/static_preview"
            return render_template(
                "done.html",
                base_path=base_path,
                status=result.get("status", "ok"),
                preview_url=preview_url,
            )

        # API clients: return JSON
        return jsonify({"ok": True, "result": result})

    except Exception as e:
        return jsonify({"ok": False, "error": f"processing failed: {e}"}), 500


# Gunicorn entrypoint: "app.server:app"
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8088, debug=False)
