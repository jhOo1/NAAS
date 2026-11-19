"""Replay native PM-Bench action menus at three archived hidden-state misses."""

import argparse
import json
from pathlib import Path
import random
import subprocess
import sys

from audit_pmbench import REVISION, day_audit
from run_evidence_pilot import MODEL, SAMPLING, SEEDS, QwenAPI, compact, sample, save_artifact


WINDOWS = {
    "d1_s5": "appointment_form_ready_d1",
    "d2_s8": "calendar_call_move_d2",
    "d3_s9": "library_hold_second_ready_d3",
}
SECONDARY_TARGETS = {"d1_s5": "send_library_email_1_d1", "d3_s9": "balance_low_95_d3"}
ARMS = ("original", "key", "unrelated")
CONTROL = "price_tracker"
CHOOSE_INSTRUCTION = "Finish this step with the native choose JSON using task handles. No further queries at this step."
MENU_REVIEW = (
    "Review the complete current action menu. For each pending action independently check the current cue, "
    "object, deadline, and any updates or conflicts using the visible history and returned state. "
    "Include all independently supported actions that are due now, including actions supported before the latest read. "
    "An available handle or a historical cue alone does not show that its action is due now. Output only the native JSON."
)


def query_message(channel):
    return {"role": "assistant", "content": compact({"action": "query_state", "choice": "NONE",
                                                    "task_ids": [], "channel": channel})}


def query_turn(bench, channel, items):
    result = bench.format_state_query_display(channel, items)
    return [query_message(channel), {"role": "user", "content": result}]


def build_frames(bench, scenario, actions, dual_evidence=False):
    frames = []
    windows = {step: WINDOWS[step] for step in SECONDARY_TARGETS} if dual_evidence else WINDOWS
    updates = bench.build_updates_by_day(scenario)
    for day in scenario["days"]:
        name = day["name"]
        if not any(step["id"] in windows for step in day["steps"]):
            continue
        next_header = "\n".join([f"=== {name} ===", *day.get("start_instructions", [])])
        history = [{"role": "system", "content": bench.build_llm_system_prompt(False, bench.list_state_channels(scenario))},
                   {"role": "user", "content": "\n".join(bench.DAILY_TASK_HEADER_LINES)},
                   {"role": "user", "content": next_header}]
        lures = bench.normalize_lure_catalog(day.get("lures", []))
        handles = None

        def observe(step, states, active, due, archived, replies, read):
            nonlocal handles
            if handles is None:
                handles, _ = bench.build_day_handle_maps(states, lures, seed_key=f"{name}:handles")
            menu, menu_ids = bench.build_step_action_menu(states, active, lures, handles, name, step["id"])
            text = "\n".join([step["text"], *step["options"], bench.format_action_menu(menu, header="Step action menu")])
            history.append({"role": "user", "content": text})
            for channel, responses in replies.items():
                for items in responses:
                    history.extend(query_turn(bench, channel, items))
            if step["id"] in windows:
                target = windows[step["id"]]
                current = states[target]["current"]
                channel = bench.required_monitor_channel(current)
                if (target not in due or target in bench.normalize_task_ids(archived)
                        or channel in replies or CONTROL in replies or scenario["time_visible_by_default"]):
                    raise ValueError(f"Unexpected native replay state at {step['id']}")
                positive, negative = read(channel), read(CONTROL)
                if not any(item["id"] == current["cue_id"] for item in positive) or not negative:
                    raise ValueError(f"Cannot obtain both planned responses at {step['id']}")
                frame = {"day": name, "step_id": step["id"], "target": target, "due": sorted(due),
                         "menu": menu_ids, "original_action": archived, "common_messages": history.copy(),
                         "reads": {"original": None, "key": {"channel": channel, "items": positive},
                                   "unrelated": {"channel": CONTROL, "items": negative}}, "trials": []}
                if dual_evidence:
                    secondary = SECONDARY_TARGETS[step["id"]]
                    secondary_state = states[secondary]["current"]
                    second_channel = bench.required_monitor_channel(secondary_state)
                    if secondary not in due or second_channel == channel or second_channel in replies:
                        raise ValueError(f"Expected a second due and unread channel at {step['id']}")
                    cue = "clock" if second_channel == "clock" else secondary_state["cue_id"]
                    second_items = read(second_channel)
                    if not any(item["id"] == cue for item in second_items):
                        raise ValueError(f"Second cue is not readable at {step['id']}")
                    second_read = {"channel": second_channel, "items": second_items}
                    frame["secondary_target"] = secondary
                    frame["reads"]["second_only"] = second_read
                    frame["reads"]["both"] = [frame["reads"]["key"], second_read]
                frames.append(frame)
            chosen = [handles[task] for task in bench.normalize_task_ids(archived) if task in handles]
            history.append({"role": "assistant", "content": compact({"action": "choose", "choice": archived["choice"],
                                                                       "task_ids": chosen, "channel": "NONE"})})

        day_audit(bench, scenario, day, [action for action in actions if action["day"] == name], updates[name], observe)
    if {frame["step_id"] for frame in frames} != windows.keys():
        raise ValueError("Missing frozen diagnostic windows")
    return frames


