"""Operator script for BIS policy-rate evidence acquisition (ADR 0014).

Requires explicit `--run` flag to execute live acquisition.
Without `--run`, this script fails closed immediately without performing any network I/O.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from fxlab.research.bis_policy_rates import (
    BIS_SDMX_XML_ACCEPT,
    FROZEN_SERIES_KEYS,
    BisRawArtifact,
    build_bis_series_request_url,
    normalize_bis_policy_rate_payloads,
    publish_canonical_acquisition_bundle,
)

DEFAULT_RAW_OUTPUT_DIR = Path("data/raw/bis_cbpol")
DEFAULT_NORMALIZED_OUTPUT_PATH = Path("data/normalized/bis_cbpol_daily_v2.json")


def fetch_bis_series_payload(series_key: str, timeout: float = 30.0) -> bytes:
    """Fetch one bounded series payload from the BIS SDMX API."""
    url = build_bis_series_request_url(series_key)
    req = Request(
        url,
        headers={"Accept": BIS_SDMX_XML_ACCEPT, "User-Agent": "FXLab-Research/1.0"},
    )
    with urlopen(req, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(
                f"BIS API request failed for {series_key} with status {response.status}"
            )
        return response.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Acquire and normalize canonical BIS policy-rate evidence (ADR 0014)."
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Explicitly execute live bounded acquisition from BIS API. Required for execution.",
    )
    parser.add_argument(
        "--raw-output-dir",
        type=Path,
        default=DEFAULT_RAW_OUTPUT_DIR,
        help="Directory to publish immutable raw evidence artifact.",
    )
    parser.add_argument(
        "--normalized-output",
        type=Path,
        default=DEFAULT_NORMALIZED_OUTPUT_PATH,
        help="File path to publish normalized dataset artifact.",
    )

    args = parser.parse_args(argv)

    if not args.run:
        print(
            "ERROR: Live BIS policy-rate evidence acquisition requires explicit '--run' flag.\n"
            "This script refuses accidental execution without explicit operator authorization.",
            file=sys.stderr,
        )
        return 1

    print("Starting bounded BIS policy-rate evidence acquisition for ADR 0014...")
    # Step 1: Fetch all 8 bounded payloads into memory
    raw_payloads: dict[str, bytes] = {}
    for series_key in FROZEN_SERIES_KEYS:
        print(f"Fetching series {series_key} [2014-01-01 to 2023-12-31]...")
        payload = fetch_bis_series_payload(series_key)
        raw_payloads[series_key] = payload
        print(f"  Received {len(payload)} bytes for {series_key}")

    # Step 2: Build and validate all 8 raw evidence objects in memory
    raw_artifacts: dict[str, BisRawArtifact] = {}
    for series_key in FROZEN_SERIES_KEYS:
        payload = raw_payloads[series_key]
        raw_art = BisRawArtifact(
            requested_series=FROZEN_SERIES_KEYS,
            start_inclusive="2014-01-01",
            end_exclusive="2024-01-01",
            content_format="sdmx-xml",
            raw_byte_count=len(payload),
            raw_sha256=hashlib.sha256(payload).hexdigest(),
            series_payloads=((series_key, payload),),
        )
        raw_artifacts[series_key] = raw_art

    # Step 3: Normalize and validate the complete 8-series dataset in memory
    print("Normalizing and cross-validating complete eight-series dataset...")
    normalized_dataset = normalize_bis_policy_rate_payloads(
        raw_payloads, content_format="sdmx-xml"
    )
    print(f"Normalized {normalized_dataset.record_count} total observations across 8 series.")
    print(f"Normalized identity: {normalized_dataset.normalized_identity}")

    # Step 4: Transactionally publish all artifacts only after complete validation
    print("Publishing complete canonical artifact bundle transactionally...")
    raw_paths, published_path = publish_canonical_acquisition_bundle(
        raw_artifacts_by_series=raw_artifacts,
        normalized_dataset=normalized_dataset,
        raw_output_dir=args.raw_output_dir,
        normalized_output_file=args.normalized_output,
    )
    for sk, p in raw_paths.items():
        print(f"  Published raw artifact for {sk}: {p}")
    print(f"Published canonical normalized dataset: {published_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
