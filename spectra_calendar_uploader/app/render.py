from PIL import Image, ImageDraw, ImageFont
from datetime import datetime, timedelta
from babel.dates import format_date
import unicodedata
import math


SPECTRA6 = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "yellow": (255, 255, 0),
    "blue": (0, 0, 255),
    "green": (41, 204, 20),
}


def _load_font(size: int):
    try:
        return ImageFont.truetype("/usr/share/fonts/ttf-dejavu/DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _cover_resize(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    src_w, src_h = img.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = int(src_w * scale), int(src_h * scale)
    img2 = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img2.crop((left, top, left + target_w, top + target_h))


def _sanitize_calendar_text(s: str) -> str:
    """
    Entfernt Emojis/Icons (Unicode Symbol/Emoji-Bereiche).
    """
    if not s:
        return ""

    s = str(s)
    out = []
    for ch in s:
        o = ord(ch)

        # Variation Selectors, ZWJ
        if o in (0x200D, 0xFE0E, 0xFE0F):
            continue

        cat = unicodedata.category(ch)
        if cat in ("So", "Sk", "Cs", "Co"):
            continue

        out.append(ch)

    cleaned = "".join(out)
    cleaned = " ".join(cleaned.split())
    return cleaned.strip()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width_px: int, max_lines: int) -> list[str]:
    """
    Word wrap by pixel width.
    """
    text = (text or "").strip()
    if not text:
        return []

    words = text.split()
    lines = []
    current = ""

    def w_px(t: str) -> int:
        try:
            bbox = draw.textbbox((0, 0), t, font=font)
            return bbox[2] - bbox[0]
        except Exception:
            return len(t) * 8

    for w in words:
        candidate = (current + " " + w).strip()
        if w_px(candidate) <= max_width_px:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = w
            if len(lines) >= max_lines:
                break

    if len(lines) < max_lines and current:
        lines.append(current)

    if len(lines) >= max_lines and len(words) > 0:
        if not lines[-1].endswith("…"):
            lines[-1] = lines[-1].rstrip()
            if lines[-1]:
                lines[-1] = lines[-1] + "…"

    return lines[:max_lines]


def _parse_event_times(ev: dict, tzinfo=None):
    """
    Returns:
      (is_all_day, start_dt, end_dt)
    """
    s = ev.get("start", {}) or {}
    e = ev.get("end", {}) or {}

    # All day
    if s.get("date"):
        try:
            sd = datetime.fromisoformat(s["date"])
            ed = datetime.fromisoformat(e.get("date") or s["date"])
            return True, sd, ed
        except Exception:
            return True, None, None

    # Timed
    try:
        sd_raw = (s.get("dateTime") or "").replace("Z", "+00:00")
        ed_raw = (e.get("dateTime") or "").replace("Z", "+00:00")
        if not sd_raw:
            return False, None, None
        sd = datetime.fromisoformat(sd_raw)
        ed = datetime.fromisoformat(ed_raw) if ed_raw else None
        if tzinfo:
            sd = sd.astimezone(tzinfo)
            if ed:
                ed = ed.astimezone(tzinfo)
        return False, sd, ed
    except Exception:
        return False, None, None


def _draw_text_white_with_outline(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font):
    """
    Immer weiße Schrift + schwarze 1px Outline für Lesbarkeit (Spectra6-freundlich).
    """
    x, y = xy
    draw.text((x - 1, y), text, fill=SPECTRA6["black"], font=font)
    draw.text((x + 1, y), text, fill=SPECTRA6["black"], font=font)
    draw.text((x, y - 1), text, fill=SPECTRA6["black"], font=font)
    draw.text((x, y + 1), text, fill=SPECTRA6["black"], font=font)
    draw.text((x, y), text, fill=SPECTRA6["white"], font=font)


