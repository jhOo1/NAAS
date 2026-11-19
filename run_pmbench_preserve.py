"""Paired action-preservation replay on fresh weeks from the native v9 builder."""

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys

from audit_pmbench import REVISION, day_audit
from run_evidence_pilot import MODEL, SAMPLING, QwenAPI, compact, save_artifact
from run_pmbench_nonclock import score_trial
from run_pmbench_probe import CHOOSE_INSTRUCTION, query_turn
from run_pmbench_reanswer import digest
from run_pmbench_timing import NO_ACTION, get_reply, match_cue


WEEK_SEEDS = (47, 83)
MODEL_SEED = 41
CONTROLS_PER_WEEK = 2
ARMS = ("original", "preserve")
PRE_READ = (
    " For each selected prospective action, add a support field mapping its handle to a short exact "
    "quotation from the current event narrative that establishes its current trigger. If the narrative "
    "cannot support that handle, omit it from support. Do not quote from the menu or choice options."
)
PRESERVE = (
    " Listed candidates were selected before the tool read and have exact quotations in the current event. "
    "Keep each candidate unless the actual tool reply explicitly invalidates its trigger, object or latest "
    "instruction. An unrelated new trigger is compatible with retaining an independently supported action. "
    "In the native choose JSON, include an optional invalidate object from candidate handle to a short exact "
    "quotation in the latest tool reply that invalidates it. Do not use hidden information or infer revocation "
    "from missing data. Continue to act on independently supported new triggers. This is the last model call."
)
GATE = {"min_restored": 2, "min_restored_windows": 2, "max_target_losses": 0, "max_other_losses": 0,
        "max_pairs_with_more_false_alarms": 0, "max_non_due_increase": 0}


def window_frames(bench, scenario, old_events):
    frames, contexts = [], {}
    channels = sorted(bench.list_state_channels(scenario))
    visibility = bench.normalize_state_visibility(scenario)
    updates = bench.build_updates_by_day(scenario)
    history = [{"role": "system", "content": bench.build_llm_system_prompt(False, channels)},
               {"role": "user", "content": "\n".join(bench.DAILY_TASK_HEADER_LINES)}]
    for day in scenario["days"]:
        name = day["name"]
        history.append({"role": "user", "content": "\n".join([f"=== {name} ===", *day["start_instructions"]])})
        lures = bench.normalize_lure_catalog(day.get("lures", []))
        actions = [{"day": name, "step_id": step["id"], "choice": "A", "task_ids": [], "state_queries": {}}
                   for step in day["steps"]]
        positions = {step["id"]: i for i, step in enumerate(day["steps"])}

        def observe(step, states, active, due, archived, replies, read_at_step):
            step_id = step["id"]
            handles, _ = bench.build_day_handle_maps(states, lures, seed_key=f"{name}:handles")
            menu, lookup = bench.build_step_action_menu(states, active, lures, handles, name, step_id)
            visible = "\n".join([step["text"], *step["options"],
                                 bench.format_action_menu(menu, header="Step action menu")])
            history.append({"role": "user", "content": visible})
            available, narrative_due = {}, []
            for task in sorted(due):
                current = states[task]["current"]
                channel = bench.required_monitor_channel(current)
                if not bench.requires_state_monitoring(current, visibility):
                    narrative_due.append(task)
                elif channel != "clock":
                    items = read_at_step(channel)
                    if match_cue(bench, step, channel, current["cue_id"], {"channel": channel, "items": items}):
                        available.setdefault(channel, []).append(task)
            if available and step["text"] not in old_events:
                channel = min(available)
                targets = available[channel]
                stratum = "concurrent" if narrative_due else "target_only" if set(targets) == set(due) else None
                if stratum is not None:
                    items = read_at_step(channel)
                    read = {"response": bench.build_state_query_response(channel, items, name, step_id),
                            "delivered_text": bench.format_state_query_display(channel, items)}
                    facts = {"step_id": step_id, "day": name, "stratum": stratum, "due": sorted(due),
                             "targets": targets, "narrative_due": narrative_due, "archive_action": archived,
                             "read": read, "trigger_event": step["text"]}
                    index = positions[step_id]
                    prefix = ({**day, "steps": day["steps"][:index + 1]}, actions[:index])
                    contexts[step_id] = (history.copy(), lookup, (prefix, updates[name], visibility))
                    frames.append(facts)
            history.append({"role": "assistant", "content": compact(NO_ACTION)})

        day_audit(bench, scenario, day, actions, updates[name], observe)
    control_ids = {row["step_id"] for row in frames if row["stratum"] == "target_only"}
    first_controls = [row["step_id"] for row in frames if row["step_id"] in control_ids][:CONTROLS_PER_WEEK]
    return [row for row in frames if row["stratum"] == "concurrent" or row["step_id"] in first_controls], contexts


