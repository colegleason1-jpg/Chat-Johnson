#!/usr/bin/env python3
"""Command-line runner for the orchestrator.

  python cli.py status              # keys detected, quota ledger
  python cli.py chat                # interactive chat (memory-enabled)
  python cli.py run "GOAL" [--repo PATH]
"""
from __future__ import annotations

import argparse
import json
import sys

from orchestrator.config import PROVIDERS, provider_api_key, provider_model
from orchestrator.executor import Orchestrator
from orchestrator.memory import TaskMemory
from orchestrator.quota import QuotaLedger
from orchestrator.router import classify, generate


def cmd_status() -> int:
    print("Provider status")
    print("-" * 60)
    for name, cfg in PROVIDERS.items():
        key = provider_api_key(cfg)
        state = "READY" if key else "no key"
        print(f"{name:<12} {state:<8} model={provider_model(cfg):<38} {cfg.label}")
    available = [n for n, c in PROVIDERS.items() if provider_api_key(c)]
    if not available:
        print("\nNo API keys found. Set at least one of:")
        for n, c in PROVIDERS.items():
            print(f"  {c.env_key}")
        return 1
    print(f"\n{len(available)} provider(s) active: {', '.join(available)}")
    return 0


def cmd_chat() -> int:
    ledger = QuotaLedger({n: (c.rpm_limit, c.tpm_limit) for n, c in PROVIDERS.items()})
    memory = TaskMemory(".orchestrator/chat_memory.json", goal="interactive chat")
    print("Chat (multi-provider routing). Empty line or Ctrl-D to exit.")
    while True:
        try:
            user = input("\nyou> ").strip()
        except EOFError:
            break
        if not user:
            break
        messages = [
            {"role": "system", "content": "You are a helpful coding assistant with provider routing."},
            {"role": "user", "content": memory.context_block() + f"\n\nUSER: {user}"},
        ]
        try:
            text, decision = generate(classify(user), messages, ledger, max_tokens=2048)
            print(f"\n[{decision.provider}] {text}")
        except Exception as exc:
            print(f"\n[error] {exc}")
    return 0


def cmd_run(goal: str, repo: str, json_out: bool) -> int:
    orch = Orchestrator()
    report = orch.run(goal, repo_path=repo or None)
    if json_out:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("\n=== ORCHESTRATOR REPORT ===")
        print(f"Goal    : {report['goal']}")
        print(f"Branch  : {report['branch']}")
        print(f"Sandbox : {report['sandbox']}")
        print(f"Ingest  : {report['ingest']}")
        print("\nMemory:\n" + report["memory"])
        if report["diff"]:
            print("\nDiff:\n" + report["diff"][:6000])
        for ev in orch.log:
            print(f"  [{ev['event']}] " + json.dumps({k: v for k, v in ev.items() if k not in ("ts", "event")}, default=str)[:300])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Free-tier LLM orchestrator")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("chat")
    run_p = sub.add_parser("run")
    run_p.add_argument("goal")
    run_p.add_argument("--repo", default="")
    run_p.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "chat":
        return cmd_chat()
    if args.cmd == "run":
        return cmd_run(args.goal, args.repo, args.json)
    return 1


if __name__ == "__main__":
    sys.exit(main())
