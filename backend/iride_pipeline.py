#!/usr/bin/env python3
"""I.R.I.D.E. — OPERA DBZH detection and contextual radar matrices."""

from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET

import h5py
import numpy as np
import requests

from pyproj import CRS, Transformer
from scipy import ndimage


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
HIST = DATA / "iride_history.json"

S3 = "https://s3.waw3-1.cloudferro.com"
BUCKET = "openradar-24h"
PREFIX = "OPERA/COMP"

TIMEOUT = 35
MAX_BYTES = 250_000_000

THRESHOLD = float(__import__("os").getenv("MIN_DBZ", "35"))
MIN_PIXELS = int(__import__("os").getenv("MIN_CELL_PIXELS", "8"))

MAX_CELLS = 100

MATRIX_SIDE = 72
MATRIX_FLOOR_DBZ = 8.0

CONTEXT_FACTOR = 2.4
MIN_CONTEXT_PIXELS = 48
MAX_CONTEXT_PIXELS = 360

UA = "IRIDE-experimental-nowcasting/2.0"


def now():
    return datetime.now(timezone.utc)


def stamp(d):
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def attr(group, key, default=None):
    if group is None or key not in group.attrs:
        return default

    value = group.attrs[key]

    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")

    if isinstance(value, np.ndarray) and value.shape == ():
        value = value.item()

    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")

    return value


def number(value, default=None):
    try:
        x = float(np.asarray(value).item())
        return x if np.isfinite(x) else default
    except Exception:
        return default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)

    tmp = path.with_suffix(path.suffix + ".tmp")

    tmp.write_text(
        json.dumps(
            obj,
            ensure_ascii=False,
            separators=(",", ":")
        ) + "\n",
        encoding="utf-8"
    )

    tmp.replace(path)


# ---------------------------------------------------------
# ACQUISIZIONE OPERA
# ---------------------------------------------------------

def list_day(day):

    prefix = f"{day:%Y/%m/%d}/{PREFIX}/"

    response = requests.get(
        f"{S3}/{BUCKET}/",
        params={
            "list-type": "2",
            "prefix": prefix
        },
        timeout=TIMEOUT,
        headers={"User-Agent": UA}
    )

    response.raise_for_status()

    root = ET.fromstring(response.content)

    ns = {
        "s": "http://s3.amazonaws.com/doc/2006-03-01/"
    }

    results = []

    for item in root.findall("s:Contents", ns):

        key = item.findtext(
            "s:Key",
            default="",
            namespaces=ns
        )

        size = int(
            item.findtext(
                "s:Size",
                default="0",
                namespaces=ns
            )
        )

        if key.endswith("@0@DBZH.h5") and 0 < size <= MAX_BYTES:
            results.append((key, size))

    return results


def latest():

    current = now()
    items = []

    for offset in range(3):

        day = (current - timedelta(days=offset)).date()

        try:
            items.extend(
                list_day(
                    datetime(
                        day.year,
                        day.month,
                        day.day,
                        tzinfo=timezone.utc
                    )
                )
            )

        except Exception as error:
            print(
                "Listing",
                day,
                error,
                file=sys.stderr
            )

    if not items:
        raise RuntimeError(
            "Nessun OPERA DBZH trovato negli ultimi tre giorni UTC."
        )

    def keytime(item):

        match = re.search(
            r"OPERA@(\d{8}T\d{4})@0@DBZH\.h5$",
            item[0]
        )

        return match.group(1) if match else item[0]

    return max(items, key=keytime)


def fetch(key, size):

    path = DATA / "raw" / "opera_latest.h5"
    path.parent.mkdir(parents=True, exist_ok=True)

    url = (
        f"{S3}/{BUCKET}/"
        + quote(key, safe="/@")
    )

    total = 0

    with requests.get(
        url,
        stream=True,
        timeout=TIMEOUT,
        headers={"User-Agent": UA}
    ) as response:

        response.raise_for_status()

        with path.open("wb") as file:

            for chunk in response.iter_content(1024 * 1024):

                if not chunk:
                    continue

                total += len(chunk)

                if total > MAX_BYTES:
                    raise RuntimeError(
                        "Download oltre il limite di sicurezza."
                    )

                file.write(chunk)

    if size and total != size:
        raise RuntimeError(
            f"Download incompleto: {total}/{size} byte."
        )

    return path, url


# ---------------------------------------------------------
# DECODIFICA HDF5 ODIM
# ---------------------------------------------------------

