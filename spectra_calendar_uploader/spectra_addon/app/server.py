import io
import json
import os
import secrets
import string
import logging
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_file
from PIL import Image

# Local imports
try:
    from .ha_calendar import fetch_calendar_events
    from .render import render_composite
    from .dithering import dither_to_spectra6_palette
    from .spectra6_encode import rgb_to_spectra6_codes_packed_4bit
    from .esp_upload import upload_bin_multipart
except ImportError:
    from ha_calendar import fetch_calendar_events
    from render import render_composite
    from dithering import dither_to_spectra6_palette
    from spectra6_encode import rgb_to_spectra6_codes_packed_4bit
    from esp_upload import upload_bin_multipart

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"

# Persistence paths
SECRET_PATH_FILE = DATA_DIR / "secret_path.txt"
VIEW_MODE_FILE = DATA_DIR / "view_mode.txt"
ORIGINAL_IMAGE_PATH = DATA_DIR / "original_upload.png"

STATIC_OUT_DIR = DATA_DIR / "out"
STATIC_OUT_DIR.mkdir(parents=True, exist_ok=True)
PREVIEW_PATH = STATIC_OUT_DIR / "preview.png"

app = Flask(
    __name__,
    template_folder=str(APP_DIR / "templates"),
    static_folder=str(APP_DIR / "static"),
)

# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
_DEFAULT_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _DEFAULT_LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("spectra_uploader.server")

def _configure_logging_from_options(opts: dict) -> None:
    level_name = str(opts.get("log_level") or "").strip().upper()
    if not level_name:
        return
    level = getattr(logging, level_name, None)
    if level is None:
        logger.warning("Invalid log_level in options.json: %r", level_name)
        return
    logging.getLogger().setLevel(level)
    logger.setLevel(level)

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _load_addon_options() -> dict:
    if OPTIONS_PATH.exists():
        with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def _read_persisted_secret_path() -> str | None:
    try:
        if SECRET_PATH_FILE.exists():
            return SECRET_PATH_FILE.read_text(encoding="utf-8").strip() or None
    except Exception:
        logger.exception("Failed reading secret path.")
    return None

def _write_persisted_secret_path(secret_path: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SECRET_PATH_FILE.write_text(secret_path.strip() + "\n", encoding="utf-8")
    except Exception:
        logger.exception("Failed writing secret path.")
        raise

def _ensure_secret_path(opts: dict) -> str:
    configured = (opts.get("secret_path") or "").strip()
    if configured:
        persisted = _read_persisted_secret_path()
        if persisted != configured:
            logger.info("Persisting configured secret_path.")
            _write_persisted_secret_path(configured)
        return configured

    persisted = _read_persisted_secret_path()
    if persisted:
        return persisted

    alphabet = string.ascii_letters + string.digits + "-_"
    secret_path = "".join(secrets.choice(alphabet) for _ in range(43))
    _write_persisted_secret_path(secret_path)

    logger.warning("Generated new secret_path on first run.")
    logger.warning("NFC URL (LAN, no HA login): http://<HA-IP>:8088/%s/", secret_path)
    return secret_path

def _get_base_path() -> str:
    return (request.headers.get("X-Ingress-Path") or "").rstrip("/")

def _wants_html_response() -> bool:
    return "text/html" in (request.headers.get("Accept") or "").lower()

def _get_uploaded_file_from_request():
    if not request.files:
        return None
    if "file" in request.files:
        return request.files["file"]
    if "image" in request.files:
        return request.files["image"]
    try:
        return next(iter(request.files.values()))
    except Exception:
        return None

def _get_view_mode() -> str:
    """Reads the persistent view mode (week or month)."""
    if VIEW_MODE_FILE.exists():
        return VIEW_MODE_FILE.read_text(encoding="utf-8").strip().lower()
    return "week"

def _set_view_mode(mode: str) -> None:
    """Writes the persistent view mode."""
    VIEW_MODE_FILE.write_text(mode.strip().lower(), encoding="utf-8")

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
        "black": (0, 0, 0), "blue": (0, 0, 255), "green": (41, 204, 20),
        "red": (255, 0, 0), "yellow": (255, 255, 0), "white": (255, 255, 255),
    }
    return palette.get(n)

def _parse_event_start_dt(ev: dict) -> datetime | None:
    s = ev.get("start")
    if not s:
        return None
    if isinstance(s, str):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            if len(s) >= 10:
                try:
                    return datetime.fromisoformat(s[:10])
                except Exception:
                    pass
    return None

def _try_upload_to_targets(bin_payload: bytes, targets: list[dict]) -> tuple[bool, str]:
    last_err = ""
    for t in targets:
        base_url = (t.get("base_url") or "").rstrip("/")
        if not base_url:
            continue
        timeout_s = int(t.get("timeout_s") or 20)
        try:
            logger.info("Upload attempt: base_url=%s payload_bytes=%s", base_url, len(bin_payload))
            upload_bin_multipart(
                base_url=base_url,
                data_bytes=bin_payload,
                field_name="data",
                filename="image_data.bin",
                content_type="application/octet-stream",
                timeout_s=timeout_s,
            )
            return True, f"Uploaded to {base_url}"
        except Exception as e:
            logger.exception("Upload attempt failed for base_url=%s", base_url)
            last_err = f"{type(e).__name__}: {e}"

    return False, last_err or "No esp_targets configured."

# ---------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------
def _process_and_upload(img: Image.Image, opts: dict, view_mode: str = "week"):
    _configure_logging_from_options(opts)
    req_id = uuid.uuid4().hex[:12]
    t0 = time.perf_counter()

    out_w = int(opts.get("out_width", 1200))
    out_h = int(opts.get("out_height", 1600))
    top_ratio = float(opts.get("top_ratio", 0.66))
    
    # New Config Params
    display_orientation = opts.get("display_orientation", "portrait").strip().lower()
    font_size_day = int(opts.get("font_size_day", 18))
    font_size_month = int(opts.get("font_size_month", 14))

    # Override days_to_show based on view_mode
    days_to_show = int(opts.get("days_to_show", 7))
    if view_mode == "month":
        days_to_show = 28  # 4 weeks for month view

    show_titles = bool(opts.get("show_titles", True))
    max_lines_per_day = int(opts.get("max_lines_per_day", 10))
    max_lines_per_event = int(opts.get("max_lines_per_event", 3))
    timegrid_start_hour = int(opts.get("timegrid_start_hour", 7))
    timegrid_end_hour = int(opts.get("timegrid_end_hour", 20))
    timegrid_step_minutes = int(opts.get("timegrid_step_minutes", 60))

    logger.info("[%s] Starting pipeline: mode=%s orientation=%s days=%s", req_id, view_mode, display_orientation, days_to_show)

    cal_sources = _parse_calendar_sources(opts)
    entity_ids = [c["entity_id"] for c in cal_sources if c.get("entity_id")]

    start_dt = datetime.now()
    end_dt = start_dt + timedelta(days=days_to_show)

    events = []
    for eid in entity_ids:
        t_fetch0 = time.perf_counter()
        evs = fetch_calendar_events(eid, start_dt, end_dt) or []
        for e in evs:
            if isinstance(e, dict):
                e["entity_id"] = eid
        events.extend(evs)

    logger.info("[%s] Total events fetched: %s", req_id, len(events))

    cal_color_map = {c["entity_id"]: _color_name_to_rgb(c.get("color")) for c in cal_sources if c.get("entity_id")}
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

    t_render0 = time.perf_counter()
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
        events=events,
        calendar_colors={k: v for k, v in cal_color_map.items() if v},
        # Pass new dynamic configs to the render engine
        view_mode=view_mode,
        display_orientation=display_orientation,
        font_size_day=font_size_day,
        font_size_month=font_size_month
    )
    logger.info("[%s] Render completed in %.1fms", req_id, (time.perf_counter() - t_render0) * 1000)

    composite.save(PREVIEW_PATH, format="PNG")

    t_dither0 = time.perf_counter()
    dithered = dither_to_spectra6_palette(composite)
    logger.info("[%s] Dithering completed in %.1fms", req_id, (time.perf_counter() - t_dither0) * 1000)

    t_enc0 = time.perf_counter()
    bin_payload = rgb_to_spectra6_codes_packed_4bit(dithered)
    logger.info("[%s] Encoding completed in %.1fms. Payload: %s bytes", req_id, (time.perf_counter() - t_enc0) * 1000, len(bin_payload))

    targets = opts.get("esp_targets") or []
    t_up0 = time.perf_counter()
    ok, msg = _try_upload_to_targets(bin_payload, targets)
    
    if not ok:
        raise RuntimeError(f"Upload failed to all targets. Last error: {msg}")

    logger.info("[%s] Pipeline finished successfully in %.1fms", req_id, (time.perf_counter() - t0) * 1000)
    return {"status": msg}

# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------
@app.get("/")
def index():
    opts = _load_addon_options()
    _configure_logging_from_options(opts)
    secret = _ensure_secret_path(opts)
    return redirect(f"/{secret}/")

@app.get("/health")
def health():
    return jsonify({"ok": True})

@app.get("/static_preview")
def static_preview():
    if not PREVIEW_PATH.exists():
        return jsonify({"error": "No preview generated yet."}), 404
    return send_file(str(PREVIEW_PATH), mimetype="image/png")

@app.get("/<secret>/")
def public_secret_page(secret: str):
    opts = _load_addon_options()
    _configure_logging_from_options(opts)
    actual = _ensure_secret_path(opts)
    if secret != actual:
        return ("Not Found", 404)
    base_path = _get_base_path()
    return render_template("index.html", secret=actual, base_path=base_path)

@app.post("/<secret>/")
def public_secret_upload(secret: str):
    opts = _load_addon_options()
    _configure_logging_from_options(opts)
    if secret != _ensure_secret_path(opts):
        return ("Not Found", 404)

    logger.info("Incoming upload request from %s", request.remote_addr)

    f = _get_uploaded_file_from_request()
    if f is None or not f.filename:
        logger.warning("Upload rejected: Missing file or filename.")
        return jsonify({"ok": False, "error": "No file provided."}), 400

    try:
        img = Image.open(f.stream).convert("RGB")
        # Feature: Save original image for later refreshes
        img.save(ORIGINAL_IMAGE_PATH, format="PNG")
        logger.debug("Original image saved to persistence layer.")
    except Exception as e:
        logger.exception("Upload rejected: Invalid image data.")
        return jsonify({"ok": False, "error": f"Invalid image: {e}"}), 400

    try:
        view_mode = _get_view_mode()
        result = _process_and_upload(img, opts, view_mode)
        if _wants_html_response():
            return render_template(
                "done.html", 
                base_path=_get_base_path(), 
                status=result.get("status", "ok"), 
                preview_url=f"{_get_base_path()}/static_preview"
            )
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        logger.exception("Processing pipeline failed.")
        return jsonify({"ok": False, "error": str(e)}), 500

# NEW ENDPOINT: Toggle View Mode
@app.post("/<secret>/view_mode")
def set_view_mode(secret: str):
    opts = _load_addon_options()
    _configure_logging_from_options(opts)
    if secret != _ensure_secret_path(opts):
        return ("Not Found", 404)

    data = request.json or {}
    mode = data.get("mode", "").strip().lower()
    
    if mode not in ["week", "month"]:
        logger.warning("Invalid view mode requested: %s", mode)
        return jsonify({"ok": False, "error": "Invalid mode. Use 'week' or 'month'."}), 400

    _set_view_mode(mode)
    logger.info("View mode successfully changed to: %s", mode)

    # Automatically refresh the display with the new mode
    if ORIGINAL_IMAGE_PATH.exists():
        try:
            logger.info("Triggering automatic refresh for new view mode.")
            img = Image.open(ORIGINAL_IMAGE_PATH).convert("RGB")
            _process_and_upload(img, opts, mode)
        except Exception as e:
            logger.exception("Background refresh after mode change failed.")
            return jsonify({"ok": True, "mode": mode, "warning": "Mode saved, but display refresh failed."})

    return jsonify({"ok": True, "mode": mode})

# NEW ENDPOINT: HA Automations Webhook
@app.post("/<secret>/refresh")
def refresh_display(secret: str):
    opts = _load_addon_options()
    _configure_logging_from_options(opts)
    if secret != _ensure_secret_path(opts):
        return ("Not Found", 404)

    logger.info("Incoming refresh webhook from Home Assistant.")

    if not ORIGINAL_IMAGE_PATH.exists():
        logger.warning("Refresh failed: No original image found to render.")
        return jsonify({"ok": False, "error": "Upload an image first before refreshing."}), 400

    try:
        img = Image.open(ORIGINAL_IMAGE_PATH).convert("RGB")
        view_mode = _get_view_mode()
        result = _process_and_upload(img, opts, view_mode)
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        logger.exception("Refresh pipeline failed.")
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8088, debug=False)