"""Operator script for bounded BIS policy-rate acquisition (ADRs 0014-0016)."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from urllib.request import Request, urlopen

from fxlab.research.bis_policy_rates import (
    BIS_SDMX_XML_ACCEPT,
    FROZEN_SERIES_KEYS,
    NORMALIZATION_VERSION,
    BisRawArtifact,
    build_bis_initialization_request_url,
    build_bis_series_request_url,
    normalize_bis_policy_rate_payloads,
    parse_bis_initialization_payload,
    publish_canonical_acquisition_bundle,
)

DEFAULT_RAW_OUTPUT_DIR = Path("data/raw/bis_cbpol_v3")
DEFAULT_INITIALIZATION_OUTPUT_DIR = Path("data/raw/bis_cbpol_initialization_v3")
DEFAULT_NORMALIZED_OUTPUT_PATH = Path("data/normalized/bis_cbpol_daily_v3.json")


def _fetch_url(url: str, series_key: str, timeout: float) -> bytes:
    req = Request(
        url,
        headers={"Accept": BIS_SDMX_XML_ACCEPT, "User-Agent": "FXLab-Research/1.0"},
    )
    with urlopen(req, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(
                f"BIS API request failed for {series_key} with status {response.status}"
            )
        body = response.read()
    if not body:
        raise RuntimeError(f"BIS API returned empty body for {series_key}")
    return body


def fetch_bis_series_payload(series_key: str, timeout: float = 30.0) -> bytes:
    """Fetch one ADR 0014 bounded in-window payload."""
    return _fetch_url(build_bis_series_request_url(series_key), series_key, timeout)


def fetch_bis_initialization_payload(series_key: str, timeout: float = 30.0) -> bytes:
    """Fetch one ADR 0016 direct-predecessor payload."""
    return _fetch_url(build_bis_initialization_request_url(series_key), series_key, timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Acquire canonical BIS policy-rate evidence under ADRs 0014-0016."
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Explicitly execute the one bounded live acquisition.",
    )
    parser.add_argument("--raw-output-dir", type=Path, default=DEFAULT_RAW_OUTPUT_DIR)
    parser.add_argument(
        "--initialization-output-dir",
        type=Path,
        default=DEFAULT_INITIALIZATION_OUTPUT_DIR,
    )
    parser.add_argument(
        "--normalized-output", type=Path, default=DEFAULT_NORMALIZED_OUTPUT_PATH
    )
    args = parser.parse_args(argv)

    if not args.run:
        print(
            "ERROR: Live BIS policy-rate evidence acquisition requires explicit '--run' flag.",
            file=sys.stderr,
        )
        return 1

    print(f"Starting bounded BIS acquisition with normalization {NORMALIZATION_VERSION}...")
    initialization_payloads: dict[str, bytes] = {}
    raw_payloads: dict[str, bytes] = {}
    for series_key in FROZEN_SERIES_KEYS:
        print(
            f"Fetching initialization {series_key} "
            "[endPeriod=2013-12-31, lastNObservations=1]..."
        )
        initialization_payloads[series_key] = fetch_bis_initialization_payload(series_key)
        print(f"Fetching research {series_key} [2014-01-01 to 2023-12-31]...")
        raw_payloads[series_key] = fetch_bis_series_payload(series_key)

    initialization_artifacts = {
        series_key: parse_bis_initialization_payload(
            initialization_payloads[series_key], expected_series=series_key
        )
        for series_key in FROZEN_SERIES_KEYS
    }
    raw_artifacts = {
        series_key: BisRawArtifact(
            requested_series=FROZEN_SERIES_KEYS,
            start_inclusive="2014-01-01",
            end_exclusive="2024-01-01",
            content_format="sdmx-xml",
            raw_byte_count=len(raw_payloads[series_key]),
            raw_sha256=hashlib.sha256(raw_payloads[series_key]).hexdigest(),
            series_payloads=((series_key, raw_payloads[series_key]),),
        )
        for series_key in FROZEN_SERIES_KEYS
    }

    print("Normalizing and cross-validating complete eight-series dataset...")
    normalized_dataset = normalize_bis_policy_rate_payloads(
        raw_payloads,
        initialization_payloads_by_series=initialization_payloads,
        content_format="sdmx-xml",
    )
    print(f"Normalized {normalized_dataset.record_count} research observations.")
    print(f"Normalized identity: {normalized_dataset.normalized_identity}")

    print("Publishing complete canonical artifact bundle transactionally...")
    raw_paths, initialization_paths, published_path = publish_canonical_acquisition_bundle(
        raw_artifacts_by_series=raw_artifacts,
        initialization_artifacts_by_series=initialization_artifacts,
        normalized_dataset=normalized_dataset,
        raw_output_dir=args.raw_output_dir,
        initialization_output_dir=args.initialization_output_dir,
        normalized_output_file=args.normalized_output,
    )
    for series_key in FROZEN_SERIES_KEYS:
        print(f"  Published research raw artifact {series_key}: {raw_paths[series_key]}")
        print(
            "  Published initialization artifact "
            f"{series_key}: {initialization_paths[series_key]}"
        )
    print(f"Published canonical normalized dataset: {published_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())