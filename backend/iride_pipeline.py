#!/usr/bin/env python3
"""I.R.I.D.E. — OPERA radar pipeline."""

from __future__ import annotations

import json
import re
import sys
import struct
import zlib
import traceback
import xml.etree.ElementTree as ET

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

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

TIMEOUT = 45
MAX_BYTES = 250_000_000

THRESHOLD = 25.0
MIN_PIXELS = 8

MAX_CELLS = 100
MAX_3D_CELLS = 30

MATRIX_SIDE = 72
MATRIX_FLOOR = 8.0
CONTEXT_FACTOR = 2.4
MIN_CONTEXT = 48
MAX_CONTEXT = 360

REGION = {
    "west": -12.0,
    "east": 30.0,
    "south": 30.0,
    "north": 50.0
}

TRACK_MAX_AGE_MIN = 70
TRACK_MAX_DISTANCE_KM = 100.0
TRACK_MAX_SPEED_KMH = 140.0
TRACK_HISTORY_SIZE = 24

UA = "IRIDE-open-radar/3.0"


def now():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def attr(group, key, default=None):
    if group is None or key not in group.attrs:
        return default
    value = group.attrs[key]
    if isinstance(value, np.ndarray) and value.shape == ():
        value = value.item()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def number(value, default=None):
    try:
        result = float(np.asarray(value).item())
        return result if np.isfinite(result) else default
    except Exception:
        return default


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8"
    )
    temp.replace(path)


def parse_product_time(key):
    match = re.search(r"OPERA@(\d{8}T\d{4})@0@DBZH\.h5$", key)
    if not match:
        return None
    return datetime.strptime(
        match.group(1), "%Y%m%dT%H%M"
    ).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------
# ACQUISIZIONE OPERA
# ---------------------------------------------------------

def list_day(day):
    prefix = f"{day:%Y/%m/%d}/{PREFIX}/"
    token = None
    results = []

    while True:
        params = {"list-type": "2", "prefix": prefix}
        if token:
            params["continuation-token"] = token

        response = requests.get(
            f"{S3}/{BUCKET}/",
            params=params,
            timeout=TIMEOUT,
            headers={"User-Agent": UA}
        )
        response.raise_for_status()

        root = ET.fromstring(response.content)
        ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}

        for item in root.findall("s:Contents", ns):
            key = item.findtext("s:Key", "", ns)
            size = int(item.findtext("s:Size", "0", ns))

            if (
                key.endswith("@0@DBZH.h5")
                and 0 < size <= MAX_BYTES
                and parse_product_time(key)
            ):
                results.append((key, size))

        truncated = root.findtext("s:IsTruncated", "false", ns)
        if truncated.lower() != "true":
            break

        token = root.findtext("s:NextContinuationToken", None, ns)
        if not token:
            raise RuntimeError("Paginazione archivio OPERA incompleta.")

    return results


def latest():
    current = now()
    products = []

    for offset in range(3):
        day = (current - timedelta(days=offset)).date()
        try:
            products.extend(list_day(datetime(
                day.year, day.month, day.day, tzinfo=timezone.utc
            )))
        except Exception as exc:
            print(f"Archivio {day}: {exc}", file=sys.stderr)

    if not products:
        raise RuntimeError("Nessun prodotto OPERA DBZH trovato.")

    return max(products, key=lambda item: parse_product_time(item[0]))


def fetch(key, expected_size):
    folder = DATA / "raw"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "opera_latest.h5"

    url = f"{S3}/{BUCKET}/" + quote(key, safe="/@")
    total = 0

    with requests.get(
        url,
        stream=True,
        timeout=TIMEOUT,
        headers={"User-Agent": UA}
    ) as response:
        response.raise_for_status()

        with path.open("wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_BYTES:
                    raise RuntimeError(
                        "File OPERA oltre il limite consentito."
                    )
                output.write(chunk)

    if expected_size and total != expected_size:
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Download incompleto: {total}/{expected_size} byte."
        )

    return path, url


# ---------------------------------------------------------
# DECODIFICA ODIM HDF5
# ---------------------------------------------------------

