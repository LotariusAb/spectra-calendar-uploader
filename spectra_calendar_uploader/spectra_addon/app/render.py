from PIL import Image, ImageDraw, ImageFont, ImageOps
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
    """Smart Center-Crop: Schneidet das Bild immer perfekt zentriert zu."""
    # Verhindert DecompressionBombs und erzwingt das richtige Format ohne Verzerrung
    return ImageOps.fit(img, (target_w, target_h), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))

def _sanitize_calendar_text(s: str) -> str:
    if not s:
        return ""
    s = str(s)
    out = []
    for ch in s:
        o = ord(ch)
        if o in (0x200D, 0xFE0E, 0xFE0F):
            continue
        cat = unicodedata.category(ch)
        if cat in ("So", "Sk", "Cs", "Co"):
            continue
        out.append(ch)
    cleaned = " ".join("".join(out).split())
    return cleaned.strip()

def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width_px: int, max_lines: int) -> list[str]:
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
            lines[-1] = lines[-1].rstrip() + "…"

    return lines[:max_lines]

def _parse_event_times(ev: dict, tzinfo=None):
    s = ev.get("start", {}) or {}
    e = ev.get("end", {}) or {}

    if s.get("date"):
        try:
            sd = datetime.fromisoformat(s["date"])
            ed = datetime.fromisoformat(e.get("date") or s["date"])
            return True, sd, ed
        except Exception:
            return True, None, None

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

# --- Render Engines ---

def _render_month_view(draw, x0, y0, w, h, events_by_day, start_day, locale, font_day, font_event):
    """Zeichnet eine klassische Monatsübersicht (Grid)."""
    cols = 7
    rows = max(1, math.ceil(len(events_by_day) / cols))
    cell_w = w / cols
    cell_h = h / rows
    
    # Grid Linien zeichnen
    for r in range(rows + 1):
        y = y0 + r * cell_h
        draw.line([(x0, y), (x0 + w, y)], fill=SPECTRA6["black"], width=2 if r==0 else 1)
    for c in range(cols + 1):
        x = x0 + c * cell_w
        draw.line([(x, y0), (x, y0 + h)], fill=SPECTRA6["black"], width=2 if c==0 else 1)

    for day_idx, day_events in enumerate(events_by_day):
        r = day_idx // cols
        c = day_idx % cols
        cx0 = x0 + c * cell_w
        cy0 = y0 + r * cell_h
        
        day = start_day + timedelta(days=day_idx)
        day_label = format_date(day, format="EEE dd.", locale=locale)
        draw.text((cx0 + 4, cy0 + 4), day_label, fill=SPECTRA6["black"], font=font_day)
        
        all_day = [ev for ev in day_events if (ev.get("start", {}) or {}).get("date")]
        timed = [ev for ev in day_events if not (ev.get("start", {}) or {}).get("date")]
        
        # Ganztagestermine zeichnen
        ey = cy0 + _text_height(draw, "X", font_day) + 8
        for ev in all_day[:3]:  # max 3 um Platz zu sparen
            title = _sanitize_calendar_text(ev.get("summary") or "Event")
            if ev.get("_busy_only"):
                title = "Belegt"
            color = ev.get("_cal_color") or SPECTRA6["blue"]
            
            draw.rectangle([cx0 + 2, ey, cx0 + cell_w - 2, ey + 20], fill=color)
            # Text sicher abschneiden, wenn zu lang
            _draw_text_white_with_outline(draw, (cx0 + 4, ey + 2), title[:10] + ".." if len(title)>10 else title, font_event)
            ey += 22
            
        # Zähler für restliche Termine
        if timed:
            count_label = f"+{len(timed)} weitere"
            draw.text((cx0 + 4, cy0 + cell_h - _text_height(draw, "X", font_event) - 6), count_label, fill=SPECTRA6["red"], font=font_event)

