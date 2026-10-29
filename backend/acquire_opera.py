
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
I.R.I.D.E. — Acquisizione radar OPERA
Fonte: EUMETNET Open Radar Data / MeteoGate
Prodotto: composito europeo DBZH
Licenza dichiarata dal servizio: CC BY 4.0, con eventuali
eccezioni indicate nei metadati.

Questo modulo acquisisce il file sorgente.
La decodifica della matrice e la segmentazione delle celle
sono operazioni successive, non simulate in questo modulo.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
META_FILE = RAW_DIR / "opera_latest.json"

API_URL = (
    "https://api.meteogate.eu/"
    "eu-eumetnet-weather-radar/collections/observations/"
    "locations/0-20010-0-OPERA"
)

USER_AGENT = "IRIDE-OpenWeatherResearch/1.0"
TIMEOUT_SECONDS = 45
MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024


def utc_now():
    return datetime.now(timezone.utc)


def request_bytes(url, timeout=TIMEOUT_SECONDS):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, application/octet-stream, */*",
        },
    )

    with urllib.request.urlopen(req, timeout=timeout) as response:
        status = response.status
        content_type = response.headers.get("Content-Type", "")

        if status != 200:
            raise RuntimeError(f"HTTP {status}: {url}")

        data = response.read(MAX_DOWNLOAD_BYTES + 1)

        if len(data) > MAX_DOWNLOAD_BYTES:
            raise RuntimeError(
                "Download oltre il limite di sicurezza di 250 MiB."
            )

        return data, content_type


def query_url(start, end):
    params = {
        "datetime": (
            start.strftime("%Y-%m-%dT%H:%MZ")
            + "/"
            + end.strftime("%Y-%m-%dT%H:%MZ")
        ),
        "f": "CoverageJSON",
        "standard_name": "DBZH",
        "format": "ODIM",
    }

    return API_URL + "?" + urllib.parse.urlencode(params)


def walk_for_links(obj):
    """Raccoglie URL dichiarati dalla risposta API."""
    found = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and value.startswith("https://"):
                lower = value.lower()

                if (
                    key.lower() in {
                        "href", "data", "url", "download",
                        "download_url", "self"
                    }
                    or lower.endswith((".h5", ".hdf5", ".hdf"))
                    or ".h5?" in lower
                    or ".hdf5?" in lower
                ):
                    found.append(value)

            else:
                found.extend(walk_for_links(value))

    elif isinstance(obj, list):
        for item in obj:
            found.extend(walk_for_links(item))

    return found


def download_latest():
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    now = utc_now().replace(second=0, microsecond=0)

    # Si cerca un intervallo recente. L'API può non restituire
    # prodotti in ogni finestra: in quel caso si riprova più indietro.
    windows = [
        (now - timedelta(minutes=45), now),
        (now - timedelta(minutes=90), now - timedelta(minutes=30)),
        (now - timedelta(hours=2), now - timedelta(minutes=60)),
    ]

    last_error = None

    for start, end in windows:
        url = query_url(start, end)

        try:
            body, content_type = request_bytes(url)

            if not body:
                last_error = "Risposta API vuota."
                continue

            try:
                response_json = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                last_error = (
                    "La risposta dell'API non è JSON: "
                    + content_type
                )
                continue

            links = list(dict.fromkeys(walk_for_links(response_json)))

            # Priorità ai collegamenti che indicano chiaramente
            # un file HDF5/ODIM.
            candidates = [
                link for link in links
                if any(
                    marker in link.lower()
                    for marker in (".h5", ".hdf5", ".hdf")
                )
            ]

            # Alcune risposte espongono un URL di download
            # senza estensione. In assenza di HDF5 espliciti,
            # non si scarica arbitrariamente l'endpoint stesso.
            if not candidates:
                last_error = (
                    "La risposta API non contiene un link HDF5 "
                    "riconoscibile. Occorre verificare i metadati "
                    "restituiti dal servizio."
                )
                continue

            for index, file_url in enumerate(candidates):
                try:
                    data, mime = request_bytes(file_url)

                    # Verifica minima della firma HDF5.
                    if not data.startswith(b"\x89HDF\r\n\x1a\n"):
                        last_error = (
                            "Il link non ha restituito un file HDF5 valido: "
                            + file_url
                        )
                        continue

                    stamp = datetime.now(timezone.utc).strftime(
                        "%Y%m%dT%H%M%SZ"
                    )

                    filename = f"opera_dbzh_{stamp}_{index}.h5"
                    destination = RAW_DIR / filename
                    temporary = destination.with_suffix(".h5.tmp")

                    with temporary.open("wb") as file:
                        file.write(data)

                    temporary.replace(destination)

                    metadata = {
                        "product": "I.R.I.D.E.",
                        "provider": "EUMETNET OPERA / MeteoGate",
                        "parameter": "DBZH",
                        "source_url": file_url,
                        "query_url": url,
                        "query_start_utc": start.isoformat(),
                        "query_end_utc": end.isoformat(),
                        "downloaded_at_utc": datetime.now(
                            timezone.utc
                        ).isoformat(),
                        "file": str(destination.relative_to(ROOT)),
                        "bytes": len(data),
                        "content_type": mime,
                        "license": "CC BY 4.0; verificare eccezioni nei metadati",
                        "status": "downloaded_not_yet_decoded",
                    }

                    temp_meta = META_FILE.with_suffix(".json.tmp")
                    with temp_meta.open("w", encoding="utf-8") as file:
                        json.dump(
                            metadata, file,
                            indent=2, ensure_ascii=False
                        )
                        file.write("\n")

                    temp_meta.replace(META_FILE)

                    print("Acquisizione OPERA riuscita.")
                    print(f"File: {destination}")
                    print(f"Byte: {len(data)}")
                    print(f"Metadati: {META_FILE}")
                    print(
                        "Stato: file acquisito; "
                        "decodifica e segmentazione ancora necessarie."
                    )

                    return 0

                except (OSError, urllib.error.URLError, RuntimeError) as exc:
                    last_error = str(exc)

        except (
            OSError,
            urllib.error.URLError,
            TimeoutError,
            RuntimeError,
        ) as exc:
            last_error = str(exc)

        time.sleep(1)

    raise RuntimeError(
        "Non è stato possibile acquisire un file OPERA DBZH valido. "
        f"Ultimo errore: {last_error}"
    )


if __name__ == "__main__":
    try:
        sys.exit(download_latest())
    except Exception as exc:
        print(f"ERRORE ACQUISIZIONE OPERA: {exc}", file=sys.stderr)
        sys.exit(1)