def added_reads(read):
    if read is None:
        return []
    return read if isinstance(read, list) else [read]


def validate_choose(bench, result, menu):
    if (not isinstance(result, dict) or result.get("action") != "choose"
            or result.get("choice") not in ("A", "B", "C")
            or result.get("channel", "NONE") != "NONE"):
        raise ValueError("expected a native choose action")
    handles = result.get("task_ids")
    if (not isinstance(handles, list) or len(handles) > bench.MAX_ACTION_TASK_IDS
            or any(not isinstance(handle, str) or handle not in menu for handle in handles)
            or len(set(handles)) != len(handles)):
        raise ValueError("invalid or duplicate native action handle")
    return [menu[handle] for handle in handles]


def score_action(bench, scenario, actions, frame, read, text):
    try:
        result = bench.parse_json_payload(text)
        task_ids = validate_choose(bench, result, frame["menu"])
    except (AttributeError, TypeError, ValueError) as error:
        return {"error": str(error), "target_action": False}
    queries = bench.get_action_state_query_counts(frame["original_action"])
    for observed in added_reads(read):
        channel = observed["channel"]
        queries[channel] = queries.get(channel, 0) + 1
    replacement = {**frame["original_action"], "choice": result["choice"], "task_ids": task_ids,
                   "state_queries": queries, "check_time": queries.get("clock", 0)}
    replay = [replacement if row["step_id"] == frame["step_id"] else row for row in actions]
    score, per_day, _ = bench.score_log(scenario, replay)
    tp, fp, fn = (score[key] for key in ("set_tp", "set_fp", "set_fn"))
    day_score = per_day[frame["day"]]["by_monitoring"]["proactive_monitoring_required"]
    return {"error": None, "target_action": frame["target"] in task_ids,
            "action": result, "task_ids": task_ids, "extra_actions": sorted(set(task_ids) - set(frame["due"])),
            "other_due_actions": sorted(set(task_ids).intersection(frame["due"]) - {frame["target"]}),
            "archived_suffix_replay": {"set_f1": 2 * tp / (2 * tp + fp + fn), "false_alarms": score["false_alarm"],
                                       "day_monitoring_required": day_score}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--api-utils", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--review-menu", action="store_true",
                        help="Add an explicit review instruction, with the same key response and one actor request")
    parser.add_argument("--dual-evidence", action="store_true",
                        help="Run the second-only and both-cue cells for two already exposed windows")
    args = parser.parse_args()
    if args.output.exists() != args.resume:
        parser.error("Use a new output path, or --resume an existing experiment")
    if args.dual_evidence and args.review_menu:
        parser.error("Select either a menu review or a dual-evidence intervention")
    upstream = args.upstream.resolve()
    revision = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if revision != REVISION:
        parser.error(f"Expected PM-Bench {REVISION}, got {revision}")
    sys.path.insert(0, str(upstream / "sim"))
    import pm_bench as bench

    scenario = bench.load_scenario(upstream / "data/synthetic_week_v9.json")
    actions = bench.read_log(args.archive)
    score, _, _ = bench.score_log(scenario, actions)
    result = {"upstream": revision, "archive": args.archive.name, "archive_score": score,
              "model": MODEL, "seeds": list(SEEDS), "sampling": SAMPLING,
              "frames": build_frames(bench, scenario, actions, args.dual_evidence)}
    if args.review_menu:
        result["actor_addendum"] = MENU_REVIEW
    if args.resume:
        existing = json.loads(args.output.read_text(encoding="utf-8"))
        for new, old in zip(result["frames"], existing["frames"]):
            new["trials"] = old["trials"]
        if result != existing:
            raise ValueError("The existing artifact uses a different replay protocol")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    save_artifact(args.output, result)
    api = QwenAPI(args.api_utils)
    for frame in result["frames"]:
        completed = {(item["seed"], item["arm"]) for item in frame["trials"]}
        for seed in SEEDS:
            arms = ["key"] if args.review_menu else list(ARMS)
            if args.dual_evidence:
                arms = ["second_only", "both"]
            random.Random(seed).shuffle(arms)
            for arm in arms:
                if (seed, arm) in completed:
                    continue
                messages = frame["common_messages"].copy()
                reads = added_reads(frame["reads"][arm]).copy()
                if args.dual_evidence:
                    # Alternate result order using the paired seeds without modifying the responses.
                    random.Random(seed).shuffle(reads)
                for read in reads:
                    messages.extend(query_turn(bench, read["channel"], read["items"]))
                instruction = CHOOSE_INSTRUCTION
                if args.review_menu:
                    instruction += " " + MENU_REVIEW
                messages.append({"role": "user", "content": instruction})
                response = sample(api, messages, seed)
                evaluation = score_action(bench, scenario, actions, frame, reads, response["raw_output"])
                trial = {"seed": seed, "arm": arm, "response": response, "evaluation": evaluation}
                if args.dual_evidence:
                    trial["read_order"] = [read["channel"] for read in reads]
                frame["trials"].append(trial)
                save_artifact(args.output, result)
                print(frame["step_id"], seed, arm, evaluation["target_action"], evaluation["error"], flush=True)


if __name__ == "__main__":
    main()
