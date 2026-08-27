"""
Join 1: an artifact is not finished when it is written, it is finished when it
has replayed.

Recording used to end at "artifact emitted" and the design assumed the result
was replayable. It might not be. Checkpoint synthesis derives assertions from
what changed on screen, and a screen that changed by showing a posting timestamp
or a confirmation number yields a checkpoint that passes once and fails forever
after. Discovering that in production is unacceptable; discovering it by hand is
the kind of thing that gets skipped.

So the recorder replays the fresh artifact once — same app, same parameters, no
model — and only then writes it to `capabilities/`. One that fails its own
verification goes to `evidence/` with the failure attached, and the caller is
told why. Three things fall out for free: the volatile-checkpoint problem becomes
self-detecting rather than theoretical, `stability` gets its first data point,
and the demo's second evidence run exists without extra work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from cua.evidence import EvidenceRecorder
from cua.schema.artifact import ApprovalState, CapabilityArtifact, RiskClass
from cua.schema.result import ReplayResult, ReplayStatus

# Values that make a checkpoint pass once and fail forever after. Matched against
# the *detector text*, not the page, so the report can name the offending string.
VOLATILE_HINTS = (
    "posted", "confirmation", "timestamp", "session", "reference",
    "generated", "as of",
)


def capability_path(root: str | Path, art: CapabilityArtifact) -> Path:
    """`capabilities/<app_id>/<capability_id>/v<N>.json`.

    Nested rather than flat because the flat name has nowhere to put a second
    tenant's variant of the same vendor product, and versions are immutable —
    re-recording writes v<N+1> beside its predecessor rather than over it.
    """
    return (Path(root) / art.target.app_id / art.capability_id
            / f"v{art.version}.json")


@dataclass
class VerificationOutcome:
    """What happened when the fresh artifact was replayed against the app."""

    verified: bool
    result: ReplayResult
    artifact_path: Path | None
    reason: str | None = None
    volatile_suspects: list[str] | None = None

    @property
    def ok(self) -> bool:
        return self.verified


def _volatile_suspects(art: CapabilityArtifact) -> list[str]:
    """Checkpoint text that looks like it will not survive the next run.

    A hint, not a verdict — the verification replay is the verdict. This exists
    so the failure report can point at the likely cause instead of leaving a
    reader to diff two screens by eye.
    """
    suspects: list[str] = []
    for step in art.steps:
        for detector in (step.checkpoint.detectors if step.checkpoint else []):
            value = (detector.value or "").lower()
            if any(hint in value for hint in VOLATILE_HINTS):
                suspects.append(f"{step.id}: {detector.value!r}")
    for detector in art.success.detectors:
        value = (detector.value or "").lower()
        if any(hint in value for hint in VOLATILE_HINTS):
            suspects.append(f"(success): {detector.value!r}")
    return suspects


def verify_and_store(
    art: CapabilityArtifact,
    params: dict[str, str],
    *,
    replay: Callable[[CapabilityArtifact, dict[str, str]], ReplayResult],
    capabilities_root: str | Path = "capabilities",
    evidence: EvidenceRecorder | None = None,
    reset_app: Callable[[], None] | None = None,
) -> VerificationOutcome:
    """Replay `art` once, then store it only if that replay succeeded.

    `replay` is injected rather than constructed here so the recorder does not
    own a browser: the caller already has an authenticated session open from the
    discovery run, and verification must use the same surface.

    `reset_app` runs first when supplied. A capability whose last step is
    irreversible would otherwise post a *second* transaction during its own
    verification — see the note in REPORT §6. Against the mock app this is
    `POST /_reset`; against a real system, verification of an irreversible
    capability needs a sandbox tenant, and that is a deployment decision rather
    than something the recorder can paper over.
    """
    if reset_app is not None:
        reset_app()

    result = replay(art, params)
    suspects = _volatile_suspects(art)

    art.stability.replay_count += 1
    if result.status in (ReplayStatus.SUCCESS, ReplayStatus.BUSINESS_OUTCOME):
        art.stability.success_count += 1
        art.stability.last_verified_at = datetime.now(timezone.utc)
        art.approval_state = ApprovalState.DRAFT_VERIFIED

        path = capability_path(capabilities_root, art)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(art.model_dump_json(indent=2))
        if evidence:
            evidence.log("verification_passed", capability_id=art.capability_id,
                         version=art.version, path=str(path),
                         status=result.status.value)
        return VerificationOutcome(True, result, path,
                                   volatile_suspects=suspects or None)

    # Failed its own verification: to evidence, never to capabilities/.
    reason = _explain(result, suspects)
    if evidence:
        evidence.write_json("unverified_artifact", json.loads(art.model_dump_json()))
        evidence.write_json("verification_failure", json.loads(result.model_dump_json()))
        evidence.log("verification_failed", capability_id=art.capability_id,
                     status=result.status.value, reason=reason,
                     volatile_suspects=suspects)
    return VerificationOutcome(False, result, None, reason, suspects or None)


def _explain(result: ReplayResult, suspects: list[str]) -> str:
    parts = [f"verification replay returned {result.status.value}"]
    if result.failure:
        parts.append(f"at step {result.failure.step_id} ({result.failure.stage}): "
                     f"expected {result.failure.expected!r}, "
                     f"observed {result.failure.observed!r}")
    elif result.message:
        parts.append(result.message)
    if suspects:
        parts.append(
            "checkpoint text that will not survive a second run: "
            + "; ".join(suspects))
    return " — ".join(parts)


def requires_sandbox(art: CapabilityArtifact) -> bool:
    """Whether verifying this artifact would commit something irreversible."""
    return any(s.risk is RiskClass.IRREVERSIBLE for s in art.steps)
