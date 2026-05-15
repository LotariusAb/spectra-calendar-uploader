# Spectra 6 Photo + Calendar Uploader (Home Assistant Add-on)

**Disclaimer: This was done by someone with rookie knowledge and a lot of ChatGPT. It is working but that is all. However it was a lot of fun to do and I'm happy with the result. Feel free to adapt and use it.**

A Home Assistant Add-on that renders a **photo + calendar** composite, dithers it to the **GoodDisplay Spectra 6** palette, encodes it to the **NeoFrame packed 4-bit format**, and uploads it to one or more **ESP32 E‑Ink frames** via HTTP multipart upload.

This project is designed to run on **Home Assistant OS (HAOS)** as a supervised add-on container and exposes both:

- a **Web UI** (Ingress + optional LAN port) for interactive uploads
- a **simple HTTP API** for automation (Apple Shortcuts, scripts, CI, etc.)

---

## What it does

End-to-end pipeline:

1. User uploads an image (Web UI or API)
2. Add-on fetches calendar events from Home Assistant Calendar entities
3. Renders a composite image:
   - top: photo
   - bottom: calendar grid + event entries
4. Dithers the composite to the Spectra 6 palette (exact RGB values)
5. Encodes the image into packed 4-bit nibbles (2 pixels per byte)
6. Uploads the binary payload to one or more ESP32 targets via multipart HTTP (`/upload`)

---

## Hardware/Firmware constraints (must be respected)
Tested on following hardware:
- https://www.good-display.com/product/574.html

Output resolution (portrait):

- **1200 x 1600**

Allowed colors (Spectra 6 palette only, exact RGB):

- Black: `(0, 0, 0)`
- Blue: `(0, 0, 255)`
- Green: `(41, 204, 20)`
- Red: `(255, 0, 0)`
- Yellow: `(255, 255, 0)`
- White: `(255, 255, 255)`

Encoding:

- Packed **4-bit** codes (2 pixels per byte)
- Code mapping follows NeoFrame conventions.

---

## Architecture overview

Key entrypoints:

- `app/server.py`  
  Flask app, routes, orchestration, options, and upload handling
- `app/ha_calendar.py`  
  Home Assistant Calendar API client (Supervisor/Core proxy)
- `app/render.py`  
  Photo + calendar composition (layout and typography)
- `app/dithering.py`  
  Floyd–Steinberg quantization to Spectra 6 palette
- `app/spectra6_encode.py`  
  RGB → packed 4-bit payload
- `app/esp_upload.py`  
  HTTP multipart uploader for ESP32 targets

Processing path:

`route -> render -> dither -> encode -> upload`

---

## Installation (Home Assistant Add-on)

1. Add this repository as a custom add-on repository in Home Assistant.
2. Install the add-on.
3. Configure options (see below).
4. Start the add-on.

The add-on provides:

- **Ingress Web UI** (via Home Assistant sidebar)
- Optional **direct LAN port** for NFC / guest devices / no HA login

---

## Configuration

Configuration is stored in `options.json` (managed by Home Assistant).

### Minimal configuration

You typically need:

- at least one `esp_targets` entry
- one or more `calendar_sources` entries (optional but recommended)

Example:

```yaml
secret_path: ""  # empty = auto-generate on first start

# Optional: protect public secret URL to local networks only (best-effort CIDR check)
allowed_cidrs:
  - "192.168.178.0/24"
  - "100.64.0.0/10"   # optional: Tailscale CGNAT range

# ESP32 targets that accept multipart upload at <base_url>/upload
esp_targets:
  - name: "LivingRoomFrame"
    base_url: "http://192.168.178.80"
    timeout_s: 20

# Calendar sources from Home Assistant
calendar_sources:
  - entity_id: "calendar.heimmerko_gmail_com"
    color: "blue"
  - entity_id: "calendar.family"
    color: "yellow"

# Rendering settings
out_width: 1200
out_height: 1600
top_ratio: 0.66
days_to_show: 7
show_titles: true

max_lines_per_day: 10
max_lines_per_event: 3

timegrid_start_hour: 7
timegrid_end_hour: 20
timegrid_step_minutes: 60

rotate_180: false
```

