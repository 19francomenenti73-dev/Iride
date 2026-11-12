
#!/usr/bin/env python3
"""I.R.I.D.E. V13 - CAPPI reali Radar-DPC."""
import concurrent.futures as cf
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import requests
from pyproj import Transformer
from rasterio.io import MemoryFile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/data/dpc3d"
API = "https://radar-api.protezionecivile.it/"
HEADERS = {"Origin": "https://19francomenenti73-dev.github.io"}
SIDE = 32
PRODUCTS = [f"CAPPI_{i}" for i in range(1, 11)]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(value, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )
    temp.replace(path)


def milliseconds(value):
    return int(
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        .timestamp() * 1000
    )


def iso(value):
    return datetime.fromtimestamp(
        value / 1000, timezone.utc
    ).isoformat()


def install():
    path = ROOT / "index.html"
    html = path.read_text(encoding="utf-8")
    bridge = (
        "window.dispatchEvent(new CustomEvent('iride:cell',"
        "{detail:{cell,timestamp:currentData?.timestamp}}));"
    )
    if bridge not in html:
        target = "  activeCell=cell;"
        if html.count(target) != 1:
            raise RuntimeError("Collegamento 3D non trovato")
        html = html.replace(
            target, target + "\n  " + bridge
        )
    tag = '<script src="dpc3d.js?v=1"></script>'
    if tag not in html:
        if html.count("</body>") != 1:
            raise RuntimeError("Chiusura HTML non trovata")
        html = html.replace(
            "</body>", tag + "\n</body>"
        )
    path.write_text(html, encoding="utf-8")
    print("Collegamento CAPPI installato")