def native_payload(bench, raw):
    try:
        payload = bench.parse_json_payload(raw)
        return payload if isinstance(payload, dict) else {}
    except ValueError:
        return {}


def exact_quotes(values, permitted, text):
    if not isinstance(values, dict):
        return {}
    return {handle: quote for handle, quote in values.items()
            if handle in permitted and isinstance(quote, str) and len(quote.strip()) >= 8 and quote in text}


def paired_trial(bench, frame, context, order, api, checkpoint):
    history, menu, args = context
    read = frame["read"]
    response = read["response"]
    turn = query_turn(bench, response["channel"], response["items"])
    if turn[-1]["content"] != read["delivered_text"]:
        raise ValueError(f"Native reply drift at {frame['step_id']}")
    trial = frame.setdefault("trial", {"step_id": frame["step_id"]})
    pre_prompt = [*history, {"role": "user", "content": CHOOSE_INSTRUCTION + PRE_READ}]
    pre_raw = get_reply(trial, "pre_reply", pre_prompt, MODEL_SEED, api, checkpoint)
    pre = score_trial(bench, frame, menu, pre_raw, None, *args)
    support = exact_quotes(native_payload(bench, pre_raw).get("support"), pre["choice"]["task_ids"], frame["trigger_event"])
    inputs = trial.setdefault("arms", {})
    for arm in order:
        instruction = CHOOSE_INSTRUCTION
        if arm == "preserve":
            instruction += PRESERVE + "\nCandidates: " + compact(support)
        prompt = [*history, *turn, {"role": "user", "content": instruction}]
        row = inputs.setdefault(arm, {"step_id": frame["step_id"]})
        raw = get_reply(row, "reply", prompt, MODEL_SEED, api, checkpoint)
        proposed = score_trial(bench, frame, menu, raw, read, *args)
        merged = proposed
        invalidated = {}
        if arm == "preserve":
            invalidated = exact_quotes(native_payload(bench, raw).get("invalidate"), support, read["delivered_text"])
            choice = proposed["choice"]
            retained = [handle for handle in support if handle not in invalidated]
            handles = list(dict.fromkeys(retained + choice["task_ids"]))[:bench.MAX_ACTION_TASK_IDS]
            merged = score_trial(bench, frame, menu, compact({**choice, "task_ids": handles}), read, *args)
        computed = {"proposed": proposed, "invalidated": invalidated, "outcome": merged}
        if any(field in row and row[field] != value for field, value in computed.items()):
            raise ValueError(f"Saved paired outcome differs at {frame['step_id']} {arm}")
        row.update(computed)
        checkpoint()
    replies = [trial["pre_reply"], *(row["reply"] for row in inputs.values())]
    if any(reply["actual_model"].casefold() != MODEL or reply["usage"] is None for reply in replies):
        raise ValueError("Missing model or usage evidence")
    computed = {"pre": pre, "support": support, "protected_task_ids": [menu[handle] for handle in support]}
    if any(field in trial and trial[field] != value for field, value in computed.items()):
        raise ValueError(f"Saved pre-read decision differs at {frame['step_id']}")
    trial.update(computed)


