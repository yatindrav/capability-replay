"""CLI: discover | replay | catalog | operator."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from cua.evidence import EvidenceRecorder
from cua.escalation.lease import InterventionQueue, SessionLease
from cua.replay.engine import ReplayEngine, new_run_id
from cua.safety.policy import Allowlist, PolicyGate
from cua.schema.artifact import CapabilityArtifact, ParamSpec
from cua.schema.result import ReplayStatus
from cua.surface.session import bootstrap_session
from cua.surface.web import WebSurfaceAdapter

EVIDENCE = "evidence"
CAPS = "capabilities"
INTERVENTIONS = "evidence/interventions"


def _parse_params(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--param expects name=value, got '{p}'")
        k, v = p.split("=", 1)
        out[k] = v
    return out


def _browser(pw, headed: bool):
    b = pw.chromium.launch(headless=not headed)
    ctx = b.new_context(viewport={"width": 1280, "height": 800})
    return b, ctx, ctx.new_page()


# ---------------------------------------------------------------------------


def cmd_discover(args) -> int:
    from cua.agent.discovery import DiscoveryAgent, build_artifact

    params = _parse_params(args.param)
    run_id = new_run_id("disc")
    ev = EvidenceRecorder(EVIDENCE, run_id)
    allow = Allowlist.load(args.allowlist_config, args.allowlist)
    gate = PolicyGate(allow, attended=False)
    lease = SessionLease()
    iq = InterventionQueue(INTERVENTIONS)

    with sync_playwright() as pw:
        b, ctx, page = _browser(pw, args.headed)
        gate.guard_navigation(page)
        adapter = WebSurfaceAdapter(page)
        bootstrap_session(page, args.entry, args.app_id)
        agent = DiscoveryAgent(adapter, gate, ev, lease, iq,
                               model=args.model, max_steps=args.max_steps)
        try:
            result = agent.run(args.goal, args.entry, params)
        finally:
            if not args.keep_open:
                b.close()

    print(f"\nrun_id: {run_id}   evidence: {ev.dir}")
    if not result["ok"]:
        print(f"DISCOVERY STUCK: {result['reason']}")
        print(f"intervention: {result.get('escalation_id')}")
        return 2

    specs = [ParamSpec(name=k, type="string", description=f"Value for {k}",
                       pattern=r"^\d+$" if v.isdigit() else None, example=v)
             for k, v in params.items()]

    art = build_artifact(
        capability_id=args.capability_id,
        title=args.title or args.capability_id,
        description=result["summary"],
        goal=args.goal, entry_url=args.entry, app_id=args.app_id,
        allowlist_id=args.allowlist, discovery=result,
        model=args.model, run_id=run_id, param_specs=specs,
    )
    out = Path(CAPS) / f"{art.capability_id}.v{art.version}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(art.model_dump_json(indent=2))
    ev.write_json("artifact", json.loads(art.model_dump_json()))

    print(f"artifact: {out}  ({len(art.steps)} steps, "
          f"{len(art.inputs)} inputs, {len(art.outputs)} outputs)")
    return 0


def cmd_replay(args) -> int:
    art = CapabilityArtifact.model_validate_json(Path(args.capability).read_text())
    params = _parse_params(args.param)

    if art.approval_state.value != "approved" and not args.allow_draft:
        print(f"refusing to replay a '{art.approval_state.value}' artifact "
              f"unattended; pass --allow-draft to override")
        return 3

    run_id = new_run_id("replay")
    ev = EvidenceRecorder(EVIDENCE, run_id)
    allow = Allowlist.load(args.allowlist_config, art.policy.allowlist_id)
    gate = PolicyGate(allow, attended=args.attended)
    lease = SessionLease()
    iq = InterventionQueue(INTERVENTIONS)

    with sync_playwright() as pw:
        b, ctx, page = _browser(pw, args.headed)
        gate.guard_navigation(page)
        adapter = WebSurfaceAdapter(page)
        bootstrap_session(page, art.target.entry_url_template or "", art.target.app_id)
        engine = ReplayEngine(
            adapter, gate, ev, lease, iq,
            reauth=lambda: bootstrap_session(
                page, art.target.entry_url_template or "", art.target.app_id),
        )

        result = engine.replay(art, params)

        # --- human-in-the-loop: the session stays open across the handoff ---
        while result.status == ReplayStatus.ESCALATED and args.attended:
            before = adapter.snapshot().tree
            print(f"\n>>> ESCALATED: {result.message}")
            print(f">>> intervention {result.escalation_id} "
                  f"(context in {INTERVENTIONS}/{result.escalation_id}.json)")
            print(">>> operate the SAME live browser session, then press Enter "
                  "to hand control back.")
            if args.auto_handback:
                time.sleep(args.auto_handback)
            else:
                input()

            lease.operator_take_control()
            lease.operator_hand_back(args.operator_note or "manual step completed")
            summary = engine.resume_after_handoff(
                _req_stub(result.escalation_id, iq), before)
            print(">>> captured human actions:")
            for line in summary:
                print(f"      {line}")

            result = engine.replay(art, params, start_at=result.resume_from_step)

        b.close()

    ev.write_json("result", json.loads(result.model_dump_json()))
    _print_result(result)
    return 0 if result.ok_for_caller else 1


def _req_stub(request_id, iq):
    from cua.escalation.lease import InterventionRequest
    doc = iq.load(request_id)
    r = InterventionRequest()
    r.request_id = doc["request_id"]
    return r


def _print_result(r) -> None:
    print("\n" + "=" * 62)
    print(f"STATUS            {r.status.value.upper()}")
    print(f"capability        {r.capability_id} v{r.capability_version}")
    if r.outputs:
        print(f"outputs           {json.dumps(r.outputs)}")
    if r.outcome_code:
        print(f"outcome_code      {r.outcome_code}")
    if r.message:
        print(f"message           {r.message}")
    if r.failure:
        print(f"failed at step    {r.failure.step_id} (stage: {r.failure.stage})")
        print(f"  expected        {r.failure.expected}")
        print(f"  observed        {r.failure.observed}")
        for ref in r.failure.evidence_refs:
            print(f"  evidence        {ref}")
    if r.escalation_id:
        print(f"intervention      {r.escalation_id}")
    if r.drift_signals:
        print(f"drift             {'; '.join(r.drift_signals)}")
    print(f"steps executed    {len(r.steps)}   ({r.duration_ms} ms)")
    for s in r.steps:
        via = f"{s.resolved_by}@{s.fallback_depth}" if s.resolved_by else "-"
        cond = f" conditions={s.conditions_fired}" if s.conditions_fired else ""
        print(f"  {s.step_id:<4} {s.action_kind:<9} via {via:<14}"
              f" cp={s.checkpoint_passed}{cond}")
    print(f"evidence          {r.evidence_dir}")
    print("=" * 62)


def cmd_catalog(args) -> int:
    """Expose saved artifacts as callable capabilities (stretch goal)."""
    tools = []
    for p in sorted(Path(CAPS).glob("*.json")):
        art = CapabilityArtifact.model_validate_json(p.read_text())
        schema = art.tool_schema()
        schema["_meta"] = {
            "artifact": str(p), "version": art.version,
            "approval_state": art.approval_state.value,
            "app_id": art.target.app_id, "variant": art.target.variant,
            "returns": {o.name: o.type for o in art.outputs},
            "outcome_codes": sorted({c.outcome_code for c in art.global_conditions
                                     if c.outcome_code}),
        }
        tools.append(schema)
    print(json.dumps(tools, indent=2))
    return 0


def cmd_operator(args) -> int:
    """Bare operator console. Deliberately minimal — see REPORT §5."""
    from flask import Flask

    iq = InterventionQueue(INTERVENTIONS)
    app = Flask(__name__)

    @app.get("/")
    def index():
        pending = iq.pending()
        if not pending:
            return "<h3>No pending interventions.</h3>"
        rows = []
        for d in pending:
            shot = (f'<img src="/shot/{d["request_id"]}" width="620">'
                    if d.get("screenshot_path") else "")
            rows.append(
                f'<div style="border:1px solid #888;margin:12px;padding:10px;'
                f'font-family:monospace">'
                f'<b>{d["request_id"]}</b> &mdash; {d["reason"]}<br>'
                f'capability: {d["capability_id"]}<br>'
                f'step: {d.get("step_id")} &mdash; {d.get("step_intent")}<br>'
                f'why: {d["detail"]}<br>url: {d["observed_url"]}<br>'
                f'params: {d.get("params_redacted")}<br>{shot}'
                f'<pre style="max-height:220px;overflow:auto;background:#eee">'
                f'{d["observed_tree"][:2500]}</pre></div>')
        return ("<h3>Pending interventions</h3>"
                "<p>Take control in the live browser window the run opened, "
                "complete the manual step, then hand control back in the "
                "run's terminal.</p>" + "".join(rows))

    @app.get("/shot/<rid>")
    def shot(rid):
        from flask import send_file
        return send_file(iq.load(rid)["screenshot_path"])

    print(f"operator console: http://127.0.0.1:{args.port}")
    app.run(host="127.0.0.1", port=args.port)
    return 0


# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cua")
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = dict(allowlist_config="config/allowlist.yaml")

    d = sub.add_parser("discover", help="LLM-driven discovery run")
    d.add_argument("--goal", required=True)
    d.add_argument("--entry", required=True)
    d.add_argument("--capability-id", required=True)
    d.add_argument("--title")
    d.add_argument("--app-id", default="acme-servicing")
    d.add_argument("--allowlist", default="acme-servicing-readonly")
    d.add_argument("--allowlist-config", default=common["allowlist_config"])
    d.add_argument("--param", action="append", default=[])
    d.add_argument("--model", default="claude-sonnet-5")
    d.add_argument("--max-steps", type=int, default=20)
    d.add_argument("--headed", action="store_true")
    d.add_argument("--keep-open", action="store_true")
    d.set_defaults(fn=cmd_discover)

    r = sub.add_parser("replay", help="deterministic replay, no LLM")
    r.add_argument("--capability", required=True)
    r.add_argument("--param", action="append", default=[])
    r.add_argument("--allowlist-config", default=common["allowlist_config"])
    r.add_argument("--attended", action="store_true",
                   help="a human is available to take control on escalation")
    r.add_argument("--auto-handback", type=float, default=0,
                   help="seconds to wait instead of prompting (for scripted demos)")
    r.add_argument("--operator-note", default="")
    r.add_argument("--allow-draft", action="store_true")
    r.add_argument("--headed", action="store_true")
    r.set_defaults(fn=cmd_replay)

    c = sub.add_parser("catalog", help="list artifacts as callable tool schemas")
    c.set_defaults(fn=cmd_catalog)

    o = sub.add_parser("operator", help="bare operator console")
    o.add_argument("--port", type=int, default=8077)
    o.set_defaults(fn=cmd_operator)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
