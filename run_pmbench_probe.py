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


def build_frames(bench, scenario, actions):
    frames = []
    updates = bench.build_updates_by_day(scenario)
    for day in scenario["days"]:
        name = day["name"]
        if not any(step["id"] in WINDOWS for step in day["steps"]):
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
            if step["id"] in WINDOWS:
                target = WINDOWS[step["id"]]
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
                frames.append(frame)
            chosen = [handles[task] for task in bench.normalize_task_ids(archived) if task in handles]
            history.append({"role": "assistant", "content": compact({"action": "choose", "choice": archived["choice"],
                                                                       "task_ids": chosen, "channel": "NONE"})})

        day_audit(bench, scenario, day, [action for action in actions if action["day"] == name], updates[name], observe)
    if {frame["step_id"] for frame in frames} != WINDOWS.keys():
        raise ValueError("Missing frozen diagnostic windows")
    return frames


def score_action(bench, scenario, actions, frame, read, text):
    try:
        result = bench.parse_json_payload(text)
        if (result.get("action") != "choose" or result.get("choice") not in ("A", "B", "C")
                or result.get("channel", "NONE") != "NONE"):
            raise ValueError("expected a native choose action")
        handles = result.get("task_ids")
        if (not isinstance(handles, list) or len(handles) > bench.MAX_ACTION_TASK_IDS
                or any(not isinstance(handle, str) or handle not in frame["menu"] for handle in handles)
                or len(set(handles)) != len(handles)):
            raise ValueError("invalid or duplicate native action handle")
    except (AttributeError, TypeError, ValueError) as error:
        return {"error": str(error), "target_action": False}
    task_ids = [frame["menu"][handle] for handle in handles]
    queries = bench.get_action_state_query_counts(frame["original_action"])
    if read is not None:
        channel = read["channel"]
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
    args = parser.parse_args()
    if args.output.exists() != args.resume:
        parser.error("Use a new output path, or --resume an existing experiment")
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
              "model": MODEL, "seeds": list(SEEDS), "sampling": SAMPLING, "frames": build_frames(bench, scenario, actions)}
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
            random.Random(seed).shuffle(arms)
            for arm in arms:
                if (seed, arm) in completed:
                    continue
                messages = frame["common_messages"].copy()
                read = frame["reads"][arm]
                if read is not None:
                    messages.extend(query_turn(bench, read["channel"], read["items"]))
                instruction = CHOOSE_INSTRUCTION
                if args.review_menu:
                    instruction += " " + MENU_REVIEW
                messages.append({"role": "user", "content": instruction})
                response = sample(api, messages, seed)
                evaluation = score_action(bench, scenario, actions, frame, read, response["raw_output"])
                frame["trials"].append({"seed": seed, "arm": arm, "response": response, "evaluation": evaluation})
                save_artifact(args.output, result)
                print(frame["step_id"], seed, arm, evaluation["target_action"], evaluation["error"], flush=True)


if __name__ == "__main__":
    main()
