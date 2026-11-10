#!/usr/bin/env python3
"""I.R.I.D.E. - Geometria e classificazione mesoscala 2D.

Input:
    mask: matrice booleana di un eco connesso.
    dbz: riflettivita radar nella stessa finestra.
    xscale_m, yscale_m: dimensioni pixel in metri.

Output:
    classificazione candidata, indicatori geometrici
    e quattro vertici del rettangolo orientato.

Non modifica la ricostruzione volumetrica 3D.
"""

from __future__ import annotations

import math

import numpy as np
from scipy import ndimage


LABELS = {
    "isolated_cell_candidate": "Cella isolata candidata",
    "multicell_candidate": "Multicella candidata",
    "squall_line_candidate": "Squall Line candidata",
    "mcs_candidate": "MCS candidato",
    "mcc_candidate": "MCC candidato",
    "bow_echo_candidate": "Bow Echo candidato",
    "hook_echo_candidate": "Hook Echo candidato",
    "mcv_candidate": "MCV candidato",
    "supercell_candidate": "Supercella candidata",
    "linear_echo_candidate": "Eco lineare candidato",
    "unclassified_echo": "Eco non classificato",
}


def _axes(mask, xscale_m, yscale_m):
    yy, xx = np.nonzero(mask)

    if len(xx) == 0:
        return None

    points = np.column_stack((
        xx.astype(float) * xscale_m,
        yy.astype(float) * yscale_m,
    ))

    center = points.mean(axis=0)

    if len(points) < 3:
        basis = np.eye(2)
    else:
        covariance = np.cov((points - center).T)
        values, vectors = np.linalg.eigh(covariance)
        basis = vectors[:, np.argsort(values)[::-1]]

    projected = (points - center) @ basis

    return center, basis, projected


def _oriented_box(mask, xscale_m, yscale_m):
    result = _axes(mask, xscale_m, yscale_m)

    if result is None:
        return [], 0.0, 0.0, 1.0

    center, basis, projected = result

    low = projected.min(axis=0)
    high = projected.max(axis=0)

    low -= [xscale_m / 2, yscale_m / 2]
    high += [xscale_m / 2, yscale_m / 2]

    corners = np.array([
        [low[0], low[1]],
        [high[0], low[1]],
        [high[0], high[1]],
        [low[0], high[1]],
    ])

    physical = center + corners @ basis.T

    pixels = np.column_stack((
        physical[:, 0] / xscale_m,
        physical[:, 1] / yscale_m,
    ))

    length = float(high[0] - low[0])
    width = float(high[1] - low[1])

    return (
        np.round(pixels, 4).tolist(),
        length / 1000.0,
        width / 1000.0,
        length / max(width, 1.0),
    )


def _convective_cores(mask, dbz):
    strong = mask & np.isfinite(dbz) & (dbz >= 40.0)

    labels, count = ndimage.label(
        strong,
        structure=np.ones((3, 3), dtype=np.uint8),
    )

    if count == 0:
        return 0

    sizes = np.bincount(labels[labels > 0])
    return int(np.count_nonzero(sizes >= 4))


def _curvature(mask, xscale_m, yscale_m):
    result = _axes(mask, xscale_m, yscale_m)

    if result is None:
        return 0.0

    _, _, projected = result
    u = projected[:, 0]
    v = projected[:, 1]

    if np.ptp(u) < 10000:
        return 0.0

    edges = np.linspace(u.min(), u.max(), 8)
    centers = []

    for i in range(7):
        selected = (u >= edges[i]) & (u < edges[i + 1])

        if np.count_nonzero(selected) < 4:
            return 0.0

        centers.append(float(np.median(v[selected])))

    centers = np.asarray(centers)
    baseline = np.linspace(centers[0], centers[-1], 7)
    departure = centers - baseline

    middle = abs(float(np.mean(departure[2:5])))
    outside = abs(float(np.mean(departure[[0, 1, 5, 6]])))

    amplitude = max(0.0, middle - outside)
    transverse = max(float(np.ptp(v)), 1000.0)

    return float(np.clip(amplitude / (transverse * 0.4), 0, 1))
  
