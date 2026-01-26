import requests
from urllib.parse import urljoin


def upload_multipart(
    base_url: str,
    endpoint: str = "/upload",
    method: str = "POST",
    field_name: str = "data",
    data_bytes: bytes = b"",
    filename: str = "image_data.bin",
    content_type: str = "application/octet-stream",
    timeout_s: int = 60,
    **kwargs,
) -> tuple[int, str]:
    """
    Uploadt ein Payload an die ESP32 Firmware via multipart/form-data.

    Erwartet von NeoFrame typischerweise:
      - endpoint: /upload
      - field_name: data
      - filename: image_data.bin

    Robustheit:
      - toleriert zusätzliche kwargs (API-Drift)
      - method/endpoint/field_name haben Defaults
    """

    base = (base_url or "").rstrip("/") + "/"
    ep = (endpoint or "/upload").lstrip("/")
    url = urljoin(base, ep)

    m = (method or "POST").upper()

    files = {
        field_name: (
            filename,
            data_bytes,
            content_type or "application/octet-stream",
        )
    }

    resp = requests.request(m, url, files=files, timeout=timeout_s)

    body = resp.text if resp.text is not None else ""
    return int(resp.status_code), body


def upload_bin_multipart(
    base_url: str,
    endpoint: str = "/upload",
    method: str = "POST",
    field_name: str = "data",
    data_bytes: bytes = b"",
    filename: str = "image_data.bin",
    content_type: str | None = None,
    timeout_s: int = 60,
    **kwargs,
) -> tuple[int, str]:
    """
    Backwards kompatibel: alter BIN Upload.

    Häufiger Bug in älteren server.py Versionen:
      upload_bin_multipart(..., content_type="application/octet-stream") -> TypeError

    Fix:
      - akzeptiert content_type optional
      - toleriert zusätzliche kwargs
      - leitet intern an upload_multipart weiter
    """

    return upload_multipart(
        base_url=base_url,
        endpoint=endpoint,
        method=method,
        field_name=field_name,
        data_bytes=data_bytes,
        filename=filename,
        content_type=content_type or "application/octet-stream",
        timeout_s=timeout_s,
        **kwargs,
    )