def decode(path):

    with h5py.File(path, "r") as h:

        if "dataset1/data1/data" not in h:
            raise RuntimeError(
                "Dataset /dataset1/data1/data assente."
            )

        raw = h["dataset1/data1/data"][()]

        parameter = h.get("dataset1/data1/what")

        quantity = str(
            attr(parameter, "quantity", "")
        )

        if quantity.upper() != "DBZH":
            raise RuntimeError(
                f"quantity non è DBZH: {quantity!r}"
            )

        gain = number(attr(parameter, "gain"), 1)
        offset = number(attr(parameter, "offset"), 0)

        nodata = number(attr(parameter, "nodata"))
        undetect = number(attr(parameter, "undetect"))

        z = raw.astype(np.float32) * gain + offset

        invalid = ~np.isfinite(z)

        if nodata is not None:
            invalid |= raw == nodata

        if undetect is not None:
            invalid |= raw == undetect

        z[invalid] = np.nan

        where = h.get("where")

        if where is None:
            raise RuntimeError(
                "Gruppo /where assente."
            )

        projection = attr(where, "projdef")

        xscale = number(attr(where, "xscale"))
        yscale = number(attr(where, "yscale"))

        nx = int(
            number(
                attr(where, "xsize"),
                z.shape[1]
            )
        )

        ny = int(
            number(
                attr(where, "ysize"),
                z.shape[0]
            )
        )

        if (
            not projection
            or not xscale
            or not yscale
            or z.shape != (ny, nx)
        ):
            raise RuntimeError(
                "Metadati griglia incompleti o dimensioni incoerenti."
            )

        llon = number(attr(where, "LL_lon"))
        llat = number(attr(where, "LL_lat"))

        if llon is None or llat is None:
            raise RuntimeError(
                "LL_lon/LL_lat assenti."
            )

        crs = CRS.from_user_input(str(projection))

        forward = Transformer.from_crs(
            4326,
            crs,
            always_xy=True
        )

        inverse = Transformer.from_crs(
            crs,
            4326,
            always_xy=True
        )

        ex, ey = forward.transform(llon, llat)

        xo = ex + xscale / 2
        yo = ey + yscale / 2

        errors = []

        corners = (
            ("LL_lon", "LL_lat", 0, ny - 1),
            ("LR_lon", "LR_lat", nx - 1, ny - 1),
            ("UL_lon", "UL_lat", 0, 0),
            ("UR_lon", "UR_lat", nx - 1, 0)
        )

        for lon_key, lat_key, col, row in corners:

            lon = number(attr(where, lon_key))
            lat = number(attr(where, lat_key))

            if lon is None or lat is None:
                continue

            px, py = forward.transform(lon, lat)

            gx = xo + col * xscale - xscale / 2

            gy = (
                yo
                + (ny - 1 - row) * yscale
                - yscale / 2
            )

            errors.append(
                float(np.hypot(px - gx, py - gy))
            )

        if errors and max(errors) > max(
            abs(xscale),
            abs(yscale)
        ) * 3:

            raise RuntimeError(
                "Controllo georeferenziazione fallito."
            )

        product = h.get("dataset1/what")

        end_date = attr(product, "enddate")
        end_time = attr(product, "endtime")

        product_time = None

        if end_date and end_time:

            product_time = datetime.strptime(
                str(end_date) + str(end_time),
                "%Y%m%d%H%M%S"
            ).replace(tzinfo=timezone.utc)

        grid = {
            "projection": str(projection),
            "xsize": nx,
            "ysize": ny,
            "xscale_m": xscale,
            "yscale_m": yscale,
            "corner_check_max_error_m": (
                round(max(errors), 2)
                if errors else None
            ),
            "license": str(
                attr(h.get("how"), "license", "")
            )
        }

        return z, grid, inverse, (xo, yo), product_time


# ---------------------------------------------------------
# GEOMETRIA E DISTANZE
# ---------------------------------------------------------

def haversine(a, b):

    from math import radians, sin, cos, asin, sqrt

    p1 = radians(a["lat"])
    p2 = radians(b["lat"])

    dp = radians(b["lat"] - a["lat"])
    dl = radians(b["lon"] - a["lon"])

    q = (
        sin(dp / 2) ** 2
        + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    )

    return 6371 * 2 * asin(min(1, sqrt(q)))


def history_read():

    try:
        return json.loads(
            HIST.read_text(encoding="utf-8")
        )

    except Exception:
        return {}


# ---------------------------------------------------------
# NUOVA MATRICE RADAR CONTESTUALE 72x72
# ---------------------------------------------------------