def _render_week_view(draw, x0, y0, w, h, events_by_day, start_day, locale, timegrid_start, timegrid_end, step_min, font_day, font_event, max_lines, tzinfo):
    """Zeichnet die klassische Tages/Wochen-Skala."""
    padding = 8
    header_h = 44
    all_day_h = 32
    time_scale_w = 120 if w > 400 else 60 # Platz anpassen, falls im Landscape-Mode

    cols = max(1, len(events_by_day))
    grid_x0 = x0 + time_scale_w
    grid_x1 = x0 + w
    col_w = (grid_x1 - grid_x0) / cols

    has_all_day = any((ev.get("start", {}) or {}).get("date") for day in events_by_day for ev in day)
    all_day_row_h = all_day_h if has_all_day else 0

    grid_y0 = y0 + header_h + all_day_row_h
    grid_y1 = y0 + h
    grid_h = max(1, grid_y1 - grid_y0)

    start_hour = int(timegrid_start)
    end_hour = int(timegrid_end)
    total_minutes = max(1, (end_hour - start_hour) * 60)

    def minute_to_y(min_from_start: int) -> int:
        min_from_start = max(0, min(total_minutes, min_from_start))
        return int(grid_y0 + (min_from_start / total_minutes) * grid_h)

    # Header Row
    draw.rectangle([x0, y0, x0 + w, y0 + header_h], fill=SPECTRA6["white"])
    draw.rectangle([x0, y0, x0 + time_scale_w, y0 + header_h], fill=SPECTRA6["black"])
    draw.text((x0 + padding, y0 + 10), "Zeit", fill=SPECTRA6["white"], font=font_day)

    for day_idx in range(cols):
        cx0 = grid_x0 + day_idx * col_w
        day = start_day + timedelta(days=day_idx)
        day_label = format_date(day, format="EEE dd.MM", locale=locale)
        draw.rectangle([cx0, y0, cx0 + col_w, y0 + header_h], fill=SPECTRA6["black"])
        draw.text((cx0 + padding, y0 + 10), day_label, fill=SPECTRA6["white"], font=font_day)

    # Borders
    draw.line([(x0, y0), (x0 + w, y0)], fill=SPECTRA6["black"], width=2)
    draw.line([(x0, y0 + header_h), (x0 + w, y0 + header_h)], fill=SPECTRA6["black"], width=2)
    draw.line([(x0 + time_scale_w, y0), (x0 + time_scale_w, y0 + h)], fill=SPECTRA6["black"], width=2)
    
    for i in range(cols + 1):
        lx = grid_x0 + i * col_w
        draw.line([(lx, y0), (lx, y0 + h)], fill=SPECTRA6["black"], width=1)

    # All-Day Row
    if has_all_day:
        ay0 = y0 + header_h
        draw.rectangle([x0, ay0, x0 + w, ay0 + all_day_row_h], fill=SPECTRA6["white"])
        draw.line([(x0, ay0 + all_day_row_h), (x0 + w, ay0 + all_day_row_h)], fill=SPECTRA6["black"], width=2)
        draw.text((x0 + padding, ay0 + 6), "Ganztägig", fill=SPECTRA6["black"], font=font_event)

        for day_idx in range(cols):
            cx0 = grid_x0 + day_idx * col_w
            day_events = events_by_day[day_idx]
            all_day_events = [ev for ev in day_events if (ev.get("start", {}) or {}).get("date")]
            
            by = ay0 + 4
            bh = all_day_row_h - 8
            for ev in all_day_events[:2]:
                summary = "Belegt" if ev.get("_busy_only") else _sanitize_calendar_text(ev.get("summary") or "Event")
                color = ev.get("_cal_color") or SPECTRA6["blue"]
                bw = min(col_w - 8, int((col_w - 8) * 0.95))
                
                draw.rectangle([cx0 + 4, by, cx0 + 4 + bw, by + bh], fill=color, outline=SPECTRA6["black"], width=1)
                lines = _wrap_text(draw, summary, font_event, bw - 8, 1)
                if lines:
                    _draw_text_white_with_outline(draw, (cx0 + 8, by + 6), lines[0], font_event)
                by += bh + 2

    # Timegrid
    step = max(15, int(step_min))
    for minutes in range(0, total_minutes + 1, step):
        ly = minute_to_y(minutes)
        is_hour = ((start_hour * 60 + minutes) % 60) == 0
        draw.line([(x0, ly), (x0 + w, ly)], fill=SPECTRA6["black"], width=2 if is_hour else 1)
        
        if is_hour:
            hour = start_hour + (minutes // 60)
            label = f"{hour:02d}:00"
            th = _text_height(draw, label, font_event)
            ly_text = max(grid_y0 + 2, ly - th - 3)
            draw.text((x0 + padding, ly_text), label, fill=SPECTRA6["black"], font=font_event)

    # Timed Events
    for day_idx in range(cols):
        cx0 = grid_x0 + day_idx * col_w
        timed = [ev for ev in events_by_day[day_idx] if not (ev.get("start", {}) or {}).get("date")]
        placed = []

        for ev in timed:
            _, sd, ed = _parse_event_times(ev, tzinfo=tzinfo)
            if sd is None: continue
            if ed is None: ed = sd + timedelta(minutes=30)

            start_min = (sd.hour - start_hour) * 60 + sd.minute
            end_min = (ed.hour - start_hour) * 60 + ed.minute

            if end_min <= 0 or start_min >= total_minutes: continue
            
            y0_ev = minute_to_y(max(0, start_min))
            y1_ev = minute_to_y(min(total_minutes, max(start_min + 10, end_min)))
            if y1_ev - y0_ev < 18: y1_ev = y0_ev + 18

            color = ev.get("_cal_color") or SPECTRA6["blue"]
            
            offset = 0
            for (py0, py1, poff) in placed:
                if not (y1_ev <= py0 or y0_ev >= py1):
                    offset = max(offset, poff + 12)
            placed.append((y0_ev, y1_ev, offset))

            bx0 = cx0 + 4 + offset
            bx1 = cx0 + col_w - 4
            if bx0 >= bx1 - 20: bx0 = cx0 + 4

            draw.rectangle([bx0, y0_ev + 1, bx1, y1_ev - 1], fill=color, outline=SPECTRA6["black"], width=1)
            
            title = "Belegt" if ev.get("_busy_only") else _sanitize_calendar_text(ev.get("summary") or "Event")
            header = f"{sd.hour:02d}:{sd.minute:02d}–{ed.hour:02d}:{ed.minute:02d} {title}".strip()

            lines = _wrap_text(draw, header, font_event, (bx1 - bx0) - 8, max_lines)
            ty = y0_ev + 4
            for line in lines:
                if ty + 18 > y1_ev: break
                _draw_text_white_with_outline(draw, (bx0 + 4, ty), line, font_event)
                ty += 20


# --- Main Entry Point ---

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
    view_mode: str = "week",
    display_orientation: str = "portrait",
    font_size_day: int = 18,
    font_size_month: int = 14,
    **kwargs,
) -> Image.Image:
    
    if now_local is None:
        now_local = datetime.now()

    if events_by_day is None:
        events_by_day = [[] for _ in range(days_to_show)]

    canvas = Image.new("RGB", (out_w, out_h), SPECTRA6["white"])
    draw = ImageDraw.Draw(canvas)

    # Dynamic Grid System basierend auf Orientation
    if display_orientation == "landscape":
        # Bild links, Kalender rechts
        photo_w = int(out_w * top_ratio)
        photo_h = out_h
        cal_x0 = photo_w
        cal_y0 = 0
        cal_w = out_w - photo_w
        cal_h = out_h
    else:
        # Standard Portrait: Bild oben, Kalender unten
        photo_w = out_w
        photo_h = int(out_h * top_ratio)
        cal_x0 = 0
        cal_y0 = photo_h
        cal_w = out_w
        cal_h = out_h - photo_h

    # Foto platzieren (mit neuem Center-Crop Feature)
    if photo_w > 0 and photo_h > 0:
        photo_resized = _cover_resize(photo.convert("RGB"), photo_w, photo_h)
        canvas.paste(photo_resized, (0, 0))

    # Kalender Hintergrund
    draw.rectangle([cal_x0, cal_y0, cal_x0 + cal_w, cal_y0 + cal_h], fill=SPECTRA6["white"])

    # Fonts laden
    font_day = _load_font(font_size_day)
    font_event = _load_font(font_size_month)

    tzinfo = getattr(now_local, "tzinfo", None)

    # Rendering an die jeweiligen Engines übergeben
    if view_mode == "month":
        _render_month_view(
            draw, cal_x0, cal_y0, cal_w, cal_h, 
            events_by_day, now_local.date(), locale, 
            font_day, font_event
        )
    else:
        _render_week_view(
            draw, cal_x0, cal_y0, cal_w, cal_h, 
            events_by_day[:days_to_show], now_local.date(), locale, 
            timegrid_start_hour, timegrid_end_hour, timegrid_step_minutes, 
            font_day, font_event, max_lines_per_event, tzinfo
        )

    return canvas