def analyze_structure(
    mask,
    dbz,
    xscale_m,
    yscale_m,
    history=None,
):
    """Analizza un eco radar senza inferire dati verticali.

    history e' riservato a eventuali criteri temporali
    verificati. Non viene usato per inventare persistenza.
    """
    mask = np.asarray(mask, dtype=bool)
    dbz = np.asarray(dbz, dtype=float)

    if mask.ndim != 2 or dbz.shape != mask.shape:
        raise ValueError("Mask e DBZH devono avere forma 2D identica")

    if not (
        math.isfinite(xscale_m)
        and math.isfinite(yscale_m)
        and xscale_m > 0
        and yscale_m > 0
    ):
        raise ValueError("Risoluzione spaziale non valida")

    if not np.any(mask):
        return {
            "class": "unclassified_echo",
            "label": LABELS["unclassified_echo"],
            "oriented_box_pixels": [],
            "status": "empty_echo",
        }

    corners, length_km, width_km, elongation = (
        _oriented_box(mask, xscale_m, yscale_m)
    )

    area_pixels = int(np.count_nonzero(mask))
    area_km2 = (
        area_pixels * xscale_m * yscale_m / 1_000_000.0
    )

    cores = _convective_cores(mask, dbz)
    curvature = _curvature(mask, xscale_m, yscale_m)

    valid = dbz[mask & np.isfinite(dbz)]
    max_dbz = float(valid.max()) if valid.size else 0.0

    classification = "unclassified_echo"
    evidence = []

    # I criteri qui sotto sono filtri morfologici
    # esplorativi, non definizioni operative di tempesta.

    if cores <= 1 and area_km2 < 400:
        classification = "isolated_cell_candidate"
        evidence.append("limited_area_and_core_count")

    if cores >= 2:
        classification = "multicell_candidate"
        evidence.append("multiple_convective_cores")

    if elongation >= 3.0 and length_km >= 50:
        classification = "linear_echo_candidate"
        evidence.append("elongated_reflectivity_structure")

    if (
        elongation >= 3.0
        and length_km >= 80
        and cores >= 3
    ):
        classification = "squall_line_candidate"
        evidence.append("long_multicore_linear_echo")

    if (
        elongation >= 2.0
        and length_km >= 40
        and curvature >= 0.65
    ):
        classification = "bow_echo_candidate"
        evidence.append("curved_reflectivity_axis")

    if (
        area_km2 >= 10000
        and cores >= 3
        and elongation < 3.0
    ):
        classification = "mcs_candidate"
        evidence.append("large_multicore_echo_cluster")

    # MCC: la forma e l'estensione radar non bastano.
    # Servono criteri satellitari IR e durata.
    # Non assegnare MCC senza tali osservazioni.

    # Hook Echo: una semplice curvatura non prova
    # un hook associato a mesociclone.
    # Non assegnare senza verifica della firma.

    # MCV: richiede evidenza di rotazione coerente.
    # Un composito DBZH non la fornisce.

    # Supercella: richiede evidenze dinamiche,
    # idealmente Doppler e persistenza temporale.
    # Non dedurla dal solo valore massimo DBZH.

    return {
        "class": classification,
        "label": LABELS[classification],
        "status": "experimental_2d_candidate",
        "evidence": evidence,
        "oriented_box_pixels": corners,
        "area_pixels": area_pixels,
        "area_km2": round(area_km2, 2),
        "length_km": round(length_km, 2),
        "width_km": round(width_km, 2),
        "elongation_ratio": round(elongation, 3),
        "convective_cores": cores,
        "max_dbz": round(max_dbz, 1),
        "curvature_score": round(curvature, 3),
        "confidence": "unvalidated_heuristic",
        "vertical_information": False,
        "rotation_confirmed": False,
        "mesocyclone_confirmed": False,
        "mcc_confirmed": False,
  }
  
