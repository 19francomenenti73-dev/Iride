#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
I.R.I.D.E. — Decoder OPERA / ODIM HDF5
Integrated Radar Imaging & Detection Engine

Input:
    data/raw/opera_dbzh_*.h5

Output:
    data/iride_database.json

Dipendenze:
    h5py
    numpy
    pyproj

Nota:
Il decoder conserva i valori numerici calibrati DBZH.
Non converte colori di immagini in riflettività radar.
Non identifica automaticamente celle temporalesche:
la segmentazione è uno stadio successivo.
"""

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import h5py
import numpy as np
from pyproj import CRS, Transformer


ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
OUTPUT_FILE = ROOT / "data" / "iride_database.json"

# Limiti per evitare di elaborare file corrotti o eccessivi.
MAX_PIXELS = 30_000_000
MAX_OUTPUT_PIXELS = 2_000_000

# DBZH è una riflettività equivalente in dBZ.
# Le soglie qui sotto sono usate esclusivamente per individuare
# regioni con riflettività significativa, non per classificare
# con certezza strutture convettive.
MIN_DBZ = 15.0
MIN_REGION_PIXELS = 4

ODIM_SIGNATURE = b"\x89HDF\r\n\x1a\n"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def attr(group, name, default=None):
    if group is None:
        return default

    try:
        value = group.attrs[name]
    except (KeyError, TypeError):
        return default

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")

    if isinstance(value, np.ndarray) and value.shape == ():
        value = value.item()

    if isinstance(value, np.generic):
        value = value.item()

    return value


def read_group_attr(handle, group_name, name, default=None):
    if group_name not in handle:
        return default
    return attr(handle[group_name], name, default)


def find_datasets(handle):
    found = []

    def visitor(name, obj):
        if not isinstance(obj, h5py.Dataset):
            return

        basename = name.rsplit("/", 1)[-1]

        # I prodotti ODIM normalmente collocano i dati in
        # datasetX/dataY/data; il parametro è nell'attributo
        # quantity del gruppo dataY.
        if basename != "data":
            return

        parent = obj.parent
        quantity = attr(parent, "quantity", "")

        if isinstance(quantity, bytes):
            quantity = quantity.decode("ascii", errors="ignore")

        found.append({
            "path": name,
            "dataset": obj,
            "quantity": str(quantity).upper(),
            "parent": parent,
            "dataset_group": parent.parent,
        })

    handle.visititems(visitor)
    return found


def find_dbzh_dataset(handle):
    datasets = find_datasets(handle)

    matches = [
        item for item in datasets
        if item["quantity"] == "DBZH"
    ]

    if not matches:
        available = sorted({
            item["quantity"] for item in datasets
            if item["quantity"]
        })

        raise ValueError(
            "Nessun dataset quantity=DBZH trovato. "
            f"Parametri disponibili: {available or 'non dichiarati'}"
        )

    # Preferisce il dataset con più pixel, senza interpolare
    # né fondere scansioni diverse.
    matches.sort(
        key=lambda item: int(np.prod(item["dataset"].shape)),
        reverse=True
    )

    return matches[0]


def read_calibration(data_group, raw):
    gain = attr(data_group, "gain")
    offset = attr(data_group, "offset")
    nodata = attr(data_group, " nodata", None)

    if nodata is None:
        nodata = attr(data_group, "nodata", None)

    undetect = attr(data_group, "undetect", None)

    if gain is None or offset is None:
        raise ValueError(
            "Metadati ODIM incompleti: gain/offset assenti. "
            "Impossibile convertire i pixel in dBZ."
        )

    gain = float(gain)
    offset = float(offset)

    if not math.isfinite(gain) or not math.isfinite(offset):
        raise ValueError("Gain/offset non finiti.")

    if gain == 0:
        raise ValueError("Gain pari a zero: calibrazione non valida.")

    valid = np.isfinite(raw)

    if nodata is not None:
        valid &= raw != float(nodata)

    if undetect is not None:
        valid &= raw != float(undetect)

    calibrated = raw.astype(np.float32) * gain + offset
    valid &= np.isfinite(calibrated)

    # I valori non validi diventano NaN e non vengono pubblicati
    # come se fossero misure.
    calibrated[~valid] = np.nan

    return calibrated, valid


def read_projection(handle, dataset_group):
    """
    Legge la proiezione quando presente nei metadati.
    I compositi OPERA possono usare una proiezione cartografica
    comune; non viene assunta automaticamente senza parametri.
    """
    where = None

    for candidate in (dataset_group, handle):
        if candidate is not None:
            where = candidate
            if attr(where, "projdef") is not None:
                break

    projdef = attr(where, "projdef") if where is not None else None

    if not projdef:
        raise ValueError(
            "Metadato projdef assente. "
            "Il file non consente una georeferenziazione "
            "affidabile tramite questo decoder."
        )

    try:
        source_crs = CRS.from_user_input(str(projdef))
        transformer = Transformer.from_crs(
            source_crs,
            CRS.from_epsg(4326),
            always_xy=True
        )
    except Exception as exc:
        raise ValueError(
            f"Proiezione ODIM non interpretabile: {projdef}"
        ) from exc

    return transformer


def read_grid_metadata(handle, dataset_group, shape):
    """
    Interpreta la griglia tramite area extent e dimensioni.
    Non inventa una risoluzione se i metadati sono incompleti.
    """
    where = dataset_group

    xsize = attr(where, "xsize")
    ysize = attr(where, "ysize")

    if xsize is None:
        xsize = attr(handle, "xsize")
    if ysize is None:
        ysize = attr(handle, "ysize")

    if xsize is None or ysize is None:
        raise ValueError(
            "Dimensioni griglia xsize/ysize assenti."
        )

    xsize = int(xsize)
    ysize = int(ysize)

    height, width = shape

    if width != xsize or height != ysize:
        raise ValueError(
            "Dimensioni raster e metadati ODIM non coincidono: "
            f"raster={width}x{height}, metadati={xsize}x{ysize}."
        )

    # Metadati tipici dei compositi OPERA.
    xscale = attr(where, "xscale")
    yscale = attr(where, "yscale")

    if xscale is None:
        xscale = attr(handle, "xscale")
    if yscale is None:
        yscale = attr(handle, "yscale")

    if xscale is None or yscale is None:
        raise ValueError(
            "Risoluzione della griglia non dichiarata."
        )

    xscale = float(xscale)
    yscale = float(yscale)

    if (
        not math.isfinite(xscale)
        or not math.isfinite(yscale)
        or xscale <= 0
        or yscale <= 0
    ):
        raise ValueError("Risoluzione della griglia non valida.")

    area = None

    for candidate in (where, handle):
        if candidate is None:
            continue

        xmin = attr(candidate, "x1")
        xmax = attr(candidate, "x2")
        ymin = attr(candidate, "y1")
        ymax = attr(candidate, "y2")

        if all(v is not None for v in (xmin, xmax, ymin, ymax)):
            area = (
                float(xmin), float(ymin),
                float(xmax), float(ymax)
            )
            break

    if area is None:
        raise ValueError(
            "Estensione spaziale x1/y1/x2/y2 assente."
        )

    xmin, ymin, xmax, ymax = area

    if not all(math.isfinite(v) for v in area):
        raise ValueError("Estensione spaziale non valida.")

    if xmax <= xmin or ymax <= ymin:
        raise ValueError("Estensione spaziale invertita o nulla.")

    return {
        "width": width,
        "height": height,
        "xscale": xscale,
        "yscale": yscale,
        "xmin": xmin,
        "ymin": ymin,
        "xmax": xmax,
        "ymax": ymax,
    }


def pixel_centres_to_lonlat(transformer, grid):
    """
    Calcola le coordinate dei centri dei pixel.
    Convenzione dell'origine da verificare sul prodotto OPERA
    specifico prima di considerare definitive le coordinate.
    """
    width = grid["width"]
    height = grid["height"]

    cols = np.arange(width, dtype=np.float64)
    rows = np.arange(height, dtype=np.float64)

    # Griglia regolare con centro del pixel a metà risoluzione.
    # Le coordinate y crescono dal basso verso l'alto.
    x = grid["xmin"] + (cols + 0.5) * grid["xscale"]
    y = grid["ymax"] - (rows + 0.5) * grid["yscale"]

    # Verifica coerenza tra area dichiarata e risoluzione.
    expected_xmax = grid["xmin"] + width * grid["xscale"]
    expected_ymin = grid["ymax"] - height * grid["yscale"]

    tolerance_x = max(grid["xscale"] * 1.1, 1e-6)
    tolerance_y = max(grid["yscale"] * 1.1, 1e-6)

    if abs(expected_xmax - grid["xmax"]) > tolerance_x:
        raise ValueError(
            "Estensione X incompatibile con risoluzione e dimensioni."
        )

    if abs(expected_ymin - grid["ymin"]) > tolerance_y:
        raise ValueError(
            "Estensione Y incompatibile con risoluzione e dimensioni."
        )

    xx, yy = np.meshgrid(x, y)
    lon, lat = transformer.transform(xx, yy)

    return lat, lon


def connected_regions(mask):
    """
    Componenti connesse 8-neighbourhood.
    Implementazione senza dipendenze da scipy.
    """
    height, width = mask.shape
    visited = np.zeros(mask.shape, dtype=bool)
    regions = []

    for row in range(height):
        for col in range(width):
            if not mask[row, col] or visited[row, col]:
                continue

            stack = [(row, col)]
            visited[row, col] = True
            pixels = []

            while stack:
                r, c = stack.pop()
                pixels.append((r, c))

                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        if dr == 0 and dc == 0:
                            continue

                        rr = r + dr
                        cc = c + dc

                        if (
                            0 <= rr < height
                            and 0 <= cc < width
                            and mask[rr, cc]
                            and not visited[rr, cc]
                        ):
                            visited[rr, cc] = True
                            stack.append((rr, cc))

            if len(pixels) >= MIN_REGION_PIXELS:
                regions.append(pixels)

    return regions


def safe_float(value):
    value = float(value)
    return round(value, 4) if math.isfinite(value) else None


def make_cells(dbzh, valid, lat_grid, lon_grid):
    mask = valid & (dbzh >= MIN_DBZ)
    regions = connected_regions(mask)
    cells = []

    for index, pixels in enumerate(regions, start=1):
        rows = np.array([p[0] for p in pixels], dtype=int)
        cols = np.array([p[1] for p in pixels], dtype=int)

        values = dbzh[rows, cols]
        finite = np.isfinite(values)

        rows = rows[finite]
        cols = cols[finite]
        values = values[finite]

        if len(values) < MIN_REGION_PIXELS:
            continue

        weights = np.maximum(values, 0.01)

        # Centroide ponderato per riflettività.
        centroid_lat = float(np.average(lat_grid[rows, cols], weights=weights))
        centroid_lon = float(np.average(lon_grid[rows, cols], weights=weights))

        local_max = float(np.max(values))

        # Matrice locale rettangolare della regione.
        # I pixel fuori dalla regione sono null, non zero dBZ.
        r0, r1 = int(rows.min()), int(rows.max())
        c0, c1 = int(cols.min()), int(cols.max())

        region_matrix = dbzh[r0:r1+1, c0:c1+1].copy()
        region_mask = np.zeros(region_matrix.shape, dtype=bool)

        region_mask[rows-r0, cols-c0] = True
        region_matrix[~region_mask] = np.nan

        # JSON non può rappresentare NaN: usa null.
        matrix_json = [
            [
                round(float(v), 1) if math.isfinite(float(v)) else None
                for v in row
            ]
            for row in region_matrix
        ]

        cell = {
            "id": f"OPERA-{index:05d}",
            "lat": round(centroid_lat, 6),
            "lon": round(centroid_lon, 6),
            "max_dbz": round(local_max, 1),
            "grid_matrix": matrix_json,
            "pixel_count": int(len(values)),
            "source_parameter": "DBZH",
            "source_units": "dBZ",
            "measurements_status": "decoded_radar_reflectivity",
            "centroid_method": "DBZH_weighted_pixel_centroid",
            "severity": {
                "max_dbzh": round(local_max, 1),
                "vil": None,
                "echo_top": None,
                "rain_mmh": None,
            },
            "geometry": {
                "row_min": r0,
                "row_max": r1,
                "col_min": c0,
                "col_max": c1,
            },
        }

        cells.append(cell)

    return cells


def decode_file(path):
    with path.open("rb") as file:
        signature = file.read(8)

    if signature != ODIM_SIGNATURE:
        raise ValueError(f"File non HDF5/ODIM valido: {path}")

    with h5py.File(path, "r") as handle:
        dataset_info = find_dbzh_dataset(handle)
        dataset = dataset_info["dataset"]
        data_group = dataset_info["parent"]
        dataset_group = dataset_info["dataset_group"]

        if len(dataset.shape) != 2:
            raise ValueError(
                f"Dataset DBZH non bidimensionale: {dataset.shape}"
            )

        height, width = dataset.shape
        total_pixels = int(height) * int(width)

        if total_pixels > MAX_PIXELS:
            raise ValueError(
                f"Raster troppo grande: {total_pixels} pixel."
            )

        raw = dataset[()]
        dbzh, valid = read_calibration(data_group, raw)

        if not np.any(valid):
            raise ValueError("Il raster non contiene pixel DBZH validi.")

        transformer = read_projection(handle, dataset_group)
        grid = read_grid_metadata(handle, dataset_group, dbzh.shape)
        lat_grid, lon_grid = pixel_centres_to_lonlat(
            transformer, grid
        )

        valid &= np.isfinite(lat_grid) & np.isfinite(lon_grid)
        dbzh[~valid] = np.nan

        # Se il raster è troppo grande, non viene ridotto in silenzio:
        # la segmentazione deve conservare i pixel originali.
        if total_pixels > MAX_OUTPUT_PIXELS:
            raise ValueError(
                "Raster superiore al limite di output. "
                "Serve un decoder a finestre/tile per mantenere "
                "la risoluzione senza consumare troppa memoria."
            )

        cells = make_cells(dbzh, valid, lat_grid, lon_grid)

        product_date = (
            read_group_attr(handle, "what", "date")
            or read_group_attr(handle, "what", "startdate")
        )

        product_time = (
            read_group_attr(handle, "what", "time")
            or read_group_attr(handle, "what", "starttime")
        )

        product_timestamp = None

        if product_date and product_time:
            try:
                parsed = datetime.strptime(
                    str(product_date) + str(product_time),
                    "%Y%m%d%H%M%S"
                ).replace(tzinfo=timezone.utc)

                product_timestamp = parsed.isoformat()
            except ValueError:
                product_timestamp = None

        return {
            "product": "I.R.I.D.E.",
            "product_name": (
                "Integrated Radar Imaging & Detection Engine"
            ),
            "timestamp": product_timestamp or utc_now(),
            "generated_at": utc_now(),
            "source_name": "EUMETNET OPERA ODIM HDF5",
            "source_file": str(path.relative_to(ROOT)),
            "source_parameter": "DBZH",
            "source_units": "dBZ",
            "source_product_timestamp": product_timestamp,
            "cell_count": len(cells),
            "processing": {
                "decoder": "decode_opera.py",
                "threshold_dbz": MIN_DBZ,
                "minimum_region_pixels": MIN_REGION_PIXELS,
                "vertical_structure": "not_available_from_single_2D_composite",
                "vil": "not_calculated",
                "echo_top": "not_calculated",
                "rain_rate": "not_calculated",
            },
            "cells": cells,
        }


def main():
    if not RAW_DIR.exists():
        raise FileNotFoundError(
            f"Cartella dati radar assente: {RAW_DIR}"
        )

    files = sorted(
        RAW_DIR.glob("opera_dbzh_*.h5"),
        key=lambda item: item.stat().st_mtime,
        reverse=True
    )

    if not files:
        raise FileNotFoundError(
            "Nessun file opera_dbzh_*.h5 in data/raw."
        )

    errors = []

    for path in files:
        try:
            result = decode_file(path)

            OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary = OUTPUT_FILE.with_suffix(".json.tmp")

            with temporary.open("w", encoding="utf-8") as file:
                json.dump(
                    result,
                    file,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False
                )
                file.write("\n")

            temporary.replace(OUTPUT_FILE)

            print("Decoder OPERA completato.")
            print(f"File sorgente: {path.name}")
            print(f"Timestamp prodotto: {result['timestamp']}")
            print(f"Celle individuate: {result['cell_count']}")
            print(f"Output: {OUTPUT_FILE}")
            return 0

        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
            print(
                f"File non elaborabile {path.name}: {exc}",
                file=sys.stderr
            )

    raise RuntimeError(
        "Nessun file radar decodificabile.\n" + "\n".join(errors)
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERRORE DECODER OPERA: {exc}", file=sys.stderr)
        sys.exit(1)
      
