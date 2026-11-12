
#!/usr/bin/env python3
"""IRIDE - Diagnostica prodotti Radar-DPC."""
import ipaddress
import json
import math
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import requests
from pyproj import Transformer
from rasterio.io import MemoryFile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/iride.json"
DEST = ROOT / "docs/data/dpc3d/diagnostics.json"
API = "https://radar-api.protezionecivile.it/"
HEADERS = {
    "Origin": "https://19francomenenti73-dev.github.io",
    "Accept": "application/json",
}
PRODUCTS = ("VIL", "ETM", "POH", "VMI")
CREDIT = "Dipartimento della Protezione Civile - Radar-DPC"
LICENSE = "CC BY-SA 4.0"
SOURCE_URL = "https://radar.protezionecivile.it/"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
MAX_AGE = 90 * 60 * 1000
MAX_DIFFERENCE = 10 * 60 * 1000

# Unita' documentate per i prodotti DPC.
# POH non viene convertito senza verifica della
# codifica numerica effettiva.
DOCUMENTED_UNITS = {
    "VIL": "kg/m²",
    "ETM": "m",
    "VMI": "dBZ",
}
UNIT_SOURCES = {
    "VIL": "DPC IT-alert - contenuto colonnare",
    "ETM": "DPC IT-alert - Echo Top Map",
    "VMI": "Radar-DPC - riflettivita'",
}
UNIT_ALIASES = {
    "VIL": {"kg/m2", "kg/m²"},
    "ETM": {"m", "metre", "meter"},
    "POH": {"%", "percent", "percentage"},
    "VMI": {"dbz"},
}


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(
            obj,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    temp.replace(path)


def parse_time(value):
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("Timestamp non finito")
        return int(
            value * (1000 if abs(value) < 1e11 else 1)
        )
    if not isinstance(value, str):
        raise ValueError("Timestamp non valido")
    if value.isdigit():
        return parse_time(int(value))
    dt = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )
    if dt.tzinfo is None:
        raise ValueError("Timestamp senza timezone")
    return int(dt.timestamp() * 1000)


def iso(ms):
    return datetime.fromtimestamp(
        ms / 1000, timezone.utc
    ).isoformat()


def current_ms():
    return int(
        datetime.now(timezone.utc).timestamp() * 1000
    )


def last_product(name):
    response = requests.get(
        API + "findLastProductByType",
        params={"type": name},
        headers=HEADERS,
        timeout=(10, 20),
    )
    response.raise_for_status()
    body = response.json()
    items = body.get("lastProducts", [])
    if not isinstance(items, list):
        raise ValueError("Elenco prodotti non valido")
    times = [
        parse_time(item["time"])
        for item in items
        if isinstance(item, dict)
        and item.get("productType") == name
        and item.get("time") is not None
    ]
    if not times:
        raise ValueError("Prodotto non disponibile")
    return max(times)


def safe_download_url(url):
    if not isinstance(url, str):
        raise ValueError("URL download mancante")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise ValueError("URL download non sicuro")
    host = parsed.hostname.lower().rstrip(".")
    allowed = (
        host == "s3.amazonaws.com"
        or (
            host.startswith("s3.")
            and host.endswith(".amazonaws.com")
        )
        or host.endswith(".s3.amazonaws.com")
        or (
            ".s3." in host
            and host.endswith(".amazonaws.com")
        )
    )
    if not allowed:
        raise ValueError("Host S3 non autorizzato")
    for address in socket.getaddrinfo(
        host, 443, type=socket.SOCK_STREAM
    ):
        ip = ipaddress.ip_address(address[4][0])
        if not ip.is_global:
            raise ValueError("IP download non pubblico")
    return url