def contextual_matrix(
    radar,
    component_slice,
    centroid_x,
    centroid_y
):

    ys, xs = component_slice

    cell_width = xs.stop - xs.start
    cell_height = ys.stop - ys.start

    largest_dimension = max(
        cell_width,
        cell_height
    )

    context_size = int(
        np.ceil(
            largest_dimension * CONTEXT_FACTOR
        )
    )

    context_size = max(
        MIN_CONTEXT_PIXELS,
        context_size
    )

    context_size = min(
        MAX_CONTEXT_PIXELS,
        context_size
    )

    half = context_size // 2

    x0 = int(round(centroid_x)) - half
    y0 = int(round(centroid_y)) - half

    x1 = x0 + context_size
    y1 = y0 + context_size

    patch = np.full(
        (context_size, context_size),
        np.nan,
        dtype=np.float32
    )

    source_x0 = max(0, x0)
    source_y0 = max(0, y0)

    source_x1 = min(radar.shape[1], x1)
    source_y1 = min(radar.shape[0], y1)

    dest_x0 = source_x0 - x0
    dest_y0 = source_y0 - y0

    dest_x1 = dest_x0 + (
        source_x1 - source_x0
    )

    dest_y1 = dest_y0 + (
        source_y1 - source_y0
    )

    patch[
        dest_y0:dest_y1,
        dest_x0:dest_x1
    ] = radar[
        source_y0:source_y1,
        source_x0:source_x1
    ]

    patch[
        ~np.isfinite(patch)
        | (patch < MATRIX_FLOOR_DBZ)
    ] = 0

    output = np.zeros(
        (MATRIX_SIDE, MATRIX_SIDE),
        dtype=np.float32
    )

    for oy in range(MATRIX_SIDE):

        py0 = int(
            oy * context_size / MATRIX_SIDE
        )

        py1 = int(
            (oy + 1) * context_size / MATRIX_SIDE
        )

        py1 = max(py0 + 1, py1)

        for ox in range(MATRIX_SIDE):

            px0 = int(
                ox * context_size / MATRIX_SIDE
            )

            px1 = int(
                (ox + 1) * context_size / MATRIX_SIDE
            )

            px1 = max(px0 + 1, px1)

            block = patch[
                py0:py1,
                px0:px1
            ]

            valid = block[
                np.isfinite(block)
                & (block >= MATRIX_FLOOR_DBZ)
            ]

            if valid.size:
                output[oy, ox] = float(
                    np.max(valid)
                )

    return (
        np.round(output, 1).tolist(),
        {
            "matrix_rows": MATRIX_SIDE,
            "matrix_cols": MATRIX_SIDE,
            "context_pixels": context_size,
            "context_floor_dbz": MATRIX_FLOOR_DBZ,
            "context_factor": CONTEXT_FACTOR,
            "pixel_origin_x": x0,
            "pixel_origin_y": y0,
            "pixel_end_x": x1,
            "pixel_end_y": y1,
            "aggregation": "maximum reflectivity per matrix sample",
            "contains_weak_echo": True
        }
    )# ---------------------------------------------------------
# RILEVAMENTO CELLE E PERIMETRI
# ---------------------------------------------------------

