"""Replay the 15 non-clock misses from the paired PM-Bench v9 timing run."""

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys

from audit_pmbench import REVISION
from run_evidence_pilot import MODEL, SAMPLING, SEEDS, QwenAPI, compact, save_artifact
from run_pmbench_probe import CHOOSE_INSTRUCTION, CONTROL, query_turn
from run_pmbench_timing import choose_from_reply, get_reply, match_cue, play_week, score_run


def planned_targets(result):
    arms = {}
    for run in result["runs"]:
        targets = {}
        for row in run["steps"]:
            for item in row["due"]:
                if item["required_channel"] in (None, "clock"):
                    continue
                if item["acted_on_time"] or item["matched_return_items"]:
                    raise ValueError(f"Expected an uncovered miss: {row['step_id']} {item['task']}")
                targets[(row["step_id"], item["task"])] = (item["required_channel"], item["cue_id"])
        arms[run["arm"]] = targets
    fixed = arms["fixed"]
    if fixed != arms["on_demand"] or len(fixed) != 15:
        raise ValueError("The completed timing arms must share exactly 15 non-clock misses")
    grouped = defaultdict(dict)
    for (step_id, task), (channel, cue) in fixed.items():
        grouped[step_id][task] = (channel, cue)
    return grouped


def make_windows(bench, scenario, source, targets):
    fixed = next(run for run in source["runs"] if run["arm"] == "fixed")
    if score_run(bench, scenario, fixed) != fixed["score"]:
        raise ValueError("The source trajectory no longer matches the native scorer")
    windows, contexts = [], {}

    def observe(step, history, menu, row, read_at_step):
        step_id = step["id"]
        if step_id not in targets:
            return
        common = history.copy()
        context_hash = hashlib.sha256(compact(common).encode("utf-8")).hexdigest()
        existing = bench.get_action_state_query_counts(row["native_action"])
        reads = {"original": None}

        def read(channel):
            if channel in existing:
                raise ValueError(f"Expected a new channel at {step_id}: {channel}")
            items = read_at_step(channel)
            if not items:
                raise ValueError(f"Expected a nonempty reply at {step_id}: {channel}")
            response = bench.build_state_query_response(channel, items, row["day"], step_id)
            return {"response": response, "delivered_text": bench.format_state_query_display(channel, items)}

        reads["unrelated"] = read(CONTROL)
        expected = []
        for task, (channel, cue) in sorted(targets[step_id].items()):
            cell = f"key:{task}"
            reads[cell] = read(channel)
            matched = match_cue(bench, step, channel, cue, reads[cell]["response"])
            if not matched or task not in menu.values() or channel == CONTROL:
                raise ValueError(f"The current cue or action is unavailable at {step_id}: {task}")
            expected.append({"task": task, "channel": channel, "cue_id": cue, "matched_items": matched})

        windows.append({"step_id": step_id, "day": row["day"], "index": row["index"],
                        "common_prompt_sha256": context_hash, "archive_action": row["native_action"],
                        "existing_reply": row["read"], "due": [item["task"] for item in row["due"]],
                        "targets": expected, "reads": reads, "trials": {}})
        contexts[step_id] = (common, menu.copy())

    for _ in play_week(bench, scenario, fixed, source["protocol"]["schedule"], None, lambda: None, observe=observe):
        pass
    if contexts.keys() != targets.keys():
        raise ValueError("One or more planned replay windows were not reconstructed")
    return windows, contexts


def score_trial(bench, window, menu, reply, read, prefix, updates, visibility):
    try:
        proposed = bench.parse_json_payload(reply)
    except ValueError:
        proposed = None
    choice, task_ids, issue = choose_from_reply(bench, proposed, menu)
    action = {**window["archive_action"], "choice": choice["choice"], "task_ids": task_ids}
    if read is not None:
        channel = read["response"]["channel"]
        action["state_queries"] = {**action["state_queries"], channel: 1}
    day, preceding_actions = prefix
    initial = [*preceding_actions, {**action, "task_ids": []}]
    acted = [*preceding_actions, action]
    score, *_ = bench.score_day(day, acted, updates.get("pre"), updates.get("by_step"), visibility)
    before, *_ = bench.score_day(day, initial, updates.get("pre"), updates.get("by_step"), visibility)
    current = {key: score[key] - before[key] for key in
               ("hit", "late", "false_alarm", "commission", "wrong_content", "set_fp")}
    due, chosen = set(window["due"]), set(task_ids)
    if current["hit"] != len(chosen & due) or current["set_fp"] != len(chosen - due):
        raise ValueError(f"Native scorer disagrees with the due set at {window['step_id']}")
    return {"choice": choice, "executor_error": issue, "native_action": action, "current_step_metrics": current,
            "completed_due": sorted(chosen & due), "missed_due": sorted(due - chosen),
            "non_due_actions": sorted(chosen - due)}


