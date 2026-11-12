
#!/usr/bin/env python3
# I.R.I.D.E. — Diagnostica Radar-DPC
# Fonte: Dipartimento della Protezione Civile
# Dati Radar-DPC — CC BY-SA 4.0
# https://radar.protezionecivile.it/
# https://dpc-radar.readthedocs.io/
#
# Conservare attribuzione, licenza e timestamp.
# Non altera CAPPI, radar 2D o renderer 3D.

import json
import math
import re
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
ORIGIN = "https://19francomenenti73-dev.github.io"
HEADERS = {
    "Origin": ORIGIN,
    "Accept": "application/json",
}
PRODUCTS = ("VIL", "ETM", "POH", "VMI")

LICENSE = "CC BY-SA 4.0"
CREDIT = "Dipartimento della Protezione Civile — Radar-DPC"
SOURCE_URL = "https://radar.protezionecivile.it/"
LICENSE_URL = (
    "https://creativecommons.org/licenses/by-sa/4.0/"
)

# Le unità vengono accettate solo se dichiarate
# esplicitamente e riconosciute nei metadati.
UNITS = {
    "VIL": {
        "kg/m2": "kg/m²",
        "kg/m²": "kg/m²",
        "mm": "mm",
    },
    "ETM": {
        "m": "m",
        "km": "km",
    },
    "POH": {
        "%": "%",
        "percent": "%",
    },
    "VMI": {
        "dbz": "dBZ",
    },
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
    if not isinstance(value, str):
        raise ValueError("Timestamp radar non valido")
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


def last_product(name):
    response = requests.get(
        API + "findLastProductByType",
        params={"type": name},
        headers=HEADERS,
        timeout=(10, 20),
    )
    response.raise_for_status()
    items = response.json().get("lastProducts", [])
    times = [
        int(item["time"])
        for item in items
        if item.get("productType") == name
    ]
    if not times:
        raise ValueError("Prodotto non disponibile")
    return max(times)


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
    url = info.get("url", "")
    parsed = urlparse(url)

    # Accettiamo esclusivamente il bucket DPC.
    if (
        parsed.scheme != "https"
        or parsed.hostname !=
        "dpc-radar.s3.eu-south-1.amazonaws.com"
        or not parsed.path.startswith("/" + name + "/")
    ):
        raise ValueError("URL download non riconosciuto")

    chunks = []
    size = 0

    with requests.get(
        url,
        stream=True,
        timeout=(10, 45),
    ) as download:
        download.raise_for_status()
        for chunk in download.iter_content(1024 * 1024):
            if not chunk:
                continue
            size += len(chunk)
            if size > 50_000_000:
                raise ValueError("GeoTIFF troppo grande")
            chunks.append(chunk)

    return b"".join(chunks)


def normalize_unit(value):
    return re.sub(
        r"\s+", "",
        str(value or "").strip().lower()
    )


def identify_unit(name, tags, band_tags):
    # Nessuna deduzione arbitraria dal prodotto.
    for metadata in (band_tags, tags):
        for key, value in metadata.items():
            if key.lower() in (
                "unit", "units", "unit_type",
                "unitofmeasure", "unit_of_measure",
            ):
                normalized = normalize_unit(value)
                unit = UNITS[name].get(normalized)
                if unit:
                    return unit
    return None


def read_product(name, timestamp):
    content = get_tiff(name, timestamp)

    with MemoryFile(content) as memory:
        with memory.open() as dataset:
            if dataset.count != 1:
                raise ValueError("GeoTIFF multibanda")
            if dataset.crs is None:
                raise ValueError("CRS assente")
            if dataset.width < 2 or dataset.height < 2:
                raise ValueError("Dimensioni non valide")

            tags = dataset.tags()
            band_tags = dataset.tags(1)
            unit = identify_unit(name, tags, band_tags)

            scale = dataset.scales[0]
            offset = dataset.offsets[0]

            if not (
                math.isfinite(scale)
                and math.isfinite(offset)
                and scale != 0
            ):
                raise ValueError("Scala non valida")

            raw = dataset.read(1, masked=True)
            valid = ~np.ma.getmaskarray(raw)
            valid &= np.isfinite(raw.data)

            values = (
                raw.data.astype(np.float64) * scale
                + offset
            )
            valid &= np.isfinite(values)

            # Trasformazione geografica per il
            # campionamento delle celle.
            transform = Transformer.from_crs(
                "EPSG:4326",
                dataset.crs,
                always_xy=True,
            )

            return {
                "values": values,
                "valid": valid,
                "transform": dataset.transform,
                "project": transform,
                "width": dataset.width,
                "height": dataset.height,
                "crs": str(dataset.crs),
                "unit": unit,
                "scale": float(scale),
                "offset": float(offset),
                "nodata": (
                    str(dataset.nodata)
                    if dataset.nodata is not None
                    else None
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

    col = math.floor(col)
    row = math.floor(row)

    if (
        row < 0 or col < 0
        or row >= product["height"]
        or col >= product["width"]
    ):
        return None

    if not product["valid"][row, col]:
        return None

    value = float(product["values"][row, col])
    if not math.isfinite(value):
        return None

    return round(value, 3)


def plausible(name, value, unit):
    # Controlli fisici conservativi, non
    # sostituiscono la documentazione del prodotto.
    if name == "POH":
        return unit == "%" and 0 <= value <= 100
    if name == "VIL":
        return unit in ("kg/m²", "mm") and 0 <= value <= 500
    if name == "ETM":
        if unit == "m":
            return 0 <= value <= 30000
        if unit == "km":
            return 0 <= value <= 30
        return False
    if name == "VMI":
        return unit == "dBZ" and -40 <= value <= 95
    return False


def main():
    report = {
        "status": "unavailable",
        "source": "Radar-DPC",
        "attribution": CREDIT,
        "source_url": SOURCE_URL,
        "license": LICENSE,
        "license_url": LICENSE_URL,
        "products": {},
        "cells": {},
    }

    try:
        payload = json.loads(
            SOURCE.read_text(encoding="utf-8")
        )
        radar_time = parse_time(payload["timestamp"])
        now = int(datetime.now(timezone.utc).timestamp() * 1000)

        if not -300000 <= now - radar_time <= 5400000:
            raise ValueError("Radar principale obsoleto")

        report["base_timestamp"] = payload["timestamp"]
        cells = [
            c for c in payload.get("cells", [])
            if c.get("3d_available")
        ]

        for cell in cells:
            track = str(cell.get("track_id", ""))
            if re.fullmatch(r"[A-Za-z0-9_-]+", track):
                report["cells"][track] = {}

        for name in PRODUCTS:
            try:
                latest = last_product(name)

                # Non associamo prodotti troppo distanti
                # temporalmente dal radar principale.
                if abs(latest - radar_time) > 600000:
                    raise ValueError(
                        "Prodotto non sincronizzato"
                    )

                product = read_product(name, latest)
                unit = product["unit"]

                report["products"][name] = {
                    "status": (
                        "ok" if unit
                        else "unit_unverified"
                    ),
                    "timestamp": iso(latest),
                    "unit": unit,
                    "crs": product["crs"],
                    "scale": product["scale"],
                    "offset": product["offset"],
                    "nodata": product["nodata"],
                    "metadata": product["tags"],
                }

                if unit:
                    for cell in cells:
                        track = str(cell.get("track_id", ""))
                        if track not in report["cells"]:
                            continue

                        value = sample_cell(product, cell)
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
                    f"{name}: acquisito, "
                    f"unità={unit or 'non verificata'}"
                )

            except Exception as exc:
                report["products"][name] = {
                    "status": "unavailable",
                    "reason": str(exc)[:180],
                }
                print(f"{name}: {exc}")

        report["status"] = (
            "ok" if any(
                item.get("status") == "ok"
                for item in report["products"].values()
            )
            else "unavailable"
        )

    except Exception as exc:
        report["reason"] = str(exc)[:180]
        print("Diagnostica:", exc)

    write_json(DEST, report)
    print("Report:", DEST)


if __name__ == "__main__":
    main()
      