def decode(path):
    with h5py.File(path, "r") as h:
        data_path = "dataset1/data1/data"
        if data_path not in h:
            raise RuntimeError("Dataset DBZH ODIM assente.")

        raw = h[data_path][()]
        parameter = h.get("dataset1/data1/what")

        quantity = str(attr(parameter, "quantity", "")).upper()
        if quantity != "DBZH":
            raise RuntimeError(f"Prodotto non DBZH: {quantity}")

        gain = number(attr(parameter, "gain"), 1.0)
        offset = number(attr(parameter, "offset"), 0.0)
        nodata = number(attr(parameter, "nodata"))
        undetect = number(attr(parameter, "undetect"))

        radar = raw.astype(np.float32) * gain + offset
        invalid = ~np.isfinite(radar)

        if nodata is not None:
            invalid |= raw == nodata
        if undetect is not None:
            invalid |= raw == undetect

        radar[invalid] = np.nan

        where = h.get("where")
        if where is None:
            raise RuntimeError("Metadati geografici ODIM assenti.")

        projection = attr(where, "projdef")
        xscale = number(attr(where, "xscale"))
        yscale = number(attr(where, "yscale"))
        nx = int(number(attr(where, "xsize"), radar.shape[1]))
        ny = int(number(attr(where, "ysize"), radar.shape[0]))

        if radar.shape != (ny, nx):
            raise RuntimeError(
                "Dimensioni raster e metadati ODIM non coerenti."
         )        if not projection or xscale is None or yscale is None:
            raise RuntimeError("Georeferenziazione ODIM incompleta.")

        ll_x = number(attr(where, "LL_x"))
        ll_y = number(attr(where, "LL_y"))

        if ll_x is None or ll_y is None:
            ll_lon = number(attr(where, "LL_lon"))
            ll_lat = number(attr(where, "LL_lat"))

            if ll_lon is None or ll_lat is None:
                raise RuntimeError(
                    "Coordinate geografiche LL ODIM mancanti."
                )

            forward = Transformer.from_crs(
                CRS.from_epsg(4326),
                CRS.from_user_input(projection),
                always_xy=True
            )
            ll_x, ll_y = forward.transform(ll_lon, ll_lat)

        if xscale <= 0 or yscale <= 0:
            raise RuntimeError("Risoluzione spaziale ODIM non valida.")

        inverse = Transformer.from_crs(
            CRS.from_user_input(projection),
            CRS.from_epsg(4326),
            always_xy=True
        )

        grid = {
            "xscale_m": xscale,
            "yscale_m": yscale,
            "xsize": nx,
            "ysize": ny,
            "projection": str(projection)
        }

        product_time = None
        product_what = h.get("what")

        date_value = attr(product_what, "date")
        time_value = attr(product_what, "time")

        if date_value and time_value:
            try:
                product_time = datetime.strptime(
                    f"{date_value}{time_value[:6]}",
                    "%Y%m%d%H%M%S"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                product_time = None

        return radar, grid, inverse, (ll_x, ll_y), product_time


# ---------------------------------------------------------
# GEOGRAFIA E MATRICE RADAR
# ---------------------------------------------------------

def haversine(a, b):
    from math import radians, sin, cos, asin, sqrt

    p1, p2 = radians(a["lat"]), radians(b["lat"])
    dp = radians(b["lat"] - a["lat"])
    dl = radians(b["lon"] - a["lon"])
    q = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 6371.0 * 2 * asin(min(1.0, sqrt(q)))


def inside_region(lat, lon):
    return (
        REGION["south"] <= lat <= REGION["north"]
        and REGION["west"] <= lon <= REGION["east"]
    )


def contextual_matrix(radar, cx, cy, component_slice):
    ys, xs = component_slice
    size = int(np.ceil(
        max(xs.stop - xs.start, ys.stop - ys.start) * CONTEXT_FACTOR
    ))
    size = min(MAX_CONTEXT, max(MIN_CONTEXT, size))

    half = size // 2
    x0, y0 = int(round(cx)) - half, int(round(cy)) - half
    x1, y1 = x0 + size, y0 + size

    patch = np.zeros((size, size), dtype=np.float32)

    sx0, sy0 = max(0, x0), max(0, y0)
    sx1, sy1 = min(radar.shape[1], x1), min(radar.shape[0], y1)

    if sx1 > sx0 and sy1 > sy0:
        dx0, dy0 = sx0 - x0, sy0 - y0
        patch[dy0:dy0 + sy1 - sy0, dx0:dx0 + sx1 - sx0] = (
            radar[sy0:sy1, sx0:sx1]
        )

    patch[~np.isfinite(patch) | (patch < MATRIX_FLOOR)] = 0

    output = np.zeros((MATRIX_SIDE, MATRIX_SIDE), dtype=np.float32)

    for oy in range(MATRIX_SIDE):
        py0 = int(oy * size / MATRIX_SIDE)
        py1 = max(py0 + 1, int((oy + 1) * size / MATRIX_SIDE))

        for ox in range(MATRIX_SIDE):
            px0 = int(ox * size / MATRIX_SIDE)
            px1 = max(px0 + 1, int((ox + 1) * size / MATRIX_SIDE))

            block = patch[py0:py1, px0:px1]
            valid = block[block >= MATRIX_FLOOR]

            if valid.size:
                output[oy, ox] = float(valid.max())

    info = {
        "rows": MATRIX_SIDE,
        "cols": MATRIX_SIDE,
        "context_pixels": size,
        "floor_dbz": MATRIX_FLOOR,
        "aggregation": "maximum_dbzh",
        "vertical_information": False
    }

    return np.round(output, 1).tolist(), info


# ---------------------------------------------------------
# PERIMETRO E RILEVAMENTO CELLE
# ---------------------------------------------------------

def ordered_perimeter(component, x_start, y_start, grid, inverse, origin):
    edge = component & ~ndimage.binary_erosion(
        component,
        structure=np.ones((3, 3)),
        border_value=0
    )

    yy, xx = np.where(edge)
    if len(xx) < 3:
        return []

    cx, cy = float(xx.mean()), float(yy.mean())
    order = np.argsort(np.arctan2(yy - cy, xx - cx))

    if len(order) > 100:
        order = order[np.linspace(
            0, len(order) - 1, 100
        ).astype(int)]

    ox, oy = origin
    points = []

    for index in order:
        px = x_start + int(xx[index])
        py = y_start + int(yy[index])

        gx = ox + px * grid["xscale_m"]
        gy = oy + (grid["ysize"] - 1 - py) * grid["yscale_m"]

        lon, lat = inverse.transform(gx, gy)

        if np.isfinite(lat) and np.isfinite(lon):
            points.append([
                round(float(lat), 4),
                round(float(lon), 4)
            ])

    return points


def detect(radar, grid, inverse, origin):
    mask = np.isfinite(radar) & (radar >= THRESHOLD)

    labels, count = ndimage.label(
        mask,
        structure=np.ones((3, 3), dtype=np.uint8)
    )

    objects = ndimage.find_objects(labels)
    ox, oy = origin
    cells = []

    for label_id, sl in enumerate(objects, 1):
        if sl is None:
            continue

        ys, xs = sl
        component = labels[sl] == label_id
        area = int(component.sum())

        if area < MIN_PIXELS:
            continue

        yy, xx = np.where(component)
        values = radar[sl][yy, xx]

        weights = np.maximum(values - THRESHOLD + 1, 1)

        cx = xs.start + float(np.average(xx, weights=weights))
        cy = ys.start + float(np.average(yy, weights=weights))

        gx = ox + cx * grid["xscale_m"]
        gy = oy + (grid["ysize"] - 1 - cy) * grid["yscale_m"]

        lon, lat = inverse.transform(gx, gy)

        if not inside_region(float(lat), float(lon)):
            continue

        matrix, matrix_info = contextual_matrix(
            radar, cx, cy, sl
        )

        perimeter = ordered_perimeter(
            component,
            xs.start,
            ys.start,
            grid,
            inverse,
            origin
        )

        bbox_width = max(1, xs.stop - xs.start)
        bbox_height = max(1, ys.stop - ys.start)
        elongation = max(
            bbox_width / bbox_height,
            bbox_height / bbox_width
        )
        fill_ratio = area / float(bbox_width * bbox_height)

        if area >= 25 and elongation >= 3.0:
            morphology = "linear_echo_candidate"
            morphology_label = "Eco lineare candidato"
        elif area >= 40 and elongation < 3.0:
            morphology = "clustered_echo"
            morphology_label = "Eco organizzato / cluster"
        elif float(values.max()) >= 40 and area < 40:
            morphology = "isolated_convective_echo"
            morphology_label = "Eco convettivo isolato"
        else:
            morphology = "unclassified_echo"
            morphology_label = "Eco non classificato"

        cells.append({
            "id": f"OPERA-{label_id:05d}",
            "lat": round(float(lat), 4),
            "lon": round(float(lon), 4),
            "max_dbz": round(float(values.max()), 1),
            "mean_dbz": round(float(values.mean()), 1),
            "threshold_dbz": THRESHOLD,
            "area_pixels": area,
            "approx_area_km2": round(
                area * abs(
                    grid["xscale_m"] * grid["yscale_m"]
                ) / 1e6,
                1
            ),
            "perimeter": perimeter,
            "perimeter_semantics": "Bordo dei pixel connessi DBZH >= soglia; non è un confine mesoscala.",            "morphology": {
                "class": morphology,
                "label": morphology_label,
                "elongation_ratio": round(float(elongation), 2),
                "fill_ratio": round(float(fill_ratio), 3),
                "status": "morphological_candidate_not_mcs_diagnosis"
            },
            "mesoscale_classification": {
                "class": "not_confirmed",
                "reason": "Un singolo composito DBZH 2D non conferma MCS, MCC, supercella o squall line."
            },
            "grid_matrix": matrix,
            "matrix_sampling": matrix_info,
            "matrix_semantics": {
                "kind": "observed_2d_dbzh_context",
                "units": "dBZ",
                "vertical_information": False
            },
            "classification": "unclassified_radar_echo",
            "severity": {
                "vil": None,
                "echo_top": None,
                "rain_mmh": None
            }
        })

    cells.sort(
        key=lambda c: (c["max_dbz"], c["area_pixels"]),
        reverse=True
    )

    return cells[:MAX_CELLS]


# ---------------------------------------------------------
# TRACKING
# ---------------------------------------------------------

def read_history():
    try:
        old = json.loads(HIST.read_text(encoding="utf-8"))

        if old.get("schema_version") != 2:
            return {"schema_version": 2, "tracks": {}}

        if not isinstance(old.get("tracks"), dict):
            return {"schema_version": 2, "tracks": {}}

        return old

    except Exception:
        return {"schema_version": 2, "tracks": {}}


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def track_cells(cells, timestamp, history):
    tracks = history.setdefault("tracks", {})
    active = {}

    for track_id, track in tracks.items():
        observations = track.get("observations", [])

        if not observations:
            continue

        try:
            age = (
                timestamp - parse_time(observations[-1]["timestamp"])
            ).total_seconds() / 60
        except Exception:
            continue

        if 0 <= age <= TRACK_MAX_AGE_MIN:
            active[track_id] = track

    candidates = []

    for cell_index, cell in enumerate(cells):
        for track_id, track in active.items():
            last = track["observations"][-1]

            try:
                age = (
                    timestamp - parse_time(last["timestamp"])
                ).total_seconds() / 60

                if age <= 0 or age > TRACK_MAX_AGE_MIN:
                    continue

                distance = haversine(last, cell)
                speed = distance / (age / 60)

                if (
                    distance <= TRACK_MAX_DISTANCE_KM
                    and speed <= TRACK_MAX_SPEED_KMH
                ):
                    candidates.append((
                        distance, cell_index, track_id
                    ))

            except Exception:
                continue

    candidates.sort(key=lambda item: item[0])

    assigned_cells = set()
    assigned_tracks = set()
    assignments = {}

    for distance, cell_index, track_id in candidates:
        if (
            cell_index in assigned_cells
            or track_id in assigned_tracks
        ):
            continue

        assignments[cell_index] = track_id
        assigned_cells.add(cell_index)
        assigned_tracks.add(track_id)

    current_stamp = stamp(timestamp)

    for index, cell in enumerate(cells):
        track_id = assignments.get(index)

        if track_id is None:
            track_id = (
                f"IRIDE-{timestamp:%Y%m%dT%H%M}-{index:03d}"
            )

            while track_id in tracks:
                track_id += "X"

            tracks[track_id] = {"observations": []}

        track = tracks[track_id]
        observations = track.setdefault("observations", [])

        if (
            not observations
            or observations[-1]["timestamp"] != current_stamp
        ):
            observations.append({
                "timestamp": current_stamp,
                "lat": cell["lat"],
                "lon": cell["lon"],
                "max_dbz": cell["max_dbz"]
            })

        del observations[:-TRACK_HISTORY_SIZE]

        track_data = {
            "track_id": track_id,
            "observations": observations[-TRACK_HISTORY_SIZE:],
            "forecast_status": "insufficient_observations",
            "real_vector": None,
            "forecast_30m": None,
            "forecast_60m": None,
            "forecast_180m": None,
            "speed_kmh": None
        }

        if len(observations) >= 2:
            a, b = observations[-2], observations[-1]

            ta = parse_time(a["timestamp"])
            tb = parse_time(b["timestamp"])

            minutes = (tb - ta).total_seconds() / 60

            if 0 < minutes <= TRACK_MAX_AGE_MIN:
                distance = haversine(a, b)
                speed = distance / (minutes / 60)

                if speed <= TRACK_MAX_SPEED_KMH:
                    def predict(future_minutes):
                        factor = future_minutes / minutes

                        return [
                            round(
                                b["lat"]
                                + (b["lat"] - a["lat"]) * factor,
                                5
                            ),
                            round(
                                b["lon"]
                                + (b["lon"] - a["lon"]) * factor,
                                5
                            )
                        ]

                    track_data.update({
                        "forecast_status":
                            "experimental_linear_extrapolation",
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
                        "forecast_180m": [
                            [b["lat"], b["lon"]],
                            predict(180)
                        ],
                        "observed_interval_minutes": round(
                            minutes, 1
                        ),
                        "speed_kmh": round(speed, 1)
                    })

        cell["track_id"] = track_id
        cell["track"] = track_data

        cell["vectors"] = {
            "real": track_data["real_vector"],
            "predictive_30m": track_data["forecast_30m"],
            "predictive_60m": track_data["forecast_60m"],
            "predictive_180m": track_data["forecast_180m"],
            "forecast_status": track_data["forecast_status"]
        }

    history["tracks"] = active | {
        cell["track_id"]: tracks[cell["track_id"]]
        for cell in cells
    }

    return cells, history


# ---------------------------------------------------------
# RASTER DBZH GEORIFERITO PER LA MAPPA LEAFLET
# ---------------------------------------------------------

def png_chunk(kind, data):
    body = kind + data
    return (
        struct.pack(">I", len(data))
        + body
        + struct.pack(">I", zlib.crc32(body) & 0xffffffff)
    )


def write_png(path, width, height, rgba):
    raw = bytearray()
    stride = width * 4

    for y in range(height):
        raw.append(0)
        start = y * stride
        raw.extend(rgba[start:start + stride])

    header = struct.pack(">2I", width, height)
    header += bytes([8, 6, 0, 0, 0])

    content = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + png_chunk(b"IEND", b"")
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")    temp.write_bytes(content)
    temp.replace(path)


def generate_radar_overlay(radar, grid, origin):
    width = 720
    height = 400

    west = REGION["west"]
    east = REGION["east"]
    south = REGION["south"]
    north = REGION["north"]

    longitudes = west + (
        np.arange(width, dtype=np.float64) + 0.5
    ) * (east - west) / width

    latitudes = north - (
        np.arange(height, dtype=np.float64) + 0.5
    ) * (north - south) / height

    lon_grid, lat_grid = np.meshgrid(longitudes, latitudes)

    forward = Transformer.from_crs(
        CRS.from_epsg(4326),
        CRS.from_user_input(grid["projection"]),
        always_xy=True
    )

    projected_x, projected_y = forward.transform(
        lon_grid, lat_grid
    )

    ox, oy = origin

    pixel_x = np.rint(
        (projected_x - ox) / grid["xscale_m"]
    ).astype(np.int32)

    pixel_y = np.rint(
        (grid["ysize"] - 1)
        - (projected_y - oy) / grid["yscale_m"]
    ).astype(np.int32)

    valid = (
        np.isfinite(projected_x)
        & np.isfinite(projected_y)
        & (pixel_x >= 0)
        & (pixel_x < grid["xsize"])
        & (pixel_y >= 0)
        & (pixel_y < grid["ysize"])
    )

    sampled = np.full((height, width), np.nan, dtype=np.float32)
    sampled[valid] = radar[
        pixel_y[valid], pixel_x[valid]
    ]

    rgba = np.zeros((height, width, 4), dtype=np.uint8)

    classes = [
        (15.0, 25.0, (57, 126, 232)),
        (25.0, 30.0, (36, 189, 112)),
        (30.0, 35.0, (244, 212, 62)),
        (35.0, 45.0, (240, 120, 50)),
        (45.0, float("inf"), (237, 66, 200))
    ]

    for lower, upper, rgb in classes:
        selected = (
            np.isfinite(sampled)
            & (sampled >= lower)
            & (sampled < upper)
        )
        rgba[selected, 0] = rgb[0]
        rgba[selected, 1] = rgb[1]
        rgba[selected, 2] = rgb[2]
        rgba[selected, 3] = 178

    path = ROOT / "docs" / "data" / "iride_radar.png"
    write_png(path, width, height, rgba)

    return {
        "image": "data/iride_radar.png",
        "bounds": [
            [south, west],
            [north, east]
        ],
        "width": width,
        "height": height,
        "format": "PNG RGBA",
        "opacity": 0.82,
        "minimum_dbz": 15,
        "transparent_below_dbz": 15,
        "georeferencing": "geographic sampling from ODIM projection",
        "timestamp": None
    }


# ---------------------------------------------------------
# PUBBLICAZIONE
# ---------------------------------------------------------

def main():
    started = now()
    source_path = None

    try:
        key, size = latest()

        key_time = parse_product_time(key)

        if key_time is None:
            raise RuntimeError(
                "Timestamp prodotto OPERA non disponibile."
            )

        age_minutes = (now() - key_time).total_seconds() / 60

        if age_minutes < -5:
            raise RuntimeError("Timestamp OPERA nel futuro.")

        if age_minutes > 90:
            raise RuntimeError(
                f"Prodotto OPERA obsoleto: {age_minutes:.1f} minuti."
            )

        source_path, source_url = fetch(key, size)

        radar, grid, inverse, origin, decoded_time = decode(
            source_path
        )

        if decoded_time is not None:
            difference = abs(
                (decoded_time - key_time).total_seconds()
            )

            if difference > 120:
                raise RuntimeError(
                    "Timestamp ODIM e archivio non coerenti."
                )

            product_time = decoded_time
        else:
            product_time = key_time

        age_minutes = (
            now() - product_time
        ).total_seconds() / 60

        if age_minutes < -5 or age_minutes > 90:
            raise RuntimeError(
                f"Età prodotto non valida: {age_minutes:.1f} minuti."
            )

        cells = detect(radar, grid, inverse, origin)
        radar_overlay = generate_radar_overlay(radar, grid, origin)
        radar_overlay["timestamp"] = stamp(product_time)

        if not cells:
            raise RuntimeError(
                "Nessuna cella rilevata. "
                "Pubblicazione precedente conservata."
            )

        history = read_history()
        cells, history = track_cells(
            cells, product_time, history
        )

        for index, cell in enumerate(cells):
            cell["3d_available"] = index < MAX_3D_CELLS
            cell["3d_rank"] = (
                index + 1 if index < MAX_3D_CELLS else None
            )

        payload = {
            "schema_version": 6,
            "product": (
                "I.R.I.D.E. — Integrated Radar Imaging "
                "& Detection Engine"
            ),
            "status": "ok",
            "timestamp": stamp(product_time),
            "generated_at": stamp(now()),
            "region": REGION,
            "source": {
                "provider": "EUMETNET OPERA",
                "archive": "CloudFerro openradar-24h",
                "url": source_url,
                "object_key": key,
                "product": "OPERA DBZH composite",
                "attribution": (
                    "Dati radar EUMETNET OPERA, archivio CloudFerro; "
                    "elaborazione I.R.I.D.E. Licenza e condizioni del "
                    "prodotto da verificare nella documentazione "
                    "ufficiale."
                )
            },
            "grid": grid,
            "radar_overlay": radar_overlay,
            "processing": {
                "threshold_dbz": THRESHOLD,
                "minimum_cell_pixels": MIN_PIXELS,
                "maximum_detected_cells": MAX_CELLS,
                "maximum_3d_cells": MAX_3D_CELLS,
                "morphology": "2D DBZH morphology only; mesoscale classes are not confirmed",
                "matrix_size": [MATRIX_SIDE, MATRIX_SIDE],
                "tracking": (
                    "one-to-one spatial and temporal association"
                ),
                "forecast": (
                    "experimental linear extrapolation "
                    "after two observations"
                ),
                "vertical_data": (
                    "not available from 2D DBZH composite"
                )
            },
            "cell_count": len(cells),
            "cells": cells
        }

        save(DATA / "iride.json", payload)
        save(ROOT / "docs" / "data" / "iride.json", payload)
        save(HIST, history)

        save(DATA / "status.json", {
            "status": "ok",
            "timestamp": payload["timestamp"],
            "cell_count": len(cells),
            "cells_3d": min(len(cells), MAX_3D_CELLS),
            "elapsed_seconds": round(
                (now() - started).total_seconds(), 1
            )
        })

        print(
            f"IRIDE OK: {len(cells)} celle; "
            f"{min(len(cells), MAX_3D_CELLS)} abilitate 3D; "
            f"{payload['timestamp']}"
        )

    except Exception as error:
        save(DATA / "status.json", {
            "status": "error",
            "generated_at": stamp(now()),
            "message": str(error),
            "previous_data_preserved": True
        })

        traceback.print_exc()
        raise

    finally:
        if source_path:
            source_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
