"""Reproduce native PM-Bench scores and locate actionable hidden-state query gaps."""

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys


REVISION = "e1093c470c8981daf522d4ef047a7c3a71e077d7"


def update_states(bench, states, updates):
    for update in updates:
        task = update.get("task_id")
        if task in states:
            bench.apply_task_update(states[task], update, task_states=states)


def label_query(chosen, witnessed, reachable):
    if chosen:
        return "hit_with_cue" if witnessed else "hit_without_cue_at_due_step"
    if witnessed:
        return "cue_returned_no_action"
    return "readable_cue_absent_no_action" if reachable else "no_readable_cue_at_due_step"


def day_audit(bench, scenario, day, actions, updates, observe=None):
    by_step = {action["step_id"]: action for action in actions}
    steps = day["steps"]
    if set(by_step) != {step["id"] for step in steps} or len(by_step) != len(actions):
        raise ValueError(f"Expected one archived final action per step for {day['name']}")
    states = {task["id"]: bench.init_task_state(task) for task in day["tasks"]}
    encoding = {task["id"]: bench.normalize_encoding(task["encoding"]) for task in day["tasks"]}
    active = {task for task, (kind, _) in encoding.items() if kind == "start"}
    for task in active:
        states[task]["active"] = True
    update_states(bench, states, updates.get("pre", []))

    channels = bench.normalize_state_channels(scenario)
    visibility = bench.normalize_state_visibility(scenario)
    day_start = bench.build_day_start_minutes(day)
    last_query, snapshot = {}, {}
    due_rows = []
    set_counts = Counter()

    def response(channel, index, last, cache):
        return bench.resolve_state_query_items(channel, steps, index, day["name"], day_start,
                                              channels, last, cache)

    for index, step in enumerate(steps):
        step_id = step["id"]
        update_states(bench, states, updates.get("by_step", {}).get(step_id, []))
        for task, (kind, start) in encoding.items():
            if kind == "step" and start == step_id:
                active.add(task)
                states[task]["active"] = True

        minutes = bench.time_to_minutes(step["time"])
        due = bench.compute_due_now(states, active, step, index, minutes, day_start)
        chosen = bench.normalize_task_ids(by_step[step_id])
        chosen_set = set(chosen)
        set_counts["set_tp"] += len(chosen_set & due)
        set_counts["set_fp"] += len(chosen_set - due)
        set_counts["set_fn"] += len(due - chosen_set)

        query_counts = bench.get_action_state_query_counts(by_step[step_id])
        needed = {bench.required_monitor_channel(states[task]["current"]) for task in due
                  if bench.requires_state_monitoring(states[task]["current"], visibility)}
        available = {channel: response(channel, index, last_query.copy(), snapshot.copy()) for channel in sorted(needed)}
        replies = {}
        # Delivery state is per channel; ordering across different channels cannot change a response.
        for channel, count in query_counts.items():
            replies[channel] = [response(channel, index, last_query, snapshot) for _ in range(count)]
        if observe is not None:
            def read_at_step(channel):
                return response(channel, index, last_query.copy(), snapshot.copy())

            observe(step, states, active, due, by_step[step_id], replies, read_at_step)

        for task in sorted(due):
            current = states[task]["current"]
            if not bench.requires_state_monitoring(current, visibility):
                continue
            channel = bench.required_monitor_channel(current)
            cue = "clock" if channel == "clock" else current["cue_id"]
            archived_items = [item for reply in replies.get(channel, []) for item in reply if item["id"] == cue]
            available_items = [item for item in available[channel] if item["id"] == cue]
            due_rows.append({"day": day["name"], "step_id": step_id, "time": step["time"], "task": task,
                             "channel": channel, "cue_id": cue, "visible_event": step["text"],
                             "queried": query_counts, "chosen_tasks": chosen,
                             "replayed_required_replies": replies.get(channel, []),
                             "counterfactual_response": available[channel],
                             "classification": label_query(task in chosen_set, archived_items, available_items)})

        bench.apply_runtime_completions(states, chosen, due, step, index, minutes, day_start)
    return due_rows, set_counts


def audit(bench, scenario, log_path):
    actions, metadata = bench.read_log_with_metadata(log_path)
    summary, per_day, steps = bench.score_log(scenario, actions)
    updates = bench.build_updates_by_day(scenario)
    monitoring = Counter()
    visible = Counter()
    audited = []
    for day in scenario["days"]:
        name = day["name"]
        rows, exact = day_audit(bench, scenario, day, [a for a in actions if a["day"] == name], updates[name])
        if any(per_day[name]["metrics"][key] != value for key, value in exact.items()):
            raise ValueError(f"Native Set-F1 event sets diverged in replay for {name}")
        monitoring.update(per_day[name]["by_monitoring"]["proactive_monitoring_required"])
        visible.update(per_day[name]["by_monitoring"]["no_proactive_monitoring"])
        audited.extend(rows)
    tp, fp, fn = (summary[key] for key in ("set_tp", "set_fp", "set_fn"))
    return {"log": log_path.name, "model": metadata.get("model") if metadata else None,
            "official": {"steps": steps, "state_queries": summary["state_query_calls"], "set_f1": 2 * tp / (2 * tp + fp + fn),
                         "false_alarms": summary["false_alarm"], "monitoring_required": dict(monitoring),
                         "monitoring_not_required": dict(visible)},
            "audited_due_opportunities": len(audited),
            "by_class": dict(Counter(row["classification"] for row in audited)),
            "opportunities": audited}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    parser.add_argument("logs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    upstream = args.upstream.resolve()
    revision = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if revision != REVISION:
        parser.error(f"Expected PM-Bench {REVISION}, got {revision}")
    sys.path.insert(0, str(upstream / "sim"))
    import pm_bench as bench

    scenario = bench.load_scenario(upstream / "data/synthetic_week_v9.json")
    result = {"upstream": revision, "scenario": scenario["scenario_name"],
              "source": "official archived action/query counts; query results reconstructed once per recorded call",
              "runs": [audit(bench, scenario, log) for log in args.logs]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
        file.write("\n")


if __name__ == "__main__":
    main()