def _text_height(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[3] - bbox[1]
    except Exception:
        return 18


def render_composite(
    photo: Image.Image,
    out_w: int = 1200,
    out_h: int = 1600,
    top_ratio: float = 0.66,
    locale: str = "de_DE",
    now_local: datetime | None = None,
    events_by_day: list[list[dict]] | None = None,
    show_titles: bool = True,
    max_lines_per_day: int = 10,
    days_to_show: int = 7,
    max_lines_per_event: int = 2,
    timegrid_start_hour: int = 7,
    timegrid_end_hour: int = 20,
    timegrid_step_minutes: int = 60,
    **kwargs,
) -> Image.Image:
    """
    Ergebnis: portrait out_w x out_h.
      - oben Foto (cover)
      - unten Kalender:
          links Zeitskala, rechts Tages-Spalten,
          Events als farbige Blöcke (Spectra6)
    """

    if now_local is None:
        now_local = datetime.now()

    if events_by_day is None:
        events_by_day = [[] for _ in range(days_to_show)]
    elif len(events_by_day) < days_to_show:
        events_by_day = list(events_by_day) + ([[]] * (days_to_show - len(events_by_day)))
    elif len(events_by_day) > days_to_show:
        events_by_day = list(events_by_day)[:days_to_show]

    canvas = Image.new("RGB", (out_w, out_h), SPECTRA6["white"])
    draw = ImageDraw.Draw(canvas)

    # Split heights
    photo_h = int(out_h * top_ratio)
    cal_y0 = photo_h

    # Photo part
    photo_resized = _cover_resize(photo.convert("RGB"), out_w, photo_h)
    canvas.paste(photo_resized, (0, 0))

    # Calendar background
    draw.rectangle([0, cal_y0, out_w, out_h], fill=SPECTRA6["white"])

    # Fonts
    header_font = _load_font(22)
    time_font = _load_font(18)
    event_font = _load_font(18)

    # Layout constants
    padding = 8
    header_h = 44
    all_day_h = 32

    # Wider time scale so labels never clash/crop
    time_scale_w = 120

    cols = max(1, int(days_to_show))
    grid_x0 = time_scale_w
    grid_x1 = out_w
    col_w = (grid_x1 - grid_x0) // cols

    start_day = now_local.date()

    # Determine if there are any all-day events
    has_all_day = False
    for day_events in events_by_day:
        for ev in day_events:
            s = ev.get("start", {}) or {}
            if s.get("date"):
                has_all_day = True
                break
        if has_all_day:
            break

    all_day_row_h = all_day_h if has_all_day else 0

    # --- Auto-fit timegrid to events (prevents "cropped" early/late events) ---
    tzinfo = getattr(now_local, "tzinfo", None)
    min_minute = None
    max_minute = None

    for day_events in events_by_day:
        for ev in day_events:
            s = ev.get("start", {}) or {}
            if s.get("date"):
                continue  # all-day excluded
            _, sd, ed = _parse_event_times(ev, tzinfo=tzinfo)
            if sd is None:
                continue
            if ed is None:
                ed = sd + timedelta(minutes=30)

            sm = sd.hour * 60 + sd.minute
            em = ed.hour * 60 + ed.minute

            if min_minute is None or sm < min_minute:
                min_minute = sm
            if max_minute is None or em > max_minute:
                max_minute = em

    start_hour = int(timegrid_start_hour)
    end_hour = int(timegrid_end_hour)

    if min_minute is not None and max_minute is not None:
        event_start_hour = int(math.floor(min_minute / 60))
        event_end_hour = int(math.ceil(max_minute / 60))

        # Give 1h padding for readability (clamped)
        start_hour = max(0, min(start_hour, event_start_hour))
        end_hour = min(24, max(end_hour, event_end_hour))

    if end_hour <= start_hour:
        end_hour = min(24, start_hour + 1)

    total_minutes = max(1, (end_hour - start_hour) * 60)

    # Calendar grid area
    grid_y0 = cal_y0 + header_h + all_day_row_h
    grid_y1 = out_h
    grid_h = max(1, grid_y1 - grid_y0)

    def minute_to_y(min_from_start: int) -> int:
        min_from_start = max(0, min(total_minutes, min_from_start))
        return int(grid_y0 + (min_from_start / total_minutes) * grid_h)

    # Header row
    draw.rectangle([0, cal_y0, out_w, cal_y0 + header_h], fill=SPECTRA6["white"])
    draw.rectangle([0, cal_y0, time_scale_w, cal_y0 + header_h], fill=SPECTRA6["black"])
    draw.text((padding, cal_y0 + 10), "Zeit", fill=SPECTRA6["white"], font=header_font)

    for day_idx in range(cols):
        x0 = grid_x0 + day_idx * col_w
        x1 = x0 + col_w

        day = start_day + timedelta(days=day_idx)
        day_label = format_date(day, format="EEE dd.MM", locale=locale)

        draw.rectangle([x0, cal_y0, x1, cal_y0 + header_h], fill=SPECTRA6["black"])
        draw.text((x0 + padding, cal_y0 + 10), day_label, fill=SPECTRA6["white"], font=header_font)

    # Grid borders
    draw.line([(0, cal_y0), (out_w, cal_y0)], fill=SPECTRA6["black"], width=2)
    draw.line([(0, cal_y0 + header_h), (out_w, cal_y0 + header_h)], fill=SPECTRA6["black"], width=2)
    draw.line([(time_scale_w, cal_y0), (time_scale_w, out_h)], fill=SPECTRA6["black"], width=2)

    for i in range(cols + 1):
        x = grid_x0 + i * col_w
        draw.line([(x, cal_y0), (x, out_h)], fill=SPECTRA6["black"], width=1)

    # All-day row
    if has_all_day:
        y0 = cal_y0 + header_h
        y1 = y0 + all_day_row_h
        draw.rectangle([0, y0, out_w, y1], fill=SPECTRA6["white"])
        draw.line([(0, y1), (out_w, y1)], fill=SPECTRA6["black"], width=2)

        draw.text((padding, y0 + 6), "Ganztägig", fill=SPECTRA6["black"], font=time_font)

        for day_idx in range(cols):
            x0 = grid_x0 + day_idx * col_w
            day_events = events_by_day[day_idx] if day_idx < len(events_by_day) else []
            all_day_events = [ev for ev in day_events if (ev.get("start", {}) or {}).get("date")]

            if not all_day_events:
                continue

            bx = x0 + 4
            by = y0 + 4
            bh = all_day_row_h - 8
            max_w = col_w - 8

            for ev in all_day_events[:2]:
                summary = _sanitize_calendar_text(ev.get("summary") or "")
                if ev.get("_busy_only"):
                    summary = "Belegt"
                if not summary:
                    summary = "Event"

                color = ev.get("_cal_color") or SPECTRA6["blue"]
                bw = min(max_w, int(max_w * 0.95))

                draw.rectangle([bx, by, bx + bw, by + bh], fill=color, outline=SPECTRA6["black"], width=1)

                lines = _wrap_text(draw, summary, event_font, bw - 8, 1)
                if lines:
                    _draw_text_white_with_outline(draw, (bx + 4, by + 6), lines[0], event_font)

                by += bh + 2
                if by + bh > y1:
                    break

    # --- Timegrid lines + labels (labels ABOVE the line) ---
    step = max(15, int(timegrid_step_minutes))

    for minutes in range(0, total_minutes + 1, step):
        y = minute_to_y(minutes)
        is_hour = ((start_hour * 60 + minutes) % 60) == 0

        draw.line([(0, y), (out_w, y)], fill=SPECTRA6["black"], width=2 if is_hour else 1)

        if is_hour:
            hour = start_hour + (minutes // 60)
            label = f"{hour:02d}:00"
            th = _text_height(draw, label, time_font)

            # place above the line, not on the line
            ly = y - th - 3
            if ly < grid_y0 + 2:
                ly = grid_y0 + 2

            draw.text((padding, ly), label, fill=SPECTRA6["black"], font=time_font)

    # Render timed events
    for day_idx in range(cols):
        x0 = grid_x0 + day_idx * col_w
        x1 = x0 + col_w

        day_events = events_by_day[day_idx] if day_idx < len(events_by_day) else []
        timed = [ev for ev in day_events if not (ev.get("start", {}) or {}).get("date")]

        placed = []

        for ev in timed:
            _, sd, ed = _parse_event_times(ev, tzinfo=tzinfo)
            if sd is None:
                continue
            if ed is None:
                ed = sd + timedelta(minutes=30)

            start_min = (sd.hour - start_hour) * 60 + sd.minute
            end_min = (ed.hour - start_hour) * 60 + ed.minute

            if end_min <= 0 or start_min >= total_minutes:
                continue
            start_min = max(0, start_min)
            end_min = min(total_minutes, max(start_min + 10, end_min))

            y0_ev = minute_to_y(start_min)
            y1_ev = minute_to_y(end_min)
            if y1_ev - y0_ev < 18:
                y1_ev = y0_ev + 18

            color = ev.get("_cal_color") or SPECTRA6["blue"]

            offset = 0
            for (py0, py1, poff) in placed:
                if not (y1_ev <= py0 or y0_ev >= py1):
                    offset = max(offset, poff + 12)
            placed.append((y0_ev, y1_ev, offset))

            bx0 = x0 + 4 + offset
            bx1 = x1 - 4
            if bx0 >= bx1 - 20:
                bx0 = x0 + 4

            draw.rectangle([bx0, y0_ev + 1, bx1, y1_ev - 1], fill=color, outline=SPECTRA6["black"], width=1)

            if ev.get("_busy_only"):
                title = "Belegt"
            else:
                title = _sanitize_calendar_text(ev.get("summary") or "")
                if not title:
                    title = "Event"

            # NEW: show "von–bis"
            start_label = f"{sd.hour:02d}:{sd.minute:02d}"
            end_label = f"{ed.hour:02d}:{ed.minute:02d}"
            header = f"{start_label}–{end_label} {title}".strip()

            lines = _wrap_text(draw, header, event_font, (bx1 - bx0) - 8, max_lines_per_event)
            ty = y0_ev + 4
            for line in lines:
                if ty + 18 > y1_ev:
                    break
                _draw_text_white_with_outline(draw, (bx0 + 4, ty), line, event_font)
                ty += 20

    return canvas