def detect(
    radar,
    grid,
    inverse,
    origin,
    timestamp,
    history
):

    labels, _ = ndimage.label(
        np.isfinite(radar)
        & (radar >= THRESHOLD),
        structure=np.ones(
            (3, 3),
            dtype=np.uint8
        )
    )

    objects = ndimage.find_objects(labels)

    cells = []

    xo, yo = origin

    for label_id, sl in enumerate(objects, 1):

        if sl is None:
            continue

        ys, xs = sl

        component = (
            labels[sl] == label_id
        )

        area = int(component.sum())

        if area < MIN_PIXELS:
            continue

        patch = radar[sl]

        yy, xx = np.where(component)

        values = patch[yy, xx]

        weights = np.maximum(
            values - THRESHOLD,
            0.1
        )

        cx = float(
            xs.start
            + np.average(xx, weights=weights)
        )

        cy = float(
            ys.start
            + np.average(yy, weights=weights)
        )

        gx = xo + cx * grid["xscale_m"]

        gy = (
            yo
            + (grid["ysize"] - 1 - cy)
            * grid["yscale_m"]
        )

        lon, lat = inverse.transform(gx, gy)

        matrix, matrix_info = contextual_matrix(
            radar,
            sl,
            cx,
            cy
        )

        edge = component & ~ndimage.binary_erosion(
            component,
            structure=np.ones((3, 3)),
            border_value=0
        )

        by, bx = np.where(edge)

        if len(bx) > 80:

            take = np.linspace(
                0,
                len(bx) - 1,
                80
            ).astype(int)

            bx = bx[take]
            by = by[take]

        perimeter = []

        for py, px in zip(by, bx):

            x = (
                xo
                + (xs.start + int(px))
                * grid["xscale_m"]
            )

            y = (
                yo
                + (
                    grid["ysize"]
                    - 1
                    - (ys.start + int(py))
                )
                * grid["yscale_m"]
            )

            lo, la = inverse.transform(x, y)

            perimeter.append([
                round(float(la), 4),
                round(float(lo), 4)
            ])

        cells.append({

            "id": f"OPERA-{label_id:05d}",

            "lat": round(float(lat), 4),
            "lon": round(float(lon), 4),

            "max_dbz": round(
                float(values.max()),
                1
            ),

            "mean_dbz": round(
                float(values.mean()),
                1
            ),

            "threshold_dbz": THRESHOLD,

            "area_pixels": area,

            "approx_area_km2": round(
                area
                * abs(
                    grid["xscale_m"]
                    * grid["yscale_m"]
                )
                / 1e6,
                1
            ),

            "bbox_pixels": [
                xs.start,
                ys.start,
                xs.stop,
                ys.stop
            ],

            "perimeter": perimeter,

            "grid_matrix": matrix,

            "matrix_sampling": matrix_info,

            "matrix_semantics": {
                "kind": "contextual_2d_radar_reflectivity",
                "units": "dBZ",
                "floor_dbz": MATRIX_FLOOR_DBZ,
                "cell_detection_threshold_dbz": THRESHOLD,
                "weak_echo_preserved": True,
                "vertical_information": False
            },

            "classification": "unclassified_radar_echo",

            "classification_note":
                "Il composito DBZH 2D non consente "
                "di diagnosticare da solo le classi convettive.",

            "severity": {
                "vil": None,
                "echo_top": None,
                "rain_mmh": None
            }

        })

    cells.sort(
        key=lambda c: (
            c["max_dbz"],
            c["area_pixels"]
        ),
        reverse=True
    )

    cells = cells[:MAX_CELLS]

    # -----------------------------------------------------
    # TRACCIAMENTO: logica invariata
    # -----------------------------------------------------

    used = set()

    for cell in cells:

        best = None
        distance = 80

        for track_id, track in history.items():

            observations = track.get(
                "observations",
                []
            )

            if not observations or track_id in used:
                continue

            try:

                last = observations[-1]

                age = (
                    timestamp
                    - datetime.fromisoformat(
                        last["timestamp"].replace(
                            "Z",
                            "+00:00"
                        )
                    )
                ).total_seconds() / 60

            except Exception:
                continue

            if 0 < age <= 90:

                d = haversine(last, cell)

                if d < distance:
                    best = track_id
                    distance = d

        track_id = best or cell["id"]

        used.add(track_id)

        track = history.setdefault(
            track_id,
            {"observations": []}
        )

        observations = track.setdefault(
            "observations",
            []
        )

        current_timestamp = stamp(timestamp)

        if (
            not observations
            or observations[-1]["timestamp"]
            != current_timestamp
        ):

            observations.append({

                "timestamp": current_timestamp,

                "lat": cell["lat"],
                "lon": cell["lon"],

                "max_dbz": cell["max_dbz"]

            })

            del observations[:-24]

        track_data = {

            "track_id": track_id,

            "observations": observations[-24:],

            "forecast_status":
                "insufficient_observations"

        }

        if len(observations) >= 2:

            a = observations[-2]
            b = observations[-1]

            ta = datetime.fromisoformat(
                a["timestamp"].replace("Z", "+00:00")
            )

            tb = datetime.fromisoformat(
                b["timestamp"].replace("Z", "+00:00")
            )

            minutes = (
                tb - ta
            ).total_seconds() / 60

            if minutes > 0:

                def predict(m):

                    factor = m / minutes

                    return [
                        round(
                            b["lat"]
                            + (b["lat"] - a["lat"])
                            * factor,
                            5
                        ),

                        round(
                            b["lon"]
                            + (b["lon"] - a["lon"])
                            * factor,
                            5
                        )
                    ]

                track_data.update({

                    "forecast_status":
                        "linear_extrapolation_from_two_observations",

                    "real_vector": [
                        [a["lat"], a["lon"]],
                        [b["lat"], b["lon"]]
                    ],

                    "forecast_30m": [
                        [b["lat"], b["lon"]],
                        predict(30)
                    ],

                    "forecast_60m": [
                        [b["lat"], b["lon"]],
                        predict(60)
                    ],

                    "observed_interval_minutes":
                        round(minutes, 1),

                    "speed_kmh": round(
                        haversine(a, b) / (minutes / 60),
                        1
                    )

                })

        cell["track"] = track_data

        cell["track_id"] = track_id

        cell["vectors"] = {

            "real": track_data.get("real_vector"),

            "predictive_30m":
                track_data.get("forecast_30m"),

            "predictive_60m":
                track_data.get("forecast_60m"),

            "forecast_status":
                track_data["forecast_status"]

        }

    return cells, history