def latest(product):
    response = requests.get(
        API + "findLastProductByType",
        params={"type": product},
        headers=HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    return max(
        int(item["time"])
        for item in response.json()["lastProducts"]
        if item["productType"] == product
    )


def download(product, when):
    response = requests.post(
        API + "downloadProduct",
        headers=HEADERS,
        json={"productType": product, "productDate": when},
        timeout=25,
    )
    response.raise_for_status()
    url = response.json()["url"]
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host.endswith(
        ".amazonaws.com"
    ):
        raise ValueError("URL DPC inatteso")

    chunks = []
    size = 0
    with requests.get(
        url, stream=True, timeout=30
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_content(1024 * 1024):
            size += len(chunk)
            if size > 40_000_000:
                raise ValueError("CAPPI troppo grande")
            chunks.append(chunk)

    with MemoryFile(b"".join(chunks)) as memory:
        with memory.open() as ds:
            name = ds.tags().get(
                "TIFFTAG_DOCUMENTNAME", ""
            )
            match = re.search(
                re.escape(product)
                + r"_(\d{2}-\d{2}-\d{4}_\d{2}-\d{2})",
                name,
            )
            if not match:
                raise ValueError(
                    "Timestamp CAPPI assente"
                )
            internal = datetime.strptime(
                match[1], "%d-%m-%Y_%H-%M"
            ).replace(tzinfo=timezone.utc)
            if int(internal.timestamp() * 1000) != when:
                raise ValueError(
                    "Timestamp CAPPI incoerente"
                )

            tr = ds.transform
            if (
                ds.count != 1
                or not ds.crs
                or not ds.crs.is_projected
                or ds.crs.linear_units != "metre"
                or tr.a <= 0
                or tr.e >= 0
                or tr.b != 0
                or tr.d != 0
            ):
                raise ValueError(
                    "Griglia CAPPI non supportata"
                )

            raw = ds.read(1)
            values = (
                raw * ds.scales[0] + ds.offsets[0]
            )
            packed = np.zeros(
                raw.shape, dtype=np.uint8
            )

            # 0 mancante, 1 senza eco,
            # 2..246 riflettivita' quantizzata.
            packed[raw == -9998] = 1
            valid = (
                (ds.read_masks(1) > 0)
                & np.isfinite(values)
                & (values >= -32)
                & (values <= 90)
                & (raw != -9998)
            )
            packed[valid] = np.rint(
                (values[valid] + 32) * 2 + 2
            ).astype(np.uint8)
            return packed, tr, ds.crs


def reduce_tile(array, x0, y0, size):
    """Massimo valido, senza confondere missing ed eco."""
    h, w = array.shape
    tile = np.zeros((SIDE, SIDE), dtype=np.uint8)
    for y in range(SIDE):
        a = y0 + y * size // SIDE
        b = y0 + (y + 1) * size // SIDE
        for x in range(SIDE):
            c = x0 + x * size // SIDE
            d = x0 + (x + 1) * size // SIDE
            ya, yb = max(0, a), min(h, b)
            xa, xb = max(0, c), min(w, d)
            if ya >= yb or xa >= xb:
                continue
            block = array[ya:yb, xa:xb]
            echoes = block[block >= 2]
            if echoes.size:
                tile[y, x] = echoes.max()
            elif np.any(block == 1):
                tile[y, x] = 1
    return tile


def cell_volume(cell, layers, grid):
    first, tr, crs = layers[0]
    forward = Transformer.from_crs(
        "EPSG:4326", crs, always_xy=True
    )
    cx, cy = forward.transform(
        cell["lon"], cell["lat"]
    )
    col, row = (~tr) * (cx, cy)
    span_m = (
        cell["matrix_sampling"]["context_pixels"]
        * abs(grid["xscale_m"])
    )
    size = max(
        SIDE, int(round(span_m / tr.a))
    )
    x0 = int(round(col - 0.5)) - size // 2
    y0 = int(round(row - 0.5)) - size // 2

    cube = np.stack([
        reduce_tile(array, x0, y0, size)
        for array, _, _ in layers
    ])
    if not np.any(cube >= 96):
        return None

    # Il pavimento e' il massimo dei CAPPI,
    # non il prodotto VMI.
    floor = cube.max(axis=0)
    return {
        "side": SIDE,
        "width_km": round(size * tr.a / 1000, 3),
        "altitudes_m": list(
            range(1000, 10001, 1000)
        ),
        "layers": cube.tolist(),
        "floor": floor.tolist(),
        "encoding": (
            "0 missing; 1 no echo; "
            "dBZ=(code-2)/2-32"
        ),
        "sampling": (
            "maximum valid reflectivity within "
            "horizontal bins; rows north to south"
        ),
        "floor_product": (
            "maximum of CAPPI 1-10 km, not VMI"
        ),
        "lat": cell["lat"],
        "lon": cell["lon"],
    }


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = {
        "status": "unavailable",
        "cells": {},
    }
    write(OUT / "manifest.json", manifest)
    try:
        payload = json.loads(
            (ROOT / "data/iride.json").read_text(
                encoding="utf-8"
            )
        )
        scan = milliseconds(payload["timestamp"])
        age = (
            datetime.now(timezone.utc).timestamp()
            * 1000 - scan
        )
        if not -300_000 <= age <= 5_400_000:
            raise ValueError("Radar 2D obsoleto")

        with cf.ThreadPoolExecutor(
            max_workers=4
        ) as pool:
            times = list(
                pool.map(latest, PRODUCTS)
            )
        when = (
            min(scan, min(times))
            // 300_000 * 300_000
        )
        if scan - when > 600_000:
            raise ValueError(
                "CAPPI oltre 10 minuti dal radar 2D"
            )

        with cf.ThreadPoolExecutor(
            max_workers=4
        ) as pool:
            layers = list(pool.map(
                lambda p: download(p, when),
                PRODUCTS,
            ))

        first, tr, crs = layers[0]
        for array, transform, projection in layers:
            if (
                array.shape != first.shape
                or transform != tr
                or projection != crs
            ):
                raise ValueError(
                    "Griglie CAPPI non coerenti"
                )

        manifest.update(
            status="ok",
            base_timestamp=payload["timestamp"],
            timestamp=iso(when),
            license="CC-BY-SA-4.0",
            source="Radar-DPC",
            temporal_offset_seconds=(
                scan - when
            ) // 1000,
        )

        for cell in payload["cells"]:
            if not cell.get("3d_available"):
                continue
            result = cell_volume(
                cell, layers, payload["grid"]
            )
            if result is None:
                continue
            track = cell["track_id"]
            if not re.fullmatch(
                r"[A-Za-z0-9_-]+", track
            ):
                raise ValueError(
                    "Identificativo cella non valido"
                )
            filename = f"{when}_{track}.json"
            result.update(
                timestamp=iso(when),
                base_timestamp=payload["timestamp"],
                track_id=track,
                source="Radar-DPC",
                license="CC-BY-SA-4.0",
            )
            write(OUT / filename, result)
            manifest["cells"][track] = filename

        keep = set(
            manifest["cells"].values()
        ) | {"manifest.json"}
        for path in OUT.glob("*.json"):
            if path.name not in keep:
                path.unlink()
        print(
            f"CAPPI: {len(manifest['cells'])} "
            f"celle, {iso(when)}"
        )
    except Exception as exc:
        manifest = {
            "status": "unavailable",
            "cells": {},
            "reason": str(exc),
        }
        print(
            f"CAPPI non disponibili; "
            f"riserva 2D: {exc}"
        )
    write(OUT / "manifest.json", manifest)


if __name__ == "__main__":
    if "--install" in sys.argv:
        install()
    else:
        build()
        
