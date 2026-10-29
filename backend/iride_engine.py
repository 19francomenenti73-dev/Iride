#!/usr/bin/env python3
"""I.R.I.D.E. - Radar matrix to physically informed pseudo-volume."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import h5py
import numpy as np
from scipy.ndimage import zoom

ROOT = Path(__file__).resolve().parents[1]


def attrs(obj):
    result = {}
    for key, value in obj.attrs.items():
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        elif isinstance(value, np.generic):
            value = value.item()
        result[key] = value
    return result


def load_dbzh(filename):
    with h5py.File(filename, "r") as h5:
        candidates = []

        def visitor(name, obj):
            if isinstance(obj, h5py.Dataset) and name.endswith("/data"):
                parent = obj.parent
                quantity = ""
                if "what" in parent:
                    quantity = str(
                        attrs(parent["what"]).get("quantity", "")
                    )
                if quantity.upper() == "DBZH":
                    candidates.append((name, obj[:], attrs(parent["what"])))

        h5.visititems(visitor)

        if not candidates:
            raise ValueError(
                "Nessun dataset DBZH trovato nel file HDF5."
            )

        dataset_name, raw, data_meta = candidates[0]
        root_meta = attrs(h5)
        where = attrs(h5["where"]) if "where" in h5 else {}
        product = (
            attrs(h5["what"]) if "what" in h5 else {}
        )

    nodata = float(data_meta.get("nodata", -9999))
    undetect = float(data_meta.get("undetect", -8888))
    gain = float(data_meta.get("gain", 1.0))
    offset = float(data_meta.get("offset", 0.0))

    raw = np.asarray(raw)
    dbzh = raw.astype(np.float32) * gain + offset
    invalid = (
        ~np.isfinite(dbzh)
        | (raw == nodata)
        | (raw == undetect)
        | (dbzh < -100)
        | (dbzh > 100)
    )
    dbzh[invalid] = np.nan

    return dbzh, {
        "dataset": dataset_name,
        "data": data_meta,
        "where": where,
        "product": product,
        "root": root_meta,
    }


def resize_matrix(matrix, max_side=180):
    h, w = matrix.shape
    factor = min(1.0, max_side / max(h, w))
    if factor == 1.0:
        return matrix

    out_h = max(2, round(h * factor))
    out_w = max(2, round(w * factor))

    # Interpolazione bilineare: valori NaN gestiti separatamente.
    valid = np.isfinite(matrix).astype(np.float32)
    values = np.nan_to_num(matrix, nan=0.0)

    numerator = zoom(
        values, (out_h / h, out_w / w), order=1
    )
    denominator = zoom(
        valid, (out_h / h, out_w / w), order=1
    )

    result = np.full((out_h, out_w), np.nan, dtype=np.float32)
    good = denominator > 0.5
    result[good] = numerator[good] / denominator[good]
    return result


def make_payload(matrix, metadata, threshold):
    sampled = resize_matrix(matrix)
    finite = np.isfinite(sampled)

    if not finite.any():
        raise ValueError("La matrice DBZH non contiene valori validi.")

    # Le celle sotto soglia restano nella matrice: possono contribuire
    # alla ricostruzione dell'incudine e delle fasce deboli.
    values = np.where(finite, sampled, None).tolist()

    source = metadata["product"]
    where = metadata["where"]
    data = metadata["data"]

    startdate = str(source.get("startdate", ""))
    starttime = str(source.get("starttime", ""))
    enddate = str(source.get("enddate", startdate))
    endtime = str(source.get("endtime", starttime))

    return {
        "schema_version": 2,
        "system": "I.R.I.D.E.",
        "generated_at_utc": datetime.now(
            timezone.utc
        ).isoformat(timespec="seconds"),
        "source": {
            "format": "HDF5 / ODIM",
            "quantity": data.get("quantity", "DBZH"),
            "dataset": metadata["dataset"],
            "product": source.get("prodname", "OPERA"),
            "start": startdate + "T" + starttime,
            "end": enddate + "T" + endtime,
            "original_rows": int(matrix.shape[0]),
            "original_columns": int(matrix.shape[1]),
            "rows": int(sampled.shape[0]),
            "columns": int(sampled.shape[1]),
            "xscale": where.get("xscale"),
            "yscale": where.get("yscale"),
            "projdef": where.get("projdef"),
        },
        "threshold_dbz": float(threshold),
        "dbzh_min": float(np.nanmin(sampled)),
        "dbzh_max": float(np.nanmax(sampled)),
        "valid_pixels": int(finite.sum()),
        "matrix_dbzh": values,
        "reconstruction": {
            "method": "nested reflectivity masks and inferred vertical profile",
            "blue_anvil": "inferred from spatial dilation of observed echoes",
            "green_band": "inferred inner band, approximately 70% of anvil radius",
            "strong_core": "lower vertical placement for higher reflectivity",
            "vertical_heights_are_measured": False,
            "vil_is_measured": False,
            "echo_top_is_measured": False,
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", default=str(ROOT / "data" / "source.h5")
    )
    parser.add_argument(
        "--output", default=str(ROOT / "docs" / "data" / "iride.json")
    )
    parser.add_argument("--threshold", type=float, default=20.0)
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.is_file():
        raise FileNotFoundError(
            f"File radar non trovato: {input_path}"
        )

    matrix, metadata = load_dbzh(input_path)
    payload = make_payload(matrix, metadata, args.threshold)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, separators=(",", ":"), allow_nan=False),
        encoding="utf-8",
    )

    print(
        f"JSON creato: {output_path} | "
        f"matrice {payload['source']['rows']}x"
        f"{payload['source']['columns']} | "
        f"DBZH max {payload['dbzh_max']:.1f}"
    )


if __name__ == "__main__":
    main()
    