# ---------------------------------------------------------
# PIPELINE PRINCIPALE
# ---------------------------------------------------------

def main():

    started = now()

    source = None

    try:

        key, size = latest()

        source, url = fetch(key, size)

        radar, grid, inverse, origin, product_time = decode(
            source
        )

        if product_time is None:

            match = re.search(
                r"OPERA@(\d{8}T\d{4})@0@DBZH\.h5$",
                key
            )

            if not match:
                raise RuntimeError(
                    "Timestamp del prodotto assente."
                )

            product_time = datetime.strptime(
                match.group(1),
                "%Y%m%dT%H%M"
            ).replace(tzinfo=timezone.utc)

        history = history_read()

        cells, history = detect(
            radar,
            grid,
            inverse,
            origin,
            product_time,
            history
        )

        if not cells:

            raise RuntimeError(
                f"Nessuna cella ≥{THRESHOLD} dBZ "
                f"con almeno {MIN_PIXELS} pixel. "
                "Output precedente conservato."
            )

        payload = {

            "schema_version": 4,

            "product":
                "I.R.I.D.E. — Integrated Radar Imaging & Detection Engine",

            "status": "ok",

            "timestamp": stamp(product_time),

            "generated_at": stamp(now()),

            "source": {

                "provider":
                    "EUMETNET OPERA via public CloudFerro object storage",

                "url": url,

                "object_key": key,

                "product":
                    "OPERA CIRRUS maximum reflectivity composite DBZH",

                "license": grid["license"],

                "attribution":
                    "Radar: EUMETNET OPERA; archivio: CloudFerro; "
                    "elaborazione: I.R.I.D.E."

            },

            "grid": grid,

            "processing": {

                "threshold_dbz": THRESHOLD,

                "minimum_cell_pixels": MIN_PIXELS,

                "cell_count": len(cells),

                "method":
                    "8-connected threshold segmentation",

                "matrix_size": [
                    MATRIX_SIDE,
                    MATRIX_SIDE
                ],

                "matrix_floor_dbz": MATRIX_FLOOR_DBZ,

                "matrix_context_factor": CONTEXT_FACTOR,

                "matrix_description":
                    "Contextual radar matrix including weak echoes "
                    "around each detected convective cell.",

                "forecast_method":
                    "Linear extrapolation from two distinct observed centroids",

                "limits": [

                    "OPERA MAX DBZH è un composito 2D, "
                    "non un volume radar 3D.",

                    "La matrice contestuale conserva gli echi "
                    "circostanti, ma non fornisce quote verticali.",

                    "VIL, echo-top e rain rate non sono ricavati "
                    "da questo prodotto.",

                    "Le classi MCS/MCC/MCV/squall line/V-shape/"
                    "comma head/echo hook/supercella non vengono assegnate.",

                    "I vettori +30/+60 minuti sono estrapolazioni "
                    "sperimentali, non previsioni ufficiali."

                ]

            },

            "cell_count": len(cells),

            "cells": cells

        }

        save(
            DATA / "iride.json",
            payload
        )

        save(
            ROOT / "docs" / "data" / "iride.json",
            payload
        )

        save(
            HIST,
            history
        )

        save(
            DATA / "status.json",
            {

                "status": "ok",

                "timestamp": payload["timestamp"],

                "cell_count": len(cells),

                "matrix_size": MATRIX_SIDE,

                "source_object": key,

                "elapsed_seconds": round(
                    (now() - started).total_seconds(),
                    1
                )

            }
        )

        print(
            f"IRIDE OK: {len(cells)} celle; "
            f"matrice {MATRIX_SIDE}x{MATRIX_SIDE}; "
            f"{payload['timestamp']}"
        )

    except Exception as error:

        save(
            DATA / "status.json",
            {

                "status": "error",

                "generated_at": stamp(now()),

                "message": str(error),

                "previous_data_preserved": True

            }
        )

        traceback.print_exc()

        raise

    finally:

        if source:

            try:
                source.unlink(missing_ok=True)

            except Exception:
                pass


if __name__ == "__main__":
    main()
