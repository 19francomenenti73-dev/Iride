#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
I.R.I.D.E.
Integrated Radar Imaging & Detection Engine

Elabora dati radar già acquisiti.
Non genera celle meteorologiche simulate.
Non dichiara misure derivate come osservazioni dirette.
Le previsioni sono estrapolazioni cinematiche, non previsioni NOAA.
"""

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

INPUT_FILE = ROOT / "data" / "iride_database.json"
HISTORY_FILE = ROOT / "data" / "iride_history.json"
OUTPUT_FILE = ROOT / "data" / "iride_output.json"
PUBLIC_FILE = ROOT / "docs" / "data" / "iride.json"

MAX_HISTORY = 500
MIN_TIME_DELTA_SECONDS = 1


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None

    try:
        normalized = value.strip().replace("Z", "+00:00")
        result = datetime.fromisoformat(normalized)

        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)

        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def valid_coordinate(lat, lon):
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return False

    return (
        math.isfinite(lat)
        and math.isfinite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )


def read_json(path, default=None):
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Impossibile leggere {path}: {exc}"
        ) from exc


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_suffix(path.suffix + ".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2,
            allow_nan=False
        )
        file.write("\n")

    temporary.replace(path)


def cell_id(cell):
    value = cell.get("id")

    if value is None:
        return None

    value = str(value).strip()
    return value or None


def matrix_statistics(matrix):
    if not isinstance(matrix, list):
        return {
            "rows": 0,
            "columns": 0,
            "numeric_cells": 0,
            "matrix_max": None,
        }

    values = []
    max_columns = 0

    for row in matrix:
        if not isinstance(row, list):
            continue

        max_columns = max(max_columns, len(row))

        for item in row:
            if isinstance(item, bool):
                continue

            try:
                value = float(item)
            except (TypeError, ValueError):
                continue

            if math.isfinite(value):
                values.append(value)

    return {
        "rows": len(matrix),
        "columns": max_columns,
        "numeric_cells": len(values),
        "matrix_max": max(values) if values else None,
    }


def normalize_history(history):
    if not isinstance(history, dict):
        history = {}

    history.setdefault("tracks", {})

    if not isinstance(history["tracks"], dict):
        history["tracks"] = {}

    return history


def add_observation(history, cell, timestamp):
    identifier = cell_id(cell)

    if not identifier:
        return

    if not valid_coordinate(cell.get("lat"), cell.get("lon")):
        return

    dt = parse_time(timestamp)

    if dt is None:
        return

    tracks = history["tracks"]
    observations = tracks.setdefault(identifier, [])

    if not isinstance(observations, list):
        observations = []
        tracks[identifier] = observations

    observation = {
        "timestamp": dt.isoformat(),
        "lat": float(cell["lat"]),
        "lon": float(cell["lon"]),
    }

    # Non aggiunge due volte la stessa osservazione.
    for old in observations:
        if not isinstance(old, dict):
            continue

        old_dt = parse_time(old.get("timestamp"))

        if old_dt == dt:
            # Aggiorna solo se la posizione coincide.
            # Una posizione differente con lo stesso timestamp
            # non viene accettata come seconda osservazione.
            try:
                same_position = (
                    abs(float(old["lat"]) - observation["lat"]) < 1e-7
                    and abs(float(old["lon"]) - observation["lon"]) < 1e-7
                )
            except (KeyError, TypeError, ValueError):
                same_position = False

            if same_position:
                return

            return

    observations.append(observation)

    observations.sort(
        key=lambda item: parse_time(item["timestamp"])
        or datetime.min.replace(tzinfo=timezone.utc)
    )

    tracks[identifier] = observations[-MAX_HISTORY:]


def distance_bearing(a, b):
    """Restituisce distanza in km e direzione in gradi."""
    lat1 = math.radians(a["lat"])
    lat2 = math.radians(b["lat"])
    dlat = lat2 - lat1
    dlon = math.radians(b["lon"] - a["lon"])

    hav = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2)
        * math.sin(dlon / 2) ** 2
    )

    hav = max(0.0, min(1.0, hav))
    distance = 6371.0088 * 2 * math.asin(math.sqrt(hav))

    y = math.sin(dlon) * math.cos(lat2)
    x = (
        math.cos(lat1) * math.sin(lat2)
        - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    )

    bearing = (math.degrees(math.atan2(y, x)) + 360) % 360

    return distance, bearing


def project_position(previous, latest, hours):
    """
    Estrapola il moto osservato con velocità angolare locale.
    È una stima lineare, non una previsione meteorologica completa.
    """
    t0 = parse_time(previous["timestamp"])
    t1 = parse_time(latest["timestamp"])

    if t0 is None or t1 is None:
        return None

    elapsed_seconds = (t1 - t0).total_seconds()

    if elapsed_seconds < MIN_TIME_DELTA_SECONDS:
        return None

    distance_km, bearing = distance_bearing(previous, latest)

    speed_kmh = distance_km / (elapsed_seconds / 3600.0)

    if not math.isfinite(speed_kmh) or speed_kmh > 250:
        return None

    future_distance = speed_kmh * hours

    if not math.isfinite(future_distance):
        return None

    angular_distance = future_distance / 6371.0088
    bearing_rad = math.radians(bearing)

    lat1 = math.radians(latest["lat"])
    lon1 = math.radians(latest["lon"])

    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular_distance)
        + math.cos(lat1) * math.sin(angular_distance)
        * math.cos(bearing_rad)
    )

    lon2 = lon1 + math.atan2(
        math.sin(bearing_rad) * math.sin(angular_distance)
        * math.cos(lat1),
        math.cos(angular_distance)
        - math.sin(lat1) * math.sin(lat2)
    )

    lat = math.degrees(lat2)
    lon = (math.degrees(lon2) + 540) % 360 - 180

    if not valid_coordinate(lat, lon):
        return None

    return [round(lat, 6), round(lon, 6)]


def build_track(identifier, history):
    observations = history["tracks"].get(identifier, [])

    if not isinstance(observations, list) or len(observations) < 2:
        return {
            "real_vector": None,
            "forecast_3h": None,
            "forecast_6h": None,
            "observation_count": len(observations)
            if isinstance(observations, list) else 0,
            "forecast_method": "unavailable_insufficient_history",
        }

    previous, latest = observations[-2], observations[-1]

    t0 = parse_time(previous.get("timestamp"))
    t1 = parse_time(latest.get("timestamp"))

    if (
        t0 is None
        or t1 is None
        or (t1 - t0).total_seconds() < MIN_TIME_DELTA_SECONDS
    ):
        return {
            "real_vector": None,
            "forecast_3h": None,
            "forecast_6h": None,
            "observation_count": len(observations),
            "forecast_method": "unavailable_invalid_timestamps",
        }

    start = [float(previous["lat"]), float(previous["lon"])]
    end = [float(latest["lat"]), float(latest["lon"])]

    p3 = project_position(previous, latest, 3)
    p6 = project_position(previous, latest, 6)

    return {
        "real_vector": [start, end],
        "forecast_3h": [end, p3] if p3 else None,
        "forecast_6h": [end, p6] if p6 else None,
        "observation_count": len(observations),
        "last_observation_utc": latest["timestamp"],
        "forecast_method": "linear_motion_extrapolation",
        "forecast_warning": (
            "Estrapolazione cinematica; non include evoluzione, "
            "dissipazione, nuove celle o interazione con altri sistemi."
        ),
    }


def process():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Database assente: {INPUT_FILE}\n"
            "Il motore non inventa dati per sostituirlo."
        )

    source = read_json(INPUT_FILE)

    if not isinstance(source, dict):
        raise ValueError("Il database deve contenere un oggetto JSON.")

    cells = source.get("cells")

    if not isinstance(cells, list):
        raise ValueError(
            "Il database JSON deve contenere un array 'cells'."
        )

    timestamp = (
        source.get("timestamp")
        or source.get("generated_at")
        or source.get("updated_at")
    )

    if not isinstance(timestamp, str) or parse_time(timestamp) is None:
        raise ValueError(
            "Timestamp del database assente o non valido. "
            "Non posso registrare un'osservazione temporale affidabile."
        )

    history = normalize_history(read_json(HISTORY_FILE, {}))

    valid_cells = []
    rejected = 0

    for original in cells:
        if not isinstance(original, dict):
            rejected += 1
            continue

        if not cell_id(original):
            rejected += 1
            continue

        if not valid_coordinate(
            original.get("lat"),
            original.get("lon")
        ):
            rejected += 1
            continue

        cell = dict(original)

        # Non sovrascrive i valori dichiarati dalla sorgente.
        cell["matrix_stats"] = matrix_statistics(
            cell.get("grid_matrix")
        )

        add_observation(history, cell, timestamp)

        cell["track"] = build_track(cell["id"], history)

        valid_cells.append(cell)

    result = {
        "product": "I.R.I.D.E.",
        "product_name": "Integrated Radar Imaging & Detection Engine",
        "timestamp": timestamp,
        "processed_at_utc": utc_now(),
        "source_timestamp": timestamp,
        "source_name": source.get("source_name", "unknown"),
        "source_status": "processed_input",
        "cell_count": len(valid_cells),
        "rejected_cell_count": rejected,
        "vector_policy": (
            "Vettori calcolati solo da osservazioni storiche distinte. "
            "Le previsioni sono estrapolazioni cinematiche."
        ),
        "cells": valid_cells,
    }

    # Scrittura degli output e dello storico.
    write_json(HISTORY_FILE, history)
    write_json(OUTPUT_FILE, result)
    write_json(PUBLIC_FILE, result)

    print("I.R.I.D.E. engine completato")
    print(f"Celle valide: {len(valid_cells)}")
    print(f"Celle scartate: {rejected}")
    print(f"Input: {INPUT_FILE}")
    print(f"Output: {OUTPUT_FILE}")
    print(f"Output pubblico: {PUBLIC_FILE}")
    print(f"Storico: {HISTORY_FILE}")


if __name__ == "__main__":
    try:
        process()
    except Exception as exc:
        print(f"ERRORE I.R.I.D.E.: {exc}", file=sys.stderr)
        sys.exit(1)
