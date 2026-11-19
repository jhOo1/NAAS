"""Add an action-only no-new-response control to the frozen channel audit."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from audit_pmbench import REVISION
from run_evidence_pilot import MODEL, SAMPLING, SEEDS, QwenAPI, compact, save_artifact
from run_pmbench_channel_lock import CHANNEL_INSTRUCTION, coverage
from run_pmbench_nonclock import score_trial
from run_pmbench_probe import CHOOSE_INSTRUCTION
from run_pmbench_timing import get_reply


ARMS = ("provisional", "no_new_reply", "archived_reply")
TRANSITIONS = ((ARMS[0], ARMS[1]), (ARMS[1], ARMS[2]), (ARMS[0], ARMS[2]))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def deltas(alternatives, targets):
    result = {}
    for left, right in TRANSITIONS:
        old, new = alternatives[left], alternatives[right]
        old_hits, new_hits = set(old["completed_due"]), set(new["completed_due"])
        gains, losses = new_hits - old_hits, old_hits - new_hits
        old_extra, new_extra = set(old["non_due_actions"]), set(new["non_due_actions"])
        result[f"{left}_to_{right}"] = {
            "target_gains": sorted(gains & targets), "target_losses": sorted(losses & targets),
            "other_gains": sorted(gains - targets), "other_losses": sorted(losses - targets),
            "non_due_added": sorted(new_extra - old_extra), "non_due_removed": sorted(old_extra - new_extra),
            "false_alarm_delta": new["current_step_metrics"]["false_alarm"] - old["current_step_metrics"]["false_alarm"],
        }
    return result


def summarize(pairs):
    arm_totals = {arm: Counter() for arm in ARMS}
    transitions = {f"{left}_to_{right}": Counter() for left, right in TRANSITIONS}
    usage = Counter()
    for pair in pairs:
        alternatives = pair["alternatives"]
        targets = set(pair["targets"])
        baseline_other = set(alternatives["provisional"]["completed_due"]) - targets
        for arm, action in alternatives.items():
            hits = set(action["completed_due"])
            arm_totals[arm].update({
                "target_hits": len(hits & targets), "hits_on_return_matched_targets": len(hits & set(pair["matched_current"])),
                "other_hits": len(hits - targets), "other_retained_vs_provisional": len(hits & baseline_other),
                "false_alarms": action["current_step_metrics"]["false_alarm"],
                "non_due": len(action["non_due_actions"]),
                "exact": int(not action["missed_due"] and not action["non_due_actions"]),
                "executor_errors": int(bool(action["executor_error"])),
            })
        for key, diff in pair["deltas"].items():
            movement = transitions[key]
            movement.update({name: len(value) for name, value in diff.items() if isinstance(value, list)})
            delta = diff["false_alarm_delta"]
            movement.update({"false_alarm_delta": delta, "pairs_with_more_false_alarms": int(delta > 0),
                             "pairs_with_fewer_false_alarms": int(delta < 0)})
        reply = pair["reply"]
        usage.update({"requests": 1, "input_tokens": reply["usage"]["prompt_tokens"],
                      "output_tokens": reply["usage"]["completion_tokens"], "retries": len(reply["retry_errors"])})
    return {"pairs": len(pairs), "target_opportunities": sum(len(pair["targets"]) for pair in pairs),
            "other_opportunities": sum(len(pair["due"]) - len(pair["targets"]) for pair in pairs),
            "arms": arm_totals, "transitions": transitions, "new_usage": usage}


def verify_source(bench, scenario, timing, audit):
    protocol = audit["protocol"]
    if (protocol["revision"] != REVISION or protocol["model"] != MODEL or protocol["sampling"] != SAMPLING
            or protocol["coverage_seeds"] != list(SEEDS) or protocol["channel_instruction"] != CHANNEL_INSTRUCTION):
        raise ValueError("Expected the frozen channel audit model and sampling contract")
    contexts = {}

    def collect(window, seed, history, menu, score_args, trial):
        if set(window["trials"]) != set(map(str, SEEDS)):
            raise ValueError(f"Expected frozen seed identities at {window['step_id']}")
        for key in ("plan_reply", "after_read_reply"):
            if trial[key]["actual_model"].casefold() != MODEL:
                raise ValueError(f"Model differs in the source audit at {window['step_id']}")
        prompt = [*history, {"role": "user", "content": CHOOSE_INSTRUCTION}]
        contexts[(window["step_id"], seed)] = (window, menu.copy(), score_args, trial["audit"], prompt)

    before = compact(audit)
    coverage(bench, scenario, timing, audit, None, lambda: None, observe=collect)
    if compact(audit) != before or len(contexts) != 42:
        raise ValueError("The 42 frozen pairs could not be reconstructed exactly")
    return contexts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    parser.add_argument("timing_archive", type=Path)
    parser.add_argument("channel_audit", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--api-utils", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    mode.add_argument("--check-inputs", action="store_true")
    args = parser.parse_args()
    if args.output.exists() != (args.resume or args.verify_only):
        parser.error("Use a new output path or --resume/--verify-only on an existing run")
    if not (args.verify_only or args.check_inputs) and args.api_utils is None:
        parser.error("--api-utils is required for the no-new-response arm")
    upstream = args.upstream.resolve()
    revision = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if revision != REVISION:
        parser.error(f"Expected PM-Bench {REVISION}, got {revision}")
    sys.path.insert(0, str(upstream / "sim"))
    import pm_bench as bench

    path = upstream / "data/synthetic_week_v9.json"
    scenario = bench.load_scenario(path)
    timing_bytes = args.timing_archive.read_bytes()
    audit_bytes = args.channel_audit.read_bytes()
    timing, audit = json.loads(timing_bytes), json.loads(audit_bytes)
    scenario_sha = digest(path.read_bytes())
    timing_sha = digest(timing_bytes)
    if (audit["protocol"]["source_sha256"] != timing_sha
            or audit["protocol"]["scenario_sha256"] != scenario_sha):
        raise ValueError("The original audit does not reference these exact inputs")
    contexts = verify_source(bench, scenario, timing, audit)
    if args.check_inputs:
        print("Verified the 42 frozen pairs before API calls")
        return
    protocol = {"revision": revision, "model": MODEL, "sampling": SAMPLING, "seeds": list(SEEDS),
                "actor_prompt": CHOOSE_INSTRUCTION, "scenario_sha256": scenario_sha,
                "timing_sha256": timing_sha, "channel_audit_sha256": digest(audit_bytes)}
    result = json.loads(args.output.read_bytes()) if args.output.exists() else {"protocol": protocol, "pairs": {}}
    if result["protocol"] != protocol:
        raise ValueError("The results file uses a different replay protocol")
    before = compact(result) if args.verify_only else None
    api = None if args.verify_only else QwenAPI(args.api_utils)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint():
        if not args.verify_only:
            save_artifact(args.output, result)

    for (step_id, seed), (window, menu, score_args, prior, prompt) in contexts.items():
        key = f"{step_id}:{seed}"
        pair = result["pairs"].setdefault(key, {"step_id": step_id, "seed": seed})
        raw = get_reply(pair, "reply", prompt, seed, api, checkpoint)
        if pair["reply"]["actual_model"].casefold() != MODEL or pair["reply"]["usage"] is None:
            raise ValueError(f"Missing model or token confirmation for {key}")
        evaluation = score_trial(bench, window, menu, raw, None, *score_args)
        target_ids = [target["task"] for target in window["targets"]]
        alternatives = {"provisional": prior["without_read"], "no_new_reply": evaluation,
                        "archived_reply": prior["with_read"]}
        fields = {"channel": prior["channel"], "read": prior["read"], "matched_current": prior["matched_current"],
                  "targets": target_ids, "due": window["due"], "alternatives": alternatives,
                  "deltas": deltas(alternatives, set(target_ids))}
        if any(name in pair and pair[name] != value for name, value in fields.items()):
            raise ValueError(f"Stored pair evaluation has changed: {key}")
        pair.update(fields)
        checkpoint()
        if api is not None:
            print(key, evaluation["completed_due"], evaluation["non_due_actions"], flush=True)
    pairs = list(result["pairs"].values())
    if len(pairs) != len(contexts):
        raise ValueError("The ledger includes unexpected pairs")
    result["summary"] = {
        "all": summarize(pairs),
        "matched_return": summarize([pair for pair in pairs if pair["matched_current"]]),
        "unmatched_return": summarize([pair for pair in pairs if not pair["matched_current"]]),
    }
    if before is not None and before != compact(result):
        raise ValueError("The saved run does not match offline replay")
    checkpoint()
    print(compact(result["summary"]), flush=True)


if __name__ == "__main__":
    main()
