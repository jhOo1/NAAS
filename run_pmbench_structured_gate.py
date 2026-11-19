"""Compare short action audits with and without a visible evidence index."""

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import subprocess
import sys

from audit_pmbench import REVISION
from run_evidence_pilot import MODEL, SAMPLING, QwenAPI, compact, save_artifact
from run_pmbench_action_gate import CRITERIA, GATE_RULES, acceptance_checks, arm_metrics, frozen_pairs, gated_choice
from run_pmbench_nonclock import score_trial
from run_pmbench_probe import query_turn
from run_pmbench_reanswer import digest
from run_pmbench_timing import get_reply


ARMS = ("short", "structured")
SHORT_FORMAT = (
    'Return only {"add":[],"remove":[]}, with accepted handles in the corresponding array. '
    "An omitted handle is a rejection. Do not include reasons, request a tool, or propose other actions."
)
INDEX_RULES = (
    "The visible_evidence_index lists verbatim day instructions and up to two recent narrative mentions "
    "per changed action. A later explicit update supersedes an older instruction for the same action. "
    "Align that current trigger to the object, attribute and time in the latest actual tool reply or visible narrative. "
    "A related object or different attribute is not evidence for this action. "
    "A missing quotation only means the index has no exact lexical match; consult the full conversation. "
    "Do not invent an event time or freshness for a returned item. Treat unsupported details as unknown. "
    "Preserve an independently supported action when the reply does not invalidate it. "
)
MENU_LABEL = re.compile(r"^- (task_\d+): (.+)$", re.MULTILINE)
WORD = re.compile(r"[\w]+")


def normalized(text):
    return " ".join(WORD.findall(text.casefold()))


def visible_index(history, handles):
    """Only quote the text that the archived actor could read."""
    labels = dict(MENU_LABEL.findall(history[-1]["content"]))
    user_turns = [message["content"] for message in history if message["role"] == "user"]
    day_open = max(index for index, turn in enumerate(user_turns) if turn.startswith("=== "))
    daily = user_turns[0].splitlines()
    day = user_turns[day_open].splitlines()
    instructions = [line[2:] for line in daily + day if line.startswith("- ")]
    narratives = [turn.splitlines()[0] for turn in user_turns[day_open + 1:] if "\n\nStep action menu:" in turn]
    result = {}
    for handle in handles:
        label = labels[handle]
        phrase = f" {normalized(label)} "
        matches = [line for line in instructions if phrase in f" {normalized(line)} "]
        mentions = [line for line in narratives if phrase in f" {normalized(line)} "]
        result[handle] = {"action": label, "day_instruction": matches[-1] if matches else None,
                          "recent_narrative_mentions_oldest_first": list(dict.fromkeys(reversed(mentions)))[:2][::-1]}
    return result


def decode_short(bench, raw, changes):
    try:
        parsed = bench.parse_json_payload(raw)
        if not isinstance(parsed, dict) or parsed.keys() != changes.keys():
            raise ValueError("expected add and remove lists")
        for operation, accepted in parsed.items():
            if (not isinstance(accepted, list)
                    or any(not isinstance(handle, str) or handle not in changes[operation] for handle in accepted)
                    or len(set(accepted)) != len(accepted)):
                raise ValueError(f"invalid accepted handles for {operation}")
        return {kind: {handle: {"accept": True} for handle in handles} for kind, handles in parsed.items()}, None
    except ValueError as error:
        return None, str(error)


def run_pair(bench, row, inputs, seed, arm_order, api, checkpoint):
    window, menu, score_args, prior, history, base, targets = inputs
    before, after = (base[arm]["choice"] for arm in ("no_new_reply", "archived_reply"))
    left, right = before["task_ids"], after["task_ids"]
    changes = {"add": [handle for handle in right if handle not in left],
               "remove": [handle for handle in left if handle not in right]}
    handles = changes["add"] + changes["remove"]
    read = prior["read"]
    obs = read["response"]
    turn = query_turn(bench, obs["channel"], obs["items"])
    if turn[-1]["content"] != read["delivered_text"]:
        raise ValueError(f"Archived tool reply differs in {window['step_id']}")
    index = visible_index(history, handles) if handles else {}
    trials = row.setdefault("trials", {})
    for arm in arm_order:
        trial = trials.setdefault(arm, {"step_id": window["step_id"]})
        review, error = None, None
        if handles:
            data = {"before": left, "after": right, "changes": changes}
            rules = GATE_RULES
            if arm == "structured":
                rules += INDEX_RULES
                data["visible_evidence_index"] = index
            prompt = [*history, *turn, {"role": "user", "content": rules + SHORT_FORMAT + "\n" + compact(data)}]
            raw = get_reply(trial, "reply", prompt, seed, api, checkpoint)
            if trial["reply"]["actual_model"].casefold() != MODEL or trial["reply"]["usage"] is None:
                raise ValueError("Expected Qwen3.5-27B and token usage for every review")
            review, error = decode_short(bench, raw, changes)
        choice = gated_choice(before, after, review)
        evaluation = score_trial(bench, window, menu, compact(choice), read, *score_args)
        if evaluation["executor_error"]:
            raise ValueError(f"Invalid native action at {window['step_id']}")
        computed = {"review": review, "review_error": error, "outcome": evaluation}
        if any(key in trial and trial[key] != value for key, value in computed.items()):
            raise ValueError(f"Review replay differs at {window['step_id']} {arm}")
        trial.update(computed)
        checkpoint()
    return {**base, "read": read, "matched_current": prior["matched_current"], "targets": targets,
            "changes": changes, "visible_evidence_index": index}