def prefixes_for(scenario, windows, source):
    indices = {window["step_id"] for window in windows}
    actions = {row["step_id"]: row["native_action"] for row in source["steps"]}
    prefixes = {}
    for day in scenario["days"]:
        preceding = []
        for index, step in enumerate(day["steps"]):
            step_id = step["id"]
            if step_id in indices:
                prefixes[step_id] = ({**day, "steps": day["steps"][:index + 1]}, preceding.copy())
            preceding.append(actions[step_id])
    return prefixes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    parser.add_argument("timing_result", type=Path)
    parser.add_argument("--api-utils", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    mode.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    existing = args.resume or args.verify_only
    if args.output.exists() != existing:
        parser.error("Use a new output path, or --resume or --verify-only with an existing run")
    if not (args.verify_only or args.prepare_only) and args.api_utils is None:
        parser.error("--api-utils is required for model requests")
    upstream = args.upstream.resolve()
    revision = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if revision != REVISION:
        parser.error(f"Expected PM-Bench {REVISION}, got {revision}")
    sys.path.insert(0, str(upstream / "sim"))
    import pm_bench as bench

    scenario_path = upstream / "data/synthetic_week_v9.json"
    scenario = bench.load_scenario(scenario_path)
    source_bytes = args.timing_result.read_bytes()
    source = json.loads(source_bytes)
    scenario_hash = hashlib.sha256(scenario_path.read_bytes()).hexdigest()
    if source["protocol"]["scenario_sha256"] != scenario_hash or source["protocol"]["model"] != MODEL:
        raise ValueError("Expected the same native scenario and Qwen route as the timing run")
    if any(CONTROL == bench.required_monitor_channel(task) for day in scenario["days"] for task in day["tasks"]):
        raise ValueError(f"Unrelated channel {CONTROL} is attached to a scenario task")
    targets = planned_targets(source)
    windows, contexts = make_windows(bench, scenario, source, targets)
    fixed = next(run for run in source["runs"] if run["arm"] == "fixed")
    prefixes = prefixes_for(scenario, windows, fixed)
    updates = bench.build_updates_by_day(scenario)
    visibility = bench.normalize_state_visibility(scenario)
    protocol = {"revision": REVISION, "scenario_sha256": scenario_hash, "source_arm": "fixed",
                "source_sha256": hashlib.sha256(source_bytes).hexdigest(), "model": MODEL,
                "seeds": list(SEEDS), "sampling": SAMPLING, "actor": CHOOSE_INSTRUCTION,
                "unrelated_channel": CONTROL, "target_count": sum(map(len, targets.values()))}
    result = {"protocol": protocol, "windows": windows}
    if existing:
        saved = json.loads(args.output.read_text(encoding="utf-8"))
        for new, old in zip(windows, saved["windows"]):
            new["trials"] = old["trials"]
        if result != saved:
            raise ValueError("The existing replay artifact uses a different protocol")

    def checkpoint():
        if not args.verify_only:
            save_artifact(args.output, result)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint()
    if args.prepare_only:
        print(f"Prepared {protocol['target_count']} targets in {len(windows)} windows")
        return

    api = None if args.verify_only else QwenAPI(args.api_utils)
    for window in windows:
        step_id = window["step_id"]
        common, menu = contexts[step_id]
        for seed in SEEDS:
            cells = list(window["reads"])
            random.Random(seed + window["index"]).shuffle(cells)
            for cell in cells:
                key = f"{seed}:{cell}"
                trial = window["trials"].setdefault(key, {"step_id": step_id})
                read = window["reads"][cell]
                prompt = common.copy()
                if read is not None:
                    observation = read["response"]
                    prompt.extend(query_turn(bench, observation["channel"], observation["items"]))
                    if prompt[-1]["content"] != read["delivered_text"]:
                        raise ValueError(f"Model-visible response drift at {step_id}")
                prompt.append({"role": "user", "content": CHOOSE_INSTRUCTION})
                raw = get_reply(trial, "response", prompt, seed, api, checkpoint)
                reply = trial["response"]
                if reply["actual_model"].casefold() != MODEL or reply["usage"] is None:
                    raise ValueError(f"Missing model or usage confirmation at {step_id}")
                evaluation = score_trial(bench, window, menu, raw, read, prefixes[step_id],
                                         updates[window["day"]], visibility)
                if "evaluation" in trial and trial["evaluation"] != evaluation:
                    raise ValueError(f"Stored replay outcome drift at {step_id}: {key}")
                trial["evaluation"] = evaluation
                checkpoint()
                if not args.verify_only:
                    print(step_id, key, evaluation["completed_due"], evaluation["non_due_actions"], flush=True)
    if args.verify_only and result != json.loads(args.output.read_text(encoding="utf-8")):
        raise ValueError("The complete artifact did not match the offline replay")
    print(f"Verified {sum(len(window['trials']) for window in windows)} distinct responses")


if __name__ == "__main__":
    main()