def get_tiff(name, timestamp):
    response = requests.post(
        API + "downloadProduct",
        json={
            "productType": name,
            "productDate": timestamp,
        },
        headers=HEADERS,
        timeout=(10, 25),
    )
    response.raise_for_status()
    info = response.json()
    url = safe_download_url(info.get("url", ""))
    chunks = []
    size = 0
    with requests.get(
        url,
        stream=True,
        timeout=(10, 45),
        allow_redirects=False,
    ) as download:
        if 300 <= download.status_code < 400:
            raise ValueError("Redirect non consentito")
        download.raise_for_status()
        for chunk in download.iter_content(1024 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > 50_000_000:
                raise ValueError("GeoTIFF troppo grande")
            chunks.append(chunk)
    content = b"".join(chunks)
    if len(content) < 8 or content[:4] not in (
        b"II*\x00", b"MM\x00*",
        b"II+\x00", b"MM\x00+",
    ):
        raise ValueError("Download non TIFF")
    return content


def normalize_unit(value):
    return re.sub(
        r"\s+", "", str(value or "").strip().lower()
    )


def metadata_unit(name, tags, band_tags):
    for metadata in (band_tags, tags):
        for key, value in metadata.items():
            if key.lower() in (
                "unit", "units", "unit_type",
                "unitofmeasure", "unit_of_measure",
            ):
                unit = normalize_unit(value)
                if unit in UNIT_ALIASES[name]:
                    return unit
    return None


def resolve_unit(name, tags, band_tags):
    found = metadata_unit(name, tags, band_tags)
    if name in DOCUMENTED_UNITS:
        if found is not None:
            return DOCUMENTED_UNITS[name], "metadata"
        return DOCUMENTED_UNITS[name], "documentazione DPC"
    if name == "POH" and found in (
        "%", "percent", "percentage"
    ):
        return "%", "metadata"
    return None, "non verificata"


def read_product(name, timestamp):
    content = get_tiff(name, timestamp)
    with MemoryFile(content) as memory:
        with memory.open() as ds:
            if ds.count != 1 or ds.crs is None:
                raise ValueError("Raster non supportato")
            if ds.width < 2 or ds.height < 2:
                raise ValueError("Dimensioni non valide")
            tags = ds.tags()
            band_tags = ds.tags(1)
            unit, provenance = resolve_unit(
                name, tags, band_tags
            )
            scale = float(ds.scales[0])
            offset = float(ds.offsets[0])
            if not (
                math.isfinite(scale)
                and math.isfinite(offset)
                and scale != 0
            ):
                raise ValueError("Scala raster non valida")
            raw = ds.read(1, masked=True)
            valid = ~np.ma.getmaskarray(raw)
            valid &= np.isfinite(raw.data)
            # Codici DPC: missing e assenza eco.
            valid &= raw.data != -9999
            valid &= raw.data != -9998
            values = (
                raw.data.astype(np.float64) * scale
                + offset
            )
            valid &= np.isfinite(values)
            project = Transformer.from_crs(
                "EPSG:4326", ds.crs,
                always_xy=True,
            )
            return {
                "values": values,
                "valid": valid,
                "transform": ds.transform,
                "project": project,
                "width": ds.width,
                "height": ds.height,
                "crs": str(ds.crs),
                "unit": unit,
                "unit_source": provenance,
                "scale": scale,
                "offset": offset,
                "nodata": (
                    str(ds.nodata)
                    if ds.nodata is not None else None
                ),
                "tags": {
                    str(k): str(v)[:200]
                    for k, v in tags.items()
                },
            }
            
def sample_cell(product, cell):
    lon = float(cell["lon"])
    lat = float(cell["lat"])
    x, y = product["project"].transform(lon, lat)
    col, row = (~product["transform"]) * (x, y)
    col, row = math.floor(col), math.floor(row)
    if (
        row < 0 or col < 0
        or row >= product["height"]
        or col >= product["width"]
    ):
        return None
    if not product["valid"][row, col]:
        return None
    value = float(product["values"][row, col])
    return round(value, 3) if math.isfinite(value) else None


def plausible(name, value, unit):
    if name == "VIL":
        return unit == "kg/m²" and 0 <= value <= 500
    if name == "ETM":
        return unit == "m" and 0 <= value <= 30000
    if name == "VMI":
        return unit == "dBZ" and -40 <= value <= 95
    if name == "POH":
        return unit == "%" and 0 <= value <= 100
    return False


def read_cells(report):
    try:
        payload = json.loads(
            SOURCE.read_text(encoding="utf-8")
        )
        original_time = payload["timestamp"]
        radar_time = parse_time(original_time)
        age = current_ms() - radar_time

        # Preserva la stringa originale.
        # Il frontend confronta esattamente
        # base_timestamp con il radar 2D.
        report["base_timestamp"] = original_time
        report["radar_age_minutes"] = round(
            age / 60000, 1
        )

        if not -300000 <= age <= MAX_AGE:
            report["radar_status"] = "stale"
            print("Radar principale obsoleto")
            return [], radar_time

        report["radar_status"] = "ok"
        valid_cells = []

        for cell in payload.get("cells", []):
            if not isinstance(cell, dict):
                continue
            if not cell.get("3d_available"):
                continue
            track = str(cell.get("track_id", ""))
            if not re.fullmatch(
                r"[A-Za-z0-9_-]+", track
            ):
                continue
            if "lat" not in cell or "lon" not in cell:
                continue
            report["cells"][track] = {}
            valid_cells.append(cell)

        return valid_cells, radar_time

    except Exception as exc:
        report["radar_status"] = "unavailable"
        report["radar_reason"] = str(exc)[:180]
        print("Radar non disponibile:", exc)
        return [], None


def main():
    report = {
        "status": "unavailable",
        "source": "Radar-DPC",
        "attribution": CREDIT,
        "source_url": SOURCE_URL,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "processing": (
            "Prodotti DPC campionati sui centroidi "
            "IRIDE, con controllo di unita', "
            "timestamp e valori mancanti"
        ),
        "generated_at": iso(current_ms()),
        "radar_status": "unavailable",
        "products": {},
        "cells": {},
    }

    cells, radar_time = read_cells(report)
    acquired = 0
    verified = 0

    for name in PRODUCTS:
        try:
            latest = last_product(name)
            age = current_ms() - latest

            if not -300000 <= age <= MAX_AGE:
                raise ValueError(
                    f"Prodotto obsoleto: "
                    f"{age / 60000:.1f} minuti"
                )

            product = read_product(name, latest)
            acquired += 1
            unit = product["unit"]

            synchronized = (
                radar_time is not None
                and report["radar_status"] == "ok"
                and abs(latest - radar_time)
                <= MAX_DIFFERENCE
            )

            status = (
                "ok" if unit
                else "unit_unverified"
            )

            report["products"][name] = {
                "status": status,
                "timestamp": iso(latest),
                "age_minutes": round(
                    age / 60000, 1
                ),
                "unit": unit,
                "unit_source": product["unit_source"],
                "crs": product["crs"],
                "scale": product["scale"],
                "offset": product["offset"],
                "nodata": product["nodata"],
                "metadata": product["tags"],
                "cell_synchronized": synchronized,
            }

            if unit:
                verified += 1

            if unit and synchronized:
                for cell in cells:
                    track = str(cell["track_id"])
                    value = sample_cell(
                        product, cell
                    )
                    if (
                        value is not None
                        and plausible(name, value, unit)
                    ):
                        report["cells"][track][name] = {
                            "value": value,
                            "unit": unit,
                            "timestamp": iso(latest),
                        }

            print(
                f"{name}: {status}; "
                f"unita={unit}; "
                f"sincronizzato={synchronized}"
            )

        except Exception as exc:
            report["products"][name] = {
                "status": "unavailable",
                "reason": str(exc)[:180],
            }
            print(f"{name}: {exc}")

    report["status"] = (
        "ok" if verified == len(PRODUCTS)
        else "partial" if verified
        else "unit_unverified" if acquired
        else "unavailable"
    )

    write_json(DEST, report)

    print(
        f"Diagnostica: {acquired}/4 acquisiti; "
        f"{verified}/4 unita' verificate; "
        f"{len(cells)} celle."
    )
    print("Report:", DEST)


if __name__ == "__main__":
    main()