def summarize(rows):
    pairs = [{**row, **{arm: row["trials"][arm]["outcome"] for arm in ARMS}} for row in rows]
    baseline = arm_metrics(pairs, "archived_reply")
    result = {}
    for arm in ARMS:
        metrics = arm_metrics(pairs, arm)
        checks = acceptance_checks(baseline, metrics)
        replies = [row["trials"][arm]["reply"] for row in rows if "reply" in row["trials"][arm]]
        result[arm] = {"metrics": metrics, "checks": checks, "passed": all(checks.values()),
                       "requests": len(replies),
                       "errors": sum(bool(row["trials"][arm]["review_error"]) for row in rows),
                       "prompt_tokens": sum(reply["usage"]["prompt_tokens"] for reply in replies),
                       "completion_tokens": sum(reply["usage"]["completion_tokens"] for reply in replies),
                       "retries": sum(len(reply["retry_errors"]) for reply in replies)}
    movement = Counter()
    for row in pairs:
        short, structured = (row[arm] for arm in ARMS)
        left, right = set(short["completed_due"]), set(structured["completed_due"])
        targets = set(row["targets"])
        alarm_delta = structured["current_step_metrics"]["false_alarm"] - short["current_step_metrics"]["false_alarm"]
        movement.update({"false_alarm_delta": alarm_delta, "pairs_fewer_false_alarms": int(alarm_delta < 0),
                         "pairs_more_false_alarms": int(alarm_delta > 0), "target_gain": len((right - left) & targets),
                         "target_loss": len((left - right) & targets), "other_gain": len(right - left - targets),
                         "other_loss": len(left - right - targets)})
    promoted = result["structured"]["passed"] and movement["false_alarm_delta"] < 0 and not (
        movement["target_loss"] or movement["other_loss"]
    )
    return {"pairs": len(rows), "archived_reply": baseline, "arms": result,
            "structured_vs_short": dict(movement), "structured_has_increment": promoted}


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
        parser.error("Use a new output path or --resume/--verify-only on an existing run")
    if not (args.verify_only or args.check_inputs) and args.api_utils is None:
        parser.error("--api-utils is required")
    upstream = args.upstream.resolve()
    revision = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if revision != REVISION:
        raise ValueError(f"Expected PM-Bench {REVISION}, got {revision}")
    sys.path.insert(0, str(upstream / "sim"))
    import pm_bench as bench

    path = upstream / "data/synthetic_week_v9.json"
    scenario = bench.load_scenario(path)
    inputs = [args.timing_archive.read_bytes(), args.channel_audit.read_bytes(), args.reanswer_archive.read_bytes()]
    timing, audit, reanswer = map(json.loads, inputs)
    scenario_sha = digest(path.read_bytes())
    source_protocol = reanswer["protocol"]
    if (source_protocol["timing_sha256"] != digest(inputs[0])
            or source_protocol["channel_audit_sha256"] != digest(inputs[1])
            or source_protocol["scenario_sha256"] != scenario_sha):
        raise ValueError("Frozen source digests differ")
    pairs = frozen_pairs(bench, scenario, timing, audit, reanswer)
    protocol = {"revision": REVISION, "scenario_sha256": scenario_sha, "reanswer_sha256": digest(inputs[2]),
                "model": MODEL, "sampling": SAMPLING, "gate_rules": GATE_RULES, "short_format": SHORT_FORMAT,
                "index_rules": INDEX_RULES, "criteria": CRITERIA}
    if args.check_inputs:
        for window, _, _, _, history, base, _ in pairs.values():
            before, after = (set(base[arm]["choice"]["task_ids"]) for arm in ("no_new_reply", "archived_reply"))
            visible_index(history, sorted(before ^ after))
        print(f"Verified {len(pairs)} frozen pairs and visible menu indexes")
        return
    result = json.loads(args.output.read_bytes()) if args.output.exists() else {"protocol": protocol, "pairs": {}}
    if result["protocol"] != protocol:
        raise ValueError("Existing ledger uses a different protocol")
    original = compact(result) if args.verify_only else None
    api = None if args.verify_only else QwenAPI(args.api_utils)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint():
        if not args.verify_only:
            save_artifact(args.output, result)

    for offset, (key, inputs_for_pair) in enumerate(pairs.items()):
        step_id, seed = key.split(":")
        row = result["pairs"].setdefault(key, {"step_id": step_id, "seed": int(seed)})
        computed = run_pair(bench, row, inputs_for_pair, int(seed), ARMS if offset % 2 == 0 else ARMS[::-1],
                            api, checkpoint)
        if any(field in row and row[field] != value for field, value in computed.items()):
            raise ValueError(f"Frozen pair outcome differs in {key}")
        row.update(computed)
        checkpoint()
        if api is not None:
            print(key, [(arm, row["trials"][arm]["outcome"]["current_step_metrics"]["false_alarm"],
                         row["trials"][arm]["review_error"]) for arm in ARMS], flush=True)
    if result["pairs"].keys() != pairs.keys():
        raise ValueError("Unexpected frozen pairs in the output")
    result["summary"] = summarize(list(result["pairs"].values()))
    if original is not None and original != compact(result):
        raise ValueError("The saved experiment differs from offline replay")
    checkpoint()
    print(compact(result["summary"]), flush=True)


if __name__ == "__main__":
    main()
