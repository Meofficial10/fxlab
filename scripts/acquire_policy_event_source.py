"""Bounded, fail-closed acquisition entrypoint for Candidate B policy-event
source artifacts.

Network access is disabled by default: :func:`main` refuses to construct a
network transport unless ``--authorize-network-acquisition`` is supplied, and it
refuses *before* any transport object is built. The fetch boundary performs
exactly one attempt with no retry, no fallback, and no cache; it enforces the
frozen request contract (exact URL, exact accept media type, bounded timeout,
strict maximum response size, redirect policy) and rejects — never truncates —
anything that deviates.

This module deliberately never constructs a policy-rate event and never invokes
qualification. A verified artifact bridges only to ``PolicySourceEvidence``
downstream, which is a separate, later step.
"""

from __future__ import annotations

import argparse
import urllib.request
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fxlab.data.policy_event_source import (
    OfficialPolicyArtifactSpec,
    PolicyEventSourceHttpResponse,
    PolicyEventSourcePublication,
    PolicyEventSourceTransport,
    persist_policy_event_source_artifact,
    resolve_official_policy_artifact_spec,
)
from fxlab.data.policy_rates import MAX_OBSERVATION_DATE, PolicyRateQualificationError

AUTHORITATIVE_TIMEOUT_SECONDS = 15
AUTHORITATIVE_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


def _enforce_redirect_contract(
    spec: OfficialPolicyArtifactSpec, response: PolicyEventSourceHttpResponse
) -> None:
    chain = tuple(response.redirect_chain)
    if spec.approved_redirect_chain:
        if chain != spec.approved_redirect_chain:
            raise PolicyRateQualificationError("redirect_rejected")
        if response.final_url != spec.approved_url or chain[-1] != spec.approved_url:
            raise PolicyRateQualificationError("redirect_rejected")
        for hop in chain:
            if (urlsplit(hop).hostname or "").lower() != spec.authority_host:
                raise PolicyRateQualificationError("redirect_outside_authority_domain")
    elif chain or response.final_url != spec.approved_url:
        raise PolicyRateQualificationError("redirect_rejected")


def _enforce_response_contract(
    spec: OfficialPolicyArtifactSpec,
    response: PolicyEventSourceHttpResponse,
    max_response_bytes: int,
) -> None:
    if not isinstance(response, PolicyEventSourceHttpResponse):
        raise PolicyRateQualificationError("transport_failure")
    # Redirect binding is validated before status so a redirected response is
    # reported as a redirect, not as an incidental status failure.
    _enforce_redirect_contract(spec, response)
    if response.status_code != 200:
        raise PolicyRateQualificationError("http_status_not_success")
    if response.media_type != spec.response_media_type:
        raise PolicyRateQualificationError("media_type_not_approved")
    if len(response.raw_bytes) > max_response_bytes:
        raise PolicyRateQualificationError("response_too_large")


def fetch_policy_event_source_response(
    spec: OfficialPolicyArtifactSpec,
    transport: PolicyEventSourceTransport,
    *,
    timeout_seconds: int = AUTHORITATIVE_TIMEOUT_SECONDS,
    max_response_bytes: int = AUTHORITATIVE_MAX_RESPONSE_BYTES,
) -> PolicyEventSourceHttpResponse:
    if not isinstance(spec, OfficialPolicyArtifactSpec):
        raise PolicyRateQualificationError("spec_not_approved")
    # Date-first sealed-window rejection, before any transport call.
    if spec.event_date > MAX_OBSERVATION_DATE:
        raise PolicyRateQualificationError("sealed_window_violation")
    try:
        response = transport.fetch(
            spec,
            exact_url=spec.approved_url,
            accept=spec.accept_media_type,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
    except PolicyRateQualificationError:
        raise
    except TimeoutError as exc:
        raise PolicyRateQualificationError("acquisition_timeout") from exc
    except Exception as exc:  # exactly one attempt, no retry, no fallback
        raise PolicyRateQualificationError("transport_failure") from exc
    _enforce_response_contract(spec, response, max_response_bytes)
    return response


def acquire_and_publish_policy_event_source(
    artifact_key: str,
    transport: PolicyEventSourceTransport,
    retrieved_at: datetime,
) -> PolicyEventSourcePublication:
    spec = resolve_official_policy_artifact_spec(artifact_key)
    response = fetch_policy_event_source_response(spec, transport)
    return persist_policy_event_source_artifact(spec, response, retrieved_at)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        del req, fp, code, msg, headers, newurl
        raise PolicyRateQualificationError("redirect_rejected")


class _UrllibPolicyEventSourceTransport:
    """Minimal one-shot HTTPS transport with redirects disabled and a bounded,
    non-truncating read. Only constructed inside :func:`main` after explicit
    network authorization; never exercised by the offline suite."""

    def fetch(
        self,
        spec: OfficialPolicyArtifactSpec,
        *,
        exact_url: str,
        accept: str,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> PolicyEventSourceHttpResponse:
        del spec
        if not exact_url.startswith("https://"):
            raise PolicyRateQualificationError("insecure_scheme_rejected")
        request = urllib.request.Request(exact_url, method="GET", headers={"Accept": accept})
        opener = urllib.request.build_opener(_NoRedirectHandler)
        with opener.open(request, timeout=timeout_seconds) as handle:
            status = getattr(handle, "status", None) or handle.getcode()
            final_url = handle.geturl()
            media_type = handle.headers.get_content_type() if handle.headers else ""
            raw_bytes = handle.read(max_response_bytes + 1)
            headers = dict(handle.headers.items()) if handle.headers else {}
        return PolicyEventSourceHttpResponse(
            status_code=int(status),
            final_url=final_url,
            media_type=media_type,
            headers=headers,
            raw_bytes=raw_bytes,
        )


def _build_network_transport() -> PolicyEventSourceTransport:
    return _UrllibPolicyEventSourceTransport()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Acquire and publish a Candidate B policy-event source artifact."
    )
    parser.add_argument("--artifact-key")
    parser.add_argument("--authorize-network-acquisition", action="store_true")
    args = parser.parse_args(argv)
    if not args.authorize_network_acquisition:
        raise SystemExit("network_acquisition_not_authorized")
    if not args.artifact_key:
        raise SystemExit("artifact_key_required")
    transport = _build_network_transport()
    published = acquire_and_publish_policy_event_source(
        args.artifact_key, transport, datetime.now(tz=UTC)
    )
    print(published.manifest.acquisition_id)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