### Notes on important options

#### `secret_path`
The add-on exposes a public URL under:

```
http://<HA-IP>:8088/<secret_path>/
```

If `secret_path` is empty, it is generated automatically on first start and written back to `/data/options.json`.

This secret path is intended to be written to an NFC tag (or used in Apple Shortcuts) to allow uploading without Home Assistant login.

#### `allowed_cidrs`
Optional lightweight protection of the public secret endpoint. When set, the add-on checks the requester IP against these ranges.

Recommended if you expose the port within your LAN or via overlay networks.

#### `esp_targets`
List of one or more ESP32 targets. Each target must be reachable and should expose an upload endpoint at:

```
<base_url>/upload
```

#### `calendar_sources`
List of Home Assistant Calendar entity IDs. Each can optionally include a `color` which is mapped to a Spectra 6 palette color (for visual differentiation).

Supported color names:

- black, blue, green, red, yellow, white

---

## Web UI usage

1. Open the add-on Web UI via Home Assistant Ingress
2. Upload an image
3. After successful processing:
   - you are redirected to a success page
   - you can view a preview of the rendered composite

Preview endpoint:

```
GET /static_preview
```

The preview is stored as a single file and overwritten each upload to prevent disk growth.

---

## API usage

### Upload image (multipart/form-data)

Endpoint:

```
POST http://<HA-IP>:8088/<secret_path>/
```

Form field names accepted:

- `image` (used by the Web UI)
- `file` (common in API clients)

Example using `curl`:

```bash
curl -X POST   -H "Accept: application/json"   -F "image=@/path/to/photo.jpg"   "http://<HA-IP>:8088/<secret_path>/"
```

Typical response:

```json
{
  "ok": true,
  "result": {
    "status": "Uploaded to http://192.168.178.80"
  }
}
```

### Download preview

```bash
curl -L "http://<HA-IP>:8088/static_preview" --output preview.png
```

---

## Apple Shortcuts (recommended setup)

Use the action **“Get Contents of URL”** with:

- Method: `POST`
- Request body: **Form**
- Field:
  - Key: `image`
  - Type: **File**
  - Value: the chosen/converted image
- Header (recommended):
  - `Accept: application/json`

The URL must be the secret endpoint:

```
http://<HA-IP>:8088/<secret_path>/
```

---

## Troubleshooting

### 400: `{"error":"no file"}`
Cause: the request did not include a multipart file field.

Fix:
- Use `multipart/form-data`
- Use field name `image` or `file`

### Calendar API 404 or “Not Found”
Cause: wrong calendar entity_id or wrong URL construction.

Fix:
- Verify the entity exists in Home Assistant (Developer Tools → States)
- Ensure it starts with `calendar.`

### Upload timeouts
Cause: ESP32 target not reachable or slow upload.

Fix:
- Verify `base_url` is reachable from the add-on container
- Increase `timeout_s` in `esp_targets`

### Palette violations / wrong colors on display
Cause: any pixel outside the strict Spectra 6 RGB palette before encoding.

Fix:
- Ensure dithering runs and outputs only allowed RGB values
- Do not modify palette values

### Disk usage growth
This add-on is designed to avoid disk growth by overwriting the preview image (`/data/out/preview.png`) on each upload.
If you add debug output, keep it bounded (e.g., overwrite instead of timestamp accumulation).

---

## Security considerations

- The `secret_path` URL provides access without Home Assistant login.
- Restrict access using `allowed_cidrs` where possible.
- Prefer overlay networks (e.g., Tailscale) if accessing remotely.
- Do not expose the port publicly without additional protection.

---

## Development notes

- Production runs under **Gunicorn**, so imports must work when `app.server` is imported as a package module.
- Avoid Flask patterns that are not supported in the pinned runtime (e.g., `before_serving` in older Flask versions).
- Keep function signatures stable across entrypoints to avoid runtime mismatch:
  - `render_composite(...)`
  - `upload_bin_multipart(...)`

