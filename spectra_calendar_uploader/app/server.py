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

# Local imports (must work under Gunicorn package import and direct dev execution)
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

# Persist secret independently from options.json (Supervisor-owned)
SECRET_PATH_FILE = DATA_DIR / "secret_path.txt"

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
logger = logging.getLogger("spectra_uploader")


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
            v = SECRET_PATH_FILE.read_text(encoding="utf-8").strip()
            return v or None
    except Exception:
        logger.exception("Failed reading %s", str(SECRET_PATH_FILE))
    return None


def _write_persisted_secret_path(secret_path: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SECRET_PATH_FILE.write_text(secret_path.strip() + "\n", encoding="utf-8")
    except Exception:
        logger.exception("Failed writing %s", str(SECRET_PATH_FILE))
        raise


def _ensure_secret_path(opts: dict) -> str:
    """
    Correct persistence model for HA add-ons:

    Priority:
      1) User-provided options.json secret_path (explicit config)
      2) Persisted /data/secret_path.txt (generated once, survives restarts)
      3) Generate new + persist to /data/secret_path.txt

    IMPORTANT:
    - We do NOT write back to /data/options.json (Supervisor-owned).
    """

    # 1) Explicit configured secret_path in add-on options
    configured = (opts.get("secret_path") or "").strip()
    if configured:
        persisted = _read_persisted_secret_path()
        if persisted != configured:
            logger.info("Persisting configured secret_path to %s", str(SECRET_PATH_FILE))
            _write_persisted_secret_path(configured)
        return configured

    # 2) Previously generated secret (persisted)
    persisted = _read_persisted_secret_path()
    if persisted:
        return persisted

    # 3) First run: generate and persist
    alphabet = string.ascii_letters + string.digits + "-_"
    secret_path = "".join(secrets.choice(alphabet) for _ in range(43))
    _write_persisted_secret_path(secret_path)

    logger.warning("Generated secret_path on first run and persisted it.")
    logger.warning("Persisted at: %s", str(SECRET_PATH_FILE))
    logger.warning("NFC URL (LAN, no HA login): http://<HA-IP>:8088/%s/", secret_path)

    return secret_path


def _get_base_path() -> str:
    p = (request.headers.get("X-Ingress-Path") or "").rstrip("/")
    return p


def _wants_html_response() -> bool:
    accept = (request.headers.get("Accept") or "").lower()
    return "text/html" in accept


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
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
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
            logger.info(
                "Upload attempt: base_url=%s timeout_s=%s payload_bytes=%s",
                base_url,
                timeout_s,
                len(bin_payload),
            )
            # FIX: send actual bytes via data_bytes (matches esp_upload + NeoFrame JS)
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
            logger.exception("Upload attempt failed: base_url=%s", base_url)
            last_err = f"{type(e).__name__}: {e}"

    return False, last_err or "No targets configured"


# ---------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------
def _process_and_upload(img: Image.Image, opts: dict):
    _configure_logging_from_options(opts)

    req_id = uuid.uuid4().hex[:12]
    t0 = time.perf_counter()

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

    logger.info(
        "[%s] Start pipeline: img=%sx%s out=%sx%s top_ratio=%.3f days=%s rotate_180=%s",
        req_id,
        img.size[0],
        img.size[1],
        out_w,
        out_h,
        top_ratio,
        days_to_show,
        rotate_180,
    )

    cal_sources = _parse_calendar_sources(opts)
    entity_ids = [c["entity_id"] for c in cal_sources if c.get("entity_id")]

    logger.info("[%s] calendar_sources=%s entity_ids=%s", req_id, len(cal_sources), entity_ids)

    start_dt = datetime.now()
    end_dt = start_dt + timedelta(days=days_to_show)

    events = []
    for eid in entity_ids:
        logger.info("[%s] Fetch events: %s start=%s end=%s", req_id, eid, start_dt.isoformat(), end_dt.isoformat())
        t_fetch0 = time.perf_counter()
        evs = fetch_calendar_events(eid, start_dt, end_dt) or []
        logger.info("[%s] Fetch events done: %s count=%s dt_ms=%.1f", req_id, eid, len(evs), (time.perf_counter() - t_fetch0) * 1000)
        for e in evs:
            if isinstance(e, dict):
                e["entity_id"] = eid
        events.extend(evs)

    logger.info("[%s] Total events=%s", req_id, len(events))

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
            logger.debug(
                "[%s] Skip event without parseable start: %s",
                req_id,
                {k: ev.get(k) for k in ("summary", "start", "end", "entity_id")},
            )
            continue

        day_idx = (ev_start.date() - start_date).days
        if 0 <= day_idx < days_to_show:
            events_by_day[day_idx].append(ev)

    logger.info(
        "[%s] Render composite: max_lines_per_day=%s max_lines_per_event=%s timegrid=%02d-%02d step=%s",
        req_id,
        max_lines_per_day,
        max_lines_per_event,
        timegrid_start_hour,
        timegrid_end_hour,
        timegrid_step_minutes,
    )

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
        # backwards compatibility
        events=events,
        calendar_colors={k: v for k, v in cal_color_map.items() if v},
    )
    logger.info("[%s] Render done dt_ms=%.1f", req_id, (time.perf_counter() - t_render0) * 1000)

    if rotate_180:
        composite = composite.rotate(180, expand=False)

    composite.save(PREVIEW_PATH, format="PNG")
    logger.info("[%s] Preview saved: %s", req_id, str(PREVIEW_PATH))

    t_dither0 = time.perf_counter()
    dithered = dither_to_spectra6_palette(composite)
    logger.info("[%s] Dither done dt_ms=%.1f", req_id, (time.perf_counter() - t_dither0) * 1000)

    t_enc0 = time.perf_counter()
    bin_payload = rgb_to_spectra6_codes_packed_4bit(dithered)
    logger.info(
        "[%s] Encode done dt_ms=%.1f payload_bytes=%s (expected=%s)",
        req_id,
        (time.perf_counter() - t_enc0) * 1000,
        len(bin_payload),
        (out_w * out_h) // 2,
    )

    targets = opts.get("esp_targets") or []
    logger.info("[%s] Upload: targets=%s", req_id, [t.get("base_url") for t in targets if isinstance(t, dict)])

    t_up0 = time.perf_counter()
    ok, msg = _try_upload_to_targets(bin_payload, targets)
    logger.info("[%s] Upload finished ok=%s dt_ms=%.1f msg=%s", req_id, ok, (time.perf_counter() - t_up0) * 1000, msg)

    if not ok:
        raise RuntimeError(f"Upload failed to all esp_targets. Last error: {msg}")

    logger.info("[%s] Pipeline finished dt_ms=%.1f", req_id, (time.perf_counter() - t0) * 1000)
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
        return jsonify({"error": "no preview yet"}), 404
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
    actual = _ensure_secret_path(opts)
    if secret != actual:
        return ("Not Found", 404)

    logger.info(
        "Incoming upload: path_secret=%s remote_addr=%s user_agent=%s content_type=%s content_length=%s",
        secret,
        request.remote_addr,
        str(request.user_agent),
        request.content_type,
        request.content_length,
    )

    f = _get_uploaded_file_from_request()
    if f is None:
        logger.warning("Upload rejected: no file. fields=%s", list(request.files.keys()))
        return jsonify(
            {
                "ok": False,
                "error": "no file",
                "expected_fields": ["file", "image"],
                "received_fields": list(request.files.keys()),
            }
        ), 400

    if not f.filename:
        logger.warning("Upload rejected: empty filename")
        return jsonify({"ok": False, "error": "empty filename"}), 400

    try:
        img = Image.open(f.stream).convert("RGB")
    except Exception as e:
        logger.exception("Upload rejected: invalid image")
        return jsonify({"ok": False, "error": f"invalid image: {e}"}), 400

    logger.info("Upload accepted: filename=%s image_size=%sx%s", f.filename, img.size[0], img.size[1])

    try:
        result = _process_and_upload(img, opts)

        if _wants_html_response():
            base_path = _get_base_path()
            preview_url = f"{base_path}/static_preview"
            return render_template(
                "done.html",
                base_path=base_path,
                status=result.get("status", "ok"),
                preview_url=preview_url,
            )

        return jsonify({"ok": True, "result": result})

    except Exception as e:
        logger.exception("Processing failed")
        return jsonify({"ok": False, "error": f"processing failed: {e}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8088, debug=False)