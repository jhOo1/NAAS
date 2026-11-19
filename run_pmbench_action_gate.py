"""Audit changes in the action set using frozen PM-Bench histories and replies."""

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys

from audit_pmbench import REVISION
from run_evidence_pilot import MODEL, SAMPLING, QwenAPI, compact, save_artifact
from run_pmbench_nonclock import score_trial
from run_pmbench_probe import query_turn
from run_pmbench_reanswer import digest, verify_source
from run_pmbench_timing import get_reply


GATE_RULES = (
    "Audit the proposed changes to this step's prospective action set, using only the visible conversation. "
    "The first set is the no-new-read decision; the second is the proposed decision after the tool reply. "
    "Review exactly the listed additions and removals. For each addition, require a concrete current trigger "
    "for the specific action, object and latest instruction. A related event or a nearby time is insufficient. "
    "The return may mix prior and new entries; do not treat every returned entry as a fresh event. "
    "Use the latest visible instruction and the exact time when an action requires one. "
    "Accept a removal only when the action lacks a current basis, was superseded, or conflicts with a "
    "better-supported action. Keep an independently supported action when another action becomes due. "
    "Do not assume the previously selected actions were all correct. "
)
GATE_INSTRUCTION = (
    GATE_RULES
    + 'Return only JSON with keys "add" and "remove". Each maps every listed handle for that operation '
    'to {"accept":true or false,"why":"brief visible evidence or reason"}. Use empty objects for empty lists. '
    "Never request a tool or propose any action outside the listed changes."
)
CRITERIA = {"min_false_alarm_reduction": 3, "max_retained_target_losses": 0,
            "max_retained_other_losses": 0, "min_other_restored": 1}


def decode_review(bench, raw, changes):
    try:
        parsed = bench.parse_json_payload(raw)
        if not isinstance(parsed, dict) or parsed.keys() != changes.keys():
            raise ValueError("expected add and remove dictionaries")
        for kind, decisions in parsed.items():
            if not isinstance(decisions, dict) or decisions.keys() != set(changes[kind]):
                raise ValueError(f"wrong {kind} handles")
            for detail in decisions.values():
                if (not isinstance(detail, dict) or detail.keys() != {"accept", "why"}
                        or type(detail["accept"]) is not bool or not isinstance(detail["why"], str)
                        or not detail["why"].strip()):
                    raise ValueError(f"invalid {kind} decision")
        return parsed, None
    except ValueError as error:
        return None, str(error)


def gated_choice(before, after, review):
    if review is None:
        return after
    added = {handle for handle, record in review["add"].items() if record["accept"]}
    removed = {handle for handle, record in review["remove"].items() if record["accept"]}
    wanted = (set(before["task_ids"]) - removed) | added
    order = dict.fromkeys(after["task_ids"] + before["task_ids"])
    return {**after, "task_ids": [handle for handle in order if handle in wanted]}


def arm_metrics(pairs, arm):
    counts = Counter()
    for pair in pairs:
        outcome = pair[arm]
        target = set(pair["targets"])
        baseline = set(pair["archived_reply"]["completed_due"])
        pre_read = set(pair["no_new_reply"]["completed_due"])
        provisional = set(pair["provisional"]["completed_due"])
        hits = set(outcome["completed_due"])
        counts.update({
            "target_hits": len(hits & target), "target_losses_from_c": len((baseline & target) - hits),
            "other_hits": len(hits - target), "other_losses_from_c": len((baseline - target) - hits),
            "other_restored_from_b": len((hits & pre_read) - baseline - target),
            "other_retained_from_b": len((hits & pre_read) - target),
            "other_retained_from_a": len((hits & provisional) - target),
            "false_alarms": outcome["current_step_metrics"]["false_alarm"],
            "non_due": len(outcome["non_due_actions"]),
            "exact": int(not outcome["missed_due"] and not outcome["non_due_actions"]),
        })
    return dict(counts)


def acceptance_checks(c, gate):
    return {
        "false_alarms": c["false_alarms"] - gate["false_alarms"] >= CRITERIA["min_false_alarm_reduction"],
        "non_due": gate["non_due"] <= c["non_due"],
        "target_retention": gate["target_losses_from_c"] <= CRITERIA["max_retained_target_losses"],
        "other_retention": gate["other_losses_from_c"] <= CRITERIA["max_retained_other_losses"],
        "other_restored": gate["other_restored_from_b"] >= CRITERIA["min_other_restored"],
    }


def summarize(pairs):
    c = arm_metrics(pairs, "archived_reply")
    gate = arm_metrics(pairs, "gate")
    checks = acceptance_checks(c, gate)
    replies = [pair["reply"] for pair in pairs if "reply" in pair]
    return {"pairs": len(pairs), "archived_reply": c, "union": arm_metrics(pairs, "union"), "gate": gate,
            "checks": checks, "passed": all(checks.values()), "requests": len(replies),
            "review_errors": sum(bool(pair["review_error"]) for pair in pairs),
            "input_tokens": sum(reply["usage"]["prompt_tokens"] for reply in replies),
            "output_tokens": sum(reply["usage"]["completion_tokens"] for reply in replies)}


