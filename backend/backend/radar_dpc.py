"""Radar-DPC VMI adapter. Data: CC BY-SA 4.0; see official documentation."""
from datetime import datetime, timezone
import re
from urllib.parse import urlparse

import numpy as np
import rasterio
import requests
from pyproj import Transformer

API = "https://radar-api.protezionecivile.it"
LICENSE = "https://creativecommons.org/licenses/by-sa/4.0/"
HEADERS = {
    "Origin": "https://19francomenenti73-dev.github.io",
    "User-Agent": "IRIDE-open-radar/4.0",
}


def decode_vmi(path):
    with rasterio.open(path) as dataset:
        transform = dataset.transform
        if (dataset.count != 1 or dataset.crs is None
                or not dataset.crs.is_projected
                or dataset.crs.linear_units != "metre"
                or transform.a <= 0 or transform.e >= 0
                or transform.b != 0 or transform.d != 0):
            raise ValueError("Georeferenziazione VMI non supportata")
        radar = dataset.read(1, masked=True).astype(np.float32).filled(np.nan)
        radar = radar * dataset.scales[0] + dataset.offsets[0]
        radar[(radar < -32) | (radar > 100)] = np.nan
        if not np.isfinite(radar).any():
            raise ValueError("VMI privo di dati validi")
        grid = {
            "xscale_m": transform.a, "yscale_m": -transform.e,
            "xsize": dataset.width, "ysize": dataset.height,
            "projection": dataset.crs.to_wkt(),
        }
        origin = (transform.c, transform.f + transform.e * dataset.height)
        inverse = Transformer.from_crs(dataset.crs, "EPSG:4326", always_xy=True)
        document = dataset.tags().get("TIFFTAG_DOCUMENTNAME", "")
        match = re.search(r"VMI_(\d{2}-\d{2}-\d{4}_\d{2}-\d{2})", document)
        product_time = (datetime.strptime(match.group(1), "%d-%m-%Y_%H-%M")
                        .replace(tzinfo=timezone.utc) if match else None)
        return radar, grid, inverse, origin, product_time


def acquire(folder):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "dpc_latest.tif"
    try:
        response = requests.get(
            API + "/findLastProductByType", params={"type": "VMI"},
            headers=HEADERS, timeout=(10, 30))
        response.raise_for_status()
        products = [p for p in response.json().get("lastProducts", [])
                    if p.get("productType") == "VMI"]
        milliseconds = max(int(p["time"]) for p in products)
        timestamp = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
        age = (datetime.now(timezone.utc) - timestamp).total_seconds() / 60
        if not -5 <= age <= 90:
            raise ValueError(f"VMI DPC obsoleto: {age:.1f} minuti")
        response = requests.post(
            API + "/downloadProduct",
            json={"productType": "VMI", "productDate": milliseconds},
            headers=HEADERS, timeout=(10, 30))
        response.raise_for_status()
        download = response.json()
        url = download["url"]
        parsed = urlparse(url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".amazonaws.com"):
            raise ValueError("URL download DPC non valido")
        total = 0
        with requests.get(url, stream=True, timeout=(10, 45)) as response:
            response.raise_for_status()
            with path.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    total += len(chunk)
                    if total > 100_000_000:
                        raise ValueError("GeoTIFF DPC troppo grande")
                    output.write(chunk)
        radar, grid, inverse, origin, decoded = decode_vmi(path)
        if decoded is None or abs((decoded - timestamp).total_seconds()) > 120:
            raise ValueError("Timestamp VMI interno assente o incoerente")
        source = {
            "provider": "Radar-DPC", "archive": "Radar-DPC REST API",
            "url": "https://radar.protezionecivile.it/",
            "object_key": download.get("key", ""),
            "product": "VMI — massimo di riflettività sulla verticale",
            "license": "CC BY-SA 4.0", "license_url": LICENSE,
            "attribution": "Radar-DPC · CC BY-SA 4.0 · elaborazione I.R.I.D.E.",
            "derived_data_license": LICENSE,
        }
        return radar, grid, inverse, origin, timestamp, source
    finally:
        path.unlink(missing_ok=True)