def summarize(frames):
    totals = {arm: Counter() for arm in ARMS}
    paired = Counter()
    restored_windows = []
    for key, frame in frames.items():
        targets = set(frame["targets"])
        trial = frame["trial"]
        old, new = (trial["arms"][arm]["outcome"] for arm in ARMS)
        original, updated = (set(row["completed_due"]) for row in (old, new))
        restored = (updated & set(trial["pre"]["completed_due"])) - original - targets
        restored_protected = restored & set(trial["protected_task_ids"])
        alarm_delta = new["current_step_metrics"]["false_alarm"] - old["current_step_metrics"]["false_alarm"]
        paired.update({"restored_from_pre_read": len(restored), "restored_protected": len(restored_protected),
                       "target_gains": len((updated - original) & targets),
                       "target_losses": len((original - updated) & targets), "other_losses": len(original - updated - targets),
                       "pairs_more_false_alarms": int(alarm_delta > 0)})
        if restored:
            restored_windows.append({"window": key, "tasks": sorted(restored), "protected": sorted(restored_protected)})
        for arm in ARMS:
            row = trial["arms"][arm]
            outcome = row["outcome"]
            hits = set(outcome["completed_due"])
            usage = row["reply"]["usage"]
            totals[arm].update({"requests": 1, "target_hits": len(hits & targets), "other_hits": len(hits - targets),
                                "false_alarms": outcome["current_step_metrics"]["false_alarm"],
                                "non_due": len(outcome["non_due_actions"]),
                                "prompt_tokens": usage["prompt_tokens"], "completion_tokens": usage["completion_tokens"],
                                "executor_errors": int(bool(row["proposed"]["executor_error"]))})
    checks = {"restored": paired["restored_from_pre_read"] >= GATE["min_restored"],
              "restored_windows": len(restored_windows) >= GATE["min_restored_windows"],
              "targets_preserved": paired["target_losses"] <= GATE["max_target_losses"],
              "other_preserved": paired["other_losses"] <= GATE["max_other_losses"],
              "false_alarms": paired["pairs_more_false_alarms"] <= GATE["max_pairs_with_more_false_alarms"],
              "non_due": totals["preserve"]["non_due"] - totals["original"]["non_due"] <= GATE["max_non_due_increase"]}
    pre_replies = [frame["trial"]["pre_reply"] for frame in frames.values()]
    return {"windows": len(frames), "strata": dict(Counter(frame["stratum"] for frame in frames.values())),
            "target_opportunities": sum(len(frame["targets"]) for frame in frames.values()),
            "other_opportunities": sum(len(frame["due"]) - len(frame["targets"]) for frame in frames.values()),
            "arms": totals, "paired": paired, "restored_windows": restored_windows, "checks": checks,
            "passed": all(checks.values()),
            "shared_pre_read_requests": len(pre_replies),
            "shared_pre_read_prompt_tokens": sum(reply["usage"]["prompt_tokens"] for reply in pre_replies),
            "shared_pre_read_completion_tokens": sum(reply["usage"]["completion_tokens"] for reply in pre_replies)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-utils", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if args.output.exists() != (args.resume or args.verify_only):
        parser.error("Use --prepare with a new path, or --resume/--verify-only on an existing ledger")
    if not (args.prepare or args.verify_only) and args.api_utils is None:
        parser.error("--api-utils is required for model calls")
    upstream = args.upstream.resolve()
    rev = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if rev != REVISION:
        raise ValueError(f"Expected PM-Bench {REVISION}, got {rev}")
    sys.path.insert(0, str(upstream / "sim"))
    import pm_bench as bench
    from week_builder_v9 import generate_week_v9

    old = bench.load_scenario(upstream / "data/synthetic_week_v9.json")
    old_events = {step["text"] for day in old["days"] for step in day["steps"]}
    weeks, frames, contexts = {}, {}, {}
    for seed in WEEK_SEEDS:
        scenario, _ = generate_week_v9(seed=seed, scenario_name=f"naas_native_v9_{seed}")
        weeks[str(seed)] = digest(compact(scenario).encode("utf-8"))
        selected, current_contexts = window_frames(bench, scenario, old_events)
        for frame in selected:
            key = f"{seed}:{frame['step_id']}"
            frames[key] = frame
            contexts[key] = current_contexts[frame["step_id"]]
    protocol = {"revision": rev, "week_seeds": list(WEEK_SEEDS), "weeks": weeks, "model": MODEL,
                "model_seed": MODEL_SEED, "sampling": SAMPLING, "pre_read": PRE_READ, "preserve": PRESERVE,
                "baseline": CHOOSE_INSTRUCTION, "gate": GATE}
    result = {"protocol": protocol, "windows": frames}
    initial = None
    if args.output.exists():
        saved = json.loads(args.output.read_bytes())
        for key, row in frames.items():
            if "trial" in saved["windows"][key]:
                row["trial"] = saved["windows"][key]["trial"]
        if "summary" in saved:
            result["summary"] = saved["summary"]
        if result != saved:
            raise ValueError("Frozen windows or protocol differ")
        initial = compact(saved)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint():
        if not args.verify_only:
            save_artifact(args.output, result)

    if args.prepare:
        checkpoint()
        print({key: row["stratum"] for key, row in frames.items()}, flush=True)
        return
    api = None if args.verify_only else QwenAPI(args.api_utils)
    for i, (key, frame) in enumerate(frames.items()):
        order = ARMS if i % 2 == 0 else ARMS[::-1]
        paired_trial(bench, frame, contexts[key], order, api, checkpoint)
        checkpoint()
        if api is not None:
            print(key, [(arm, frame["trial"]["arms"][arm]["outcome"]["completed_due"]) for arm in ARMS], flush=True)
    result["summary"] = summarize(frames)
    if args.verify_only and initial != compact(result):
        raise ValueError("Saved paired run differs from offline replay")
    checkpoint()
    print(compact(result["summary"]), flush=True)


if __name__ == "__main__":
    main()