def frozen_pairs(bench, scenario, timing, audit, previous):
    contexts = verify_source(bench, scenario, timing, audit)
    pairs = {}
    for (step_id, seed), (window, menu, score_args, prior, prompt) in contexts.items():
        key = f"{step_id}:{seed}"
        saved = previous["pairs"][key]
        if (saved["read"] != prior["read"] or saved["matched_current"] != prior["matched_current"]
                or saved["targets"] != [item["task"] for item in window["targets"]]
                or saved["due"] != window["due"]
                or saved["reply"]["prompt_sha256"] != digest(compact(prompt).encode("utf-8"))
                or saved["reply"]["actual_model"].casefold() != MODEL):
            raise ValueError(f"Source reply or prompt differs in {key}")
        b = score_trial(bench, window, menu, saved["reply"]["raw_output"], None, *score_args)
        expected = {"provisional": prior["without_read"], "no_new_reply": b, "archived_reply": prior["with_read"]}
        if saved["alternatives"] != expected:
            raise ValueError(f"Source executor result differs in {key}")
        pairs[key] = (window, menu, score_args, prior, prompt[:-1], expected, saved["targets"])
    if pairs.keys() != previous["pairs"].keys():
        raise ValueError("Expected all and only the original pairs")
    return pairs


def run_pair(bench, result, inputs, seed, api, checkpoint):
    window, menu, score_args, prior, history, base, targets = inputs
    before, after = (base[arm]["choice"] for arm in ("no_new_reply", "archived_reply"))
    left, right = before["task_ids"], after["task_ids"]
    changes = {"add": [handle for handle in right if handle not in left],
               "remove": [handle for handle in left if handle not in right]}
    read = prior["read"]
    obs = read["response"]
    turn = query_turn(bench, obs["channel"], obs["items"])
    if turn[-1]["content"] != read["delivered_text"]:
        raise ValueError(f"Model-visible return differs in {window['step_id']}")
    review, error = None, None
    if changes["add"] or changes["remove"]:
        review_input = {"before": left, "after": right, "changes": changes}
        messages = [*history, *turn, {"role": "user", "content": GATE_INSTRUCTION + "\n" + compact(review_input)}]
        raw = get_reply(result, "reply", messages, seed, api, checkpoint)
        if result["reply"]["actual_model"].casefold() != MODEL or result["reply"]["usage"] is None:
            raise ValueError("Gate did not return the expected model and usage")
        review, error = decode_review(bench, raw, changes)
    choice = gated_choice(before, after, review)
    union = {**after, "task_ids": list(dict.fromkeys(right + left))}
    gate_score = score_trial(bench, window, menu, compact(choice), read, *score_args)
    union_score = score_trial(bench, window, menu, compact(union), read, *score_args)
    if gate_score["executor_error"] or union_score["executor_error"]:
        raise ValueError("Gate or union could not be executed with the native action contract")
    return {**base, "targets": targets, "read": read, "matched_current": prior["matched_current"],
            "changes": changes, "review": review, "review_error": error, "gate": gate_score, "union": union_score}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    parser.add_argument("timing_archive", type=Path)
    parser.add_argument("channel_audit", type=Path)
    parser.add_argument("reanswer_archive", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--api-utils", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    mode.add_argument("--check-inputs", action="store_true")
    args = parser.parse_args()
    if args.output.exists() != (args.resume or args.verify_only):
        parser.error("Use a new output path, or --resume/--verify-only on an existing run")
    if not (args.verify_only or args.check_inputs) and args.api_utils is None:
        parser.error("--api-utils is required for the gate")
    upstream = args.upstream.resolve()
    revision = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if revision != REVISION:
        raise ValueError(f"Expected PM-Bench {REVISION}, got {revision}")
    sys.path.insert(0, str(upstream / "sim"))
    import pm_bench as bench

    path = upstream / "data/synthetic_week_v9.json"
    scenario = bench.load_scenario(path)
    inputs = [args.timing_archive.read_bytes(), args.channel_audit.read_bytes(), args.reanswer_archive.read_bytes()]
    timing, audit, previous = map(json.loads, inputs)
    scenario_sha = digest(path.read_bytes())
    if (previous["protocol"]["timing_sha256"] != digest(inputs[0])
            or previous["protocol"]["channel_audit_sha256"] != digest(inputs[1])
            or previous["protocol"]["scenario_sha256"] != scenario_sha):
        raise ValueError("Frozen source digests differ")
    pairs = frozen_pairs(bench, scenario, timing, audit, previous)
    if args.check_inputs:
        print(f"Verified {len(pairs)} frozen pairs")
        return
    protocol = {"revision": REVISION, "scenario_sha256": scenario_sha, "reanswer_sha256": digest(inputs[2]),
                "model": MODEL, "sampling": SAMPLING, "instruction": GATE_INSTRUCTION, "criteria": CRITERIA}
    result = json.loads(args.output.read_bytes()) if args.output.exists() else {"protocol": protocol, "pairs": {}}
    if result["protocol"] != protocol:
        raise ValueError("Existing ledger uses a different gate protocol")
    before = compact(result) if args.verify_only else None
    api = None if args.verify_only else QwenAPI(args.api_utils)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint():
        if not args.verify_only:
            save_artifact(args.output, result)

    for key, inputs_for_pair in pairs.items():
        step_id, seed = key.split(":")
        row = result["pairs"].setdefault(key, {"step_id": step_id, "seed": int(seed)})
        computed = run_pair(bench, row, inputs_for_pair, int(seed), api, checkpoint)
        if any(field in row and row[field] != value for field, value in computed.items()):
            raise ValueError(f"Saved gate outcome differs in {key}")
        row.update(computed)
        checkpoint()
        if api is not None:
            print(key, "reply" in row, row["gate"]["completed_due"], row["gate"]["non_due_actions"],
                  row["review_error"], flush=True)
    if result["pairs"].keys() != pairs.keys():
        raise ValueError("The gate result contains unexpected pairs")
    result["summary"] = summarize(result["pairs"].values())
    if before is not None and before != compact(result):
        raise ValueError("The saved gate result differs from offline replay")
    checkpoint()
    print(compact(result["summary"]), flush=True)


if __name__ == "__main__":
    main()
