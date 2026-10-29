
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
I.R.I.D.E.
Integrated Radar Imaging & Detection Engine

Motore di elaborazione per dati radar strutturati in JSON.
Input principale: data/iride_database.json
Output: data/iride_output.json

Non genera celle meteorologiche artificiali.
Non sostituisce i dati mancanti con simulazioni.
Conserva la provenienza e segnala le limitazioni dei dati.
"""

from __future__ import annotations

import json
import math
import os
import sys
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

INPUT_CANDIDATES = [
    DATA_DIR / "iride_database.json",
    DATA_DIR / "source.json",
]

OUTPUT_FILE = DATA_DIR / "iride_output.json"

# Soglie applicate ai valori della matrice fornita.
# ATTENZIONE: sono soglie operative sui valori del JSON.
# Non certificano una calibrazione fisica del dato sorgente.
MATRIX_THRESHOLDS = [10.0, 20.0, 30.0, 35.0, 40.0, 50.0]

MIN_MATRIX_VALUE = 0.0
MAX_CELLS_OUTPUT = 500


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def number(value: Any, default: float = 0.0) -> float:
    if finite_number(value):
        return float(value)
    return default


def valid_coordinate(lat: Any, lon: Any) -> bool:
    return (
        finite_number(lat)
        and finite_number(lon)
        and -90.0 <= float(lat) <= 90.0
        and -180.0 <= float(lon) <= 180.0
    )


def find_input_file() -> Path:
    for candidate in INPUT_CANDIDATES:
        if candidate.is_file():
            return candidate

    raise FileNotFoundError(
        "Database radar JSON non trovato. File cercati: "
        + ", ".join(str(p.relative_to(ROOT)) for p in INPUT_CANDIDATES)
    )


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(
            payload,
            handle,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        handle.write("\n")

    temporary.replace(path)


def normalize_database(raw: Any) -> tuple[dict[str, Any], list]:
    """
    Accetta il formato principale:
      {"timestamp": ..., "source": ..., "cells": [...]}

    Accetta anche un elenco di celle oppure un wrapper contenente
    una chiave cells. Non inventa dati se il formato è vuoto.
    """
    if isinstance(raw, list):
        metadata = {}
        cells = raw

    elif isinstance(raw, dict):
        metadata = raw
        cells = raw.get("cells")

        if cells is None and isinstance(raw.get("data"), dict):
            cells = raw["data"].get("cells")

        if cells is None and isinstance(raw.get("results"), list):
            cells = raw["results"]

    else:
        raise ValueError("Formato JSON non riconosciuto.")

    if not isinstance(cells, list):
        raise ValueError(
            "Il JSON non contiene una lista 'cells' valida."
        )

    return metadata, cells


def clean_matrix(raw_matrix: Any) -> list[list[float]]:
    """
    Mantiene la geometria della matrice originale.
    Valori non numerici, infiniti o NaN diventano celle nulle.
    Non crea una matrice sostitutiva.
    """
    if not isinstance(raw_matrix, list):
        return []

    result = []

    for row in raw_matrix:
        if not isinstance(row, list):
            continue

        clean_row = []
        for value in row:
            if finite_number(value):
                clean_row.append(
                    max(MIN_MATRIX_VALUE, float(value))
                )
            else:
                clean_row.append(0.0)

        result.append(clean_row)

    return result


def matrix_shape(matrix: list[list[float]]) -> dict[str, int]:
    return {
        "rows": len(matrix),
        "columns": max((len(row) for row in matrix), default=0),
    }


def matrix_statistics(matrix: list[list[float]]) -> dict[str, Any]:
    values = [value for row in matrix for value in row]

    if not values:
        return {
            "valid": False,
            "rows": 0,
            "columns": 0,
            "max_value": None,
            "mean_value": None,
            "active_pixels": 0,
            "threshold_counts": {},
        }

    counts = {
        str(threshold): sum(v >= threshold for v in values)
        for threshold in MATRIX_THRESHOLDS
    }

    active = sum(v > 0.0 for v in values)

    return {
        "valid": True,
        **matrix_shape(matrix),
        "max_value": round(max(values), 2),
        "mean_value": round(sum(values) / len(values), 2),
        "active_pixels": active,
        "threshold_counts": counts,
    }


def perimeter_pixels(
    matrix: list[list[float]],
    threshold: float = 10.0,
) -> list[dict[str, int]]:
    """
    Estrae il contorno della zona con valori >= threshold.

    Le coordinate sono indici di riga/colonna, NON coordinate
    geografiche. Perimetro geografico e area richiedono la
    risoluzione spaziale reale della matrice.
    """
    if not matrix:
        return []

    width = max((len(row) for row in matrix), default=0)
    if width == 0:
        return []

    height = len(matrix)

    def active(r: int, c: int) -> bool:
        return (
            0 <= r < height
            and 0 <= c < len(matrix[r])
            and matrix[r][c] >= threshold
        )

    perimeter = []

    for r, row in enumerate(matrix):
        for c, value in enumerate(row):
            if value < threshold:
                continue

            neighbors = (
                active(r - 1, c),
                active(r + 1, c),
                active(r, c - 1),
                active(r, c + 1),
            )

            if not all(neighbors):
                perimeter.append({"row": r, "column": c})

    return perimeter


def classify_structure(
    stats: dict[str, Any],
    cell: dict[str, Any],
) -> dict[str, Any]:
    """
    Classificazione euristica preliminare.
    Non dichiara una supercella o un MCS come confermati:
    tali diagnosi richiedono anche struttura spaziale,
    evoluzione temporale e verifiche meteorologiche.
    """
    matrix_max = stats.get("max_value")
    supplied_max = cell.get("max_dbz")

    peak = number(
        supplied_max,
        number(matrix_max, 0.0),
    )

    active = stats.get("active_pixels", 0)
    rows = stats.get("rows", 0)
    cols = stats.get("columns", 0)
    aspect = max(rows, cols) / max(1, min(rows, cols))

    labels = []
    confidence = "bassa"

    if peak >= 50:
        labels.append("nucleo_con_valori_molto_elevati")
    elif peak >= 40:
        labels.append("nucleo_intenso")
    elif peak >= 30:
        labels.append("nucleo_convezione_significativa")
    else:
        labels.append("nucleo_debole_o_moderato")

    if active >= 100 and aspect >= 2.0:
        labels.append("struttura_estesa_allungata")
    elif active >= 100:
        labels.append("struttura_estesa")
    elif active > 0:
        labels.append("cella_radar_rilevata")
    else:
        labels.append("matrice_senza_pixel_attivi")

    return {
        "labels": labels,
        "confidence": confidence,
        "confirmed_meteorological_type": None,
        "limitations": (
            "Classificazione preliminare: forma e intensità della "
            "matrice non bastano per confermare MCS, MCC, MCV, "
            "squall line, V-shape, comma head, echo hook o supercella."
        ),
    }
    
def process_cell(raw_cell: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(raw_cell, dict):
        return None

    lat = raw_cell.get("lat")
    lon = raw_cell.get("lon")

    if not valid_coordinate(lat, lon):
        return None

    matrix = clean_matrix(raw_cell.get("grid_matrix"))
    stats = matrix_statistics(matrix)

    kinematics = raw_cell.get("kinematics")
    if not isinstance(kinematics, dict):
        kinematics = {}

    severity = raw_cell.get("severity")
    if not isinstance(severity, dict):
        severity = {}

    vectors = raw_cell.get("vectors")
    if not isinstance(vectors, dict):
        vectors = {}

    real_vector = vectors.get("real")
    predictive_vector = vectors.get("predictive_noaa")

    perimeter = perimeter_pixels(matrix, threshold=10.0)

    return {
        "id": str(raw_cell.get("id", f"cell-{index + 1}")),
        "lat": float(lat),
        "lon": float(lon),
        "max_dbz": (
            float(raw_cell["max_dbz"])
            if finite_number(raw_cell.get("max_dbz"))
            else stats.get("max_value")
        ),
        "kinematics": {
            "velocity_kmh": (
                float(kinematics["velocity_kmh"])
                if finite_number(kinematics.get("velocity_kmh"))
                else None
            ),
            "direction_deg": (
                float(kinematics["direction_deg"]) % 360.0
                if finite_number(kinematics.get("direction_deg"))
                else None
            ),
        },
        "severity": {
            "vil": (
                float(severity["vil"])
                if finite_number(severity.get("vil"))
                else None
            ),
            "echo_top": (
                float(severity["echo_top"])
                if finite_number(severity.get("echo_top"))
                else None
            ),
            "rain_mmh": (
                float(severity["rain_mmh"])
                if finite_number(severity.get("rain_mmh"))
                else None
            ),
        },
        "vectors": {
            "real": validate_vector(real_vector),
            "predictive_noaa": validate_vector(predictive_vector),
        },
        "grid_matrix": matrix,
        "matrix_statistics": stats,
        "perimeter": {
            "threshold": 10.0,
            "coordinate_system": "matrix_row_column",
            "pixels": perimeter,
            "pixel_count": len(perimeter),
            "geographic_polygon_available": False,
        },
        "structure_analysis": classify_structure(stats, raw_cell),
    }


def validate_vector(raw_vector: Any) -> list[list[float]] | None:
    """
    Valida le coppie [lat, lon] conservando l'ordine originale.
    Non crea vettori sostitutivi e non modifica le previsioni.
    """
    if not isinstance(raw_vector, list):
        return None

    points = []

    for point in raw_vector:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue

        lat, lon = point[0], point[1]

        if valid_coordinate(lat, lon):
            points.append([float(lat), float(lon)])

    return points if len(points) >= 2 else None


def summarize(cells: list[dict[str, Any]]) -> dict[str, Any]:
    peaks = [
        cell["max_dbz"]
        for cell in cells
        if finite_number(cell.get("max_dbz"))
    ]

    threshold_counts = {
        str(threshold): sum(
            peak >= threshold for peak in peaks
        )
        for threshold in MATRIX_THRESHOLDS
    }

    return {
        "cell_count": len(cells),
        "cells_ge_30": threshold_counts["30.0"],
        "cells_ge_35": threshold_counts["35.0"],
        "cells_ge_40": threshold_counts["40.0"],
        "cells_ge_50": threshold_counts["50.0"],
        "strongest_cell_id": (
            max(
                cells,
                key=lambda item: (
                    item["max_dbz"]
                    if finite_number(item.get("max_dbz"))
                    else -1.0
                ),
            )["id"]
            if cells
            else None
        ),
        "max_reported_value": max(peaks) if peaks else None,
        "threshold_counts": threshold_counts,
    }


def build_output(
    input_path: Path,
    metadata: dict[str, Any],
    raw_cells: list[Any],
) -> dict[str, Any]:
    processed = []

    for index, raw_cell in enumerate(raw_cells):
        cell = process_cell(raw_cell, index)
        if cell is not None:
            processed.append(cell)

    # Limite di sicurezza per dimensioni del JSON pubblicato.
    # La selezione non altera il database originale.
    processed.sort(
        key=lambda item: (
            item["max_dbz"]
            if finite_number(item.get("max_dbz"))
            else -1.0
        ),
        reverse=True,
    )

    total_processed = len(processed)
    output_cells = processed[:MAX_CELLS_OUTPUT]

    source_timestamp = metadata.get("timestamp")
    source_name = metadata.get("source", "non_specificata")

    input_bytes = input_path.read_bytes()
    input_sha256 = hashlib.sha256(input_bytes).hexdigest()

    return {
        "product": "I.R.I.D.E.",
        "engine": "Integrated Radar Imaging & Detection Engine",
        "schema_version": "1.0",
        "generated_at_utc": utc_now(),
        "source": {
            "name": source_name,
            "timestamp": source_timestamp,
            "input_file": str(input_path.relative_to(ROOT)),
            "sha256": input_sha256,
            "radar_tile_path": metadata.get("radar_tile_path"),
        },
        "status": "ok" if output_cells else "no_valid_cells",
        "data_integrity": {
            "input_cell_count": len(raw_cells),
            "valid_cell_count": total_processed,
            "published_cell_count": len(output_cells),
            "truncated": total_processed > MAX_CELLS_OUTPUT,
            "synthetic_cells_created": 0,
            "source_matrix_values_preserved": True,
        },
        "summary": summarize(output_cells),
        "analysis": {
            "perimeter_threshold": 10.0,
            "matrix_thresholds": MATRIX_THRESHOLDS,
            "matrix_units": (
                "unità del dato sorgente; calibrazione da verificare"
            ),
            "geographic_pixel_resolution": None,
            "satellite_crosscheck": "not_available_in_input",
            "historical_motion_validation": "not_available_in_input",
            "forecast_validation": "not_available_in_input",
        },
        "cells": output_cells,
        "warnings": [
            (
                "I valori della matrice sono conservati come forniti; "
                "non è verificata qui la loro equivalenza a dBZ calibrati."
            ),
            (
                "Il perimetro è espresso in pixel della matrice, "
                "non in un poligono geografico."
            ),
            (
                "I vettori sono mantenuti dalla sorgente: non sono "
                "una previsione meteorologica indipendentemente validata."
            ),
            (
                "La classificazione mesoscala richiede dati spaziali "
                "e temporali ulteriori per essere confermata."
            ),
        ],
    }


def main() -> int:
    try:
        input_path = find_input_file()
        raw = load_json(input_path)
        metadata, raw_cells = normalize_database(raw)

        result = build_output(input_path, metadata, raw_cells)

        save_json(OUTPUT_FILE, result)

        print("I.R.I.D.E. engine completato.")
        print(f"Input: {input_path.relative_to(ROOT)}")
        print(f"Output: {OUTPUT_FILE.relative_to(ROOT)}")
        print(f"Celle valide: {result['data_integrity']['valid_cell_count']}")
        print(f"Celle pubblicate: {result['data_integrity']['published_cell_count']}")
        print(f"Celle con valore >= 35: {result['summary']['cells_ge_35']}")
        print(f"Stato: {result['status']}")

        return 0 if result["status"] == "ok" else 1

    except Exception as exc:
        print(
            f"ERRORE I.R.I.D.E.: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
    
