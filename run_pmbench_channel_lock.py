"""Audit a frozen channel policy, then lock actual read sources for a timing pair."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from audit_pmbench import REVISION, day_audit
from run_evidence_pilot import MODEL, SAMPLING, SEEDS, QwenAPI, compact, save_artifact
from run_pmbench_nonclock import planned_targets, prefixes_for, score_trial
from run_pmbench_probe import CHOOSE_INSTRUCTION, query_turn
from run_pmbench_timing import (
    ARMS, BUDGET, NO_ACTION, SEED, get_reply, match_cue, plan_from_reply, play_week, score_run,
)


CHANNEL_INSTRUCTION = (
    "Choose the single listed native state channel that you would check at this event if you had one read. "
    "From the visible instructions, current action menu, narrative and previous replies, identify a pending "
    "action with an unresolved cue. Prefer a specific non-clock source when it can check that cue. "
    "Use topical cues in the latest event to prioritize among pending tasks, but topical similarity alone "
    "does not make an action due. Never choose a source just because it appeared in a previous reply. "
    "If no specific non-clock source is supported, choose clock. Also give the native action you would take "
    "without any additional read. Do not act on a hidden cue solely because its action is listed. "
    'Output exactly {"channel":"calendar","read_now":true,'
    '"provisional":{"action":"choose","choice":"A","task_ids":[],"channel":"NONE"}} '
    "using the correct values rather than the examples. read_now indicates whether the chosen channel "
    "could usefully resolve a pending condition at this event."
)
GATE = {"min_windows": 10, "majority_matched_windows": 8, "majority_matched_targets": 5,
        "majority_matched_sources": 3, "min_grounded_hits": 8, "other_due_retained_pct": 90,
        "max_extra_false_alarms": 1}


def selection(bench, history, row, seed, channels, api, checkpoint):
    prompt = [*history, {"role": "user", "content": CHANNEL_INSTRUCTION}]
    raw = get_reply(row, "plan_reply", prompt, seed, api, checkpoint)
    return plan_from_reply(bench, raw, channels)


def coverage(bench, scenario, source, result, api, checkpoint):
    targets = planned_targets(source)
    archive = next(run for run in source["runs"] if run["arm"] == "fixed")
    prefixes = prefixes_for(scenario, [{"step_id": step_id} for step_id in targets], archive)
    updates = bench.build_updates_by_day(scenario)
    visibility = bench.normalize_state_visibility(scenario)
    channels = bench.normalize_state_channels(scenario)
    windows = result.setdefault("coverage_windows", {})

    def audit(step, history, menu, row, read_at_step):
        step_id = step["id"]
        if step_id not in targets:
            return
        due = [item["task"] for item in row["due"]]
        # Remove the archive's current read from both cells. Earlier reads and actions stay in the prefix.
        action = {**row["native_action"], "state_queries": {}, "check_time": 0}
        current = {"step_id": step_id, "day": row["day"], "due": due, "archive_action": action}
        expected = [{"task": task, "channel": channel, "cue": cue}
                    for task, (channel, cue) in sorted(targets[step_id].items())]
        window = windows.setdefault(step_id, {**current, "targets": expected, "trials": {}})
        for seed in SEEDS:
            trial = window["trials"].setdefault(str(seed), {"step_id": step_id})
            plan, plan_error = selection(bench, history, trial, seed, channels, api, checkpoint)
            channel = plan["channel"]
            read = {"response": bench.build_state_query_response(channel, read_at_step(channel), row["day"], step_id)}
            items = read["response"]["items"]
            messages = [*history, *query_turn(bench, channel, items)]
            read["delivered_text"] = messages[-1]["content"]
            matches = {item["task"]: matched for item in expected
                       if (matched := match_cue(bench, step, item["channel"], item["cue"], read["response"]))}
            # Verify the scored evidence is the exact native payload in the actor prompt.
            if read["delivered_text"] != bench.format_state_query_display(channel, items):
                raise ValueError(f"Model-visible response drift at {step_id}")
            messages.append({"role": "user", "content": CHOOSE_INSTRUCTION})
            raw = get_reply(trial, "after_read_reply", messages, seed, api, checkpoint)
            args = (prefixes[step_id], updates[row["day"]], visibility)
            before = score_trial(bench, window, menu, compact(plan["provisional"]), None, *args)
            after = score_trial(bench, window, menu, raw, read, *args)
            computed = {"plan_error": plan_error, "channel": channel, "read": read,
                        "matched_current": matches, "without_read": before, "with_read": after}
            if "audit" in trial and trial["audit"] != computed:
                raise ValueError(f"Coverage replay diverged at {step_id} seed {seed}")
            trial["audit"] = computed
            checkpoint()
            if api is not None:
                print("coverage", step_id, seed, channel, sorted(matches), after["completed_due"], flush=True)

    for _ in play_week(bench, scenario, archive, source["protocol"]["schedule"],
                       None, lambda: None, before_read=audit):
        pass
    summary = score_coverage(windows)
    result["coverage_score"] = summary
    checkpoint()
    print("coverage decision", compact(summary), flush=True)


def score_coverage(windows):
    totals = Counter()
    stable_windows, stable_targets, stable_sources = [], [], set()
    majority = len(SEEDS) // 2 + 1
    for step_id, window in windows.items():
        by_task = Counter()
        matched_runs = 0
        target_ids = {item["task"] for item in window["targets"]}
        for trial in window["trials"].values():
            audit = trial["audit"]
            matches = set(audit["matched_current"])
            before, after = audit["without_read"], audit["with_read"]
            before_hit, after_hit = set(before["completed_due"]), set(after["completed_due"])
            other_before = before_hit - target_ids
            by_task.update(matches)
            matched_runs += bool(matches)
            totals.update({"trials": 1, "matched_reads": int(bool(matches)), "matched_targets": len(matches),
                           "targets": len(target_ids), "without_read_target_hits": len(before_hit & target_ids),
                           "with_read_target_hits": len(after_hit & target_ids),
                           "grounded_target_hits": len(after_hit & matches),
                           "other_before": len(other_before), "other_retained": len(other_before & after_hit),
                           "other_after": len(after_hit - target_ids),
                           "false_alarms_before": before["current_step_metrics"]["false_alarm"],
                           "false_alarms_after": after["current_step_metrics"]["false_alarm"],
                           "non_due_before": len(before["non_due_actions"]),
                           "non_due_after": len(after["non_due_actions"]),
                           "format_errors": sum((bool(audit["plan_error"]), bool(before["executor_error"]),
                                                 bool(after["executor_error"])))})
        if matched_runs >= majority:
            stable_windows.append(step_id)
        for item in window["targets"]:
            if by_task[item["task"]] >= majority:
                stable_targets.append(item["task"])
                stable_sources.add(item["channel"])
    checks = {
        "window_count": len(windows) >= GATE["min_windows"],
        "window_coverage": len(stable_windows) >= GATE["majority_matched_windows"],
        "task_coverage": len(set(stable_targets)) >= GATE["majority_matched_targets"],
        "source_coverage": len(stable_sources) >= GATE["majority_matched_sources"],
        "grounded_execution": totals["grounded_target_hits"] >= GATE["min_grounded_hits"],
        "other_due_retained": 100 * totals["other_retained"] >= GATE["other_due_retained_pct"] * totals["other_before"],
        "false_alarm_delta": totals["false_alarms_after"] - totals["false_alarms_before"] <= GATE["max_extra_false_alarms"],
    }
    return {**totals, "windows": len(windows), "stable_windows": stable_windows,
            "stable_targets": sorted(set(stable_targets)), "stable_sources": sorted(stable_sources),
            "checks": checks, "passed": all(checks.values())}


def public_block_contexts(bench, scenario, schedule):
    """Read-free common reference prefixes, with native no-op menus and public events only."""
    beginnings = {block["first"] for block in schedule}
    channels = bench.list_state_channels(scenario)
    history = [{"role": "system", "content": bench.build_llm_system_prompt(False, sorted(channels))},
               {"role": "user", "content": "\n".join(bench.DAILY_TASK_HEADER_LINES)}]
    updates = bench.build_updates_by_day(scenario)
    contexts = {}
    index = 0
    for day in scenario["days"]:
        name = day["name"]
        history.append({"role": "user", "content": "\n".join([f"=== {name} ===", *day.get("start_instructions", [])])})
        lures = bench.normalize_lure_catalog(day.get("lures", []))
        actions = [{"day": name, "step_id": step["id"], "choice": "A", "task_ids": [], "state_queries": {}}
                   for step in day["steps"]]

        def observe(step, states, active, due, archived, replies, read):
            nonlocal index
            index += 1
            handles, _ = bench.build_day_handle_maps(states, lures, seed_key=f"{name}:handles")
            menu, _ = bench.build_step_action_menu(states, active, lures, handles, name, step["id"])
            visible = "\n".join([step["text"], *step["options"],
                                 bench.format_action_menu(menu, header="Step action menu")])
            history.append({"role": "user", "content": visible})
            if index in beginnings:
                contexts[index] = (step["id"], history.copy())
            history.append({"role": "assistant", "content": compact(NO_ACTION)})

        day_audit(bench, scenario, day, actions, updates[name], observe)
    return contexts


def timing_pair(bench, scenario, result, api, checkpoint):
    schedule = result["protocol"]["schedule"]
    contexts = public_block_contexts(bench, scenario, schedule)
    sources = result.setdefault("source_assignments", {})
    channels = bench.normalize_state_channels(scenario)
    locked = []
    for i, block in enumerate(schedule, start=1):
        index = block["first"]
        step_id, history = contexts[index]
        assignment = sources.setdefault(str(i), {"step_id": step_id, "index": index})
        plan, error = selection(bench, history, assignment, SEED + index - 1, channels, api, checkpoint)
        if "channel" in assignment and assignment["channel"] != plan["channel"]:
            raise ValueError(f"Frozen channel assignment changed in block {i}")
        assignment.update({"channel": plan["channel"], "plan_error": error})
        locked.append(plan["channel"])
        checkpoint()
    if len(locked) != BUDGET:
        raise ValueError("Expected a frozen source for each of the nine blocks")
    print("locked source order", locked, flush=True)
    runs = result.setdefault("timing_runs", [{"arm": arm, "steps": []} for arm in ARMS])
    runners = {run["arm"]: play_week(bench, scenario, run, schedule, api, checkpoint, locked_channels=locked)
               for run in runs}
    for offset in range(schedule[-1]["last"]):
        for arm in (ARMS if offset % 2 == 0 else ARMS[::-1]):
            row = next(runners[arm])
            if api is not None and (row["read"] or (offset + 1) % 10 == 0):
                print(arm, offset + 1, row["step_id"], row["timing_reason"],
                      row["read"]["channel"] if row["read"] else None, flush=True)
    for run in runs:
        actual = [row["read"]["channel"] for row in run["steps"] if row["read"]]
        if actual != locked:
            raise ValueError(f"Actual source order departed from the shared plan: {run['arm']}")
        run["score"] = score_run(bench, scenario, run)
    checkpoint()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    parser.add_argument("timing_archive", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--api-utils", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if args.output.exists() != (args.resume or args.verify_only):
        parser.error("Use a new output path or --resume/--verify-only with an existing run")
    if not args.verify_only and args.api_utils is None:
        parser.error("--api-utils is required for model requests")
    upstream = args.upstream.resolve()
    revision = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if revision != REVISION:
        parser.error(f"Expected PM-Bench {REVISION}, got {revision}")
    sys.path.insert(0, str(upstream / "sim"))
    import pm_bench as bench

    path = upstream / "data/synthetic_week_v9.json"
    scenario = bench.load_scenario(path)
    source_bytes = args.timing_archive.read_bytes()
    source = json.loads(source_bytes)
    scenario_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if source["protocol"]["scenario_sha256"] != scenario_hash or source["protocol"]["model"] != MODEL:
        raise ValueError("The archive must match the official scenario and Qwen model")
    protocol = {"revision": REVISION, "scenario_sha256": scenario_hash, "model": MODEL,
                "source_sha256": hashlib.sha256(source_bytes).hexdigest(), "sampling": SAMPLING,
                "coverage_seeds": list(SEEDS), "gate": GATE, "channel_instruction": CHANNEL_INSTRUCTION,
                "schedule": source["protocol"]["schedule"]}
    result = json.loads(args.output.read_text(encoding="utf-8")) if args.output.exists() else {"protocol": protocol}
    if result["protocol"] != protocol:
        raise ValueError("The existing run uses a different channel or gate protocol")
    before = compact(result) if args.verify_only else None

    def checkpoint():
        if not args.verify_only:
            save_artifact(args.output, result)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint()
    api = None if args.verify_only else QwenAPI(args.api_utils)
    coverage(bench, scenario, source, result, api, checkpoint)
    if result["coverage_score"]["passed"]:
        timing_pair(bench, scenario, result, api, checkpoint)
    elif "timing_runs" in result or "source_assignments" in result:
        raise ValueError("Timing was launched without passing the frozen gate")
    if before is not None and before != compact(result):
        raise ValueError("Stored results differ from the offline replay")


if __name__ == "__main__":
    main()
