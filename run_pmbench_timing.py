"""Paired closed-loop PM-Bench v9 timing comparison at nine reads per week."""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from audit_pmbench import REVISION, day_audit, update_states
from run_evidence_pilot import MODEL, SAMPLING, QwenAPI, compact, sample, save_artifact
from run_pmbench_probe import CHOOSE_INSTRUCTION, query_turn, validate_choose


ARMS = ("fixed", "on_demand")
BUDGET = 9
SEED = 19
PLAN_INSTRUCTION = (
    "You are at the current step. Output one JSON object with exactly three fields. "
    '"provisional" is the native choose JSON you would issue right now without any new state read. '
    '"channel" is the single listed state channel you would read if given one read now, even if you prefer to wait. '
    '"read_now" is true only if there is a concrete, unresolved due-condition of an active task that this channel '
    "could check now, and waiting to the next event may miss it. Use visible evidence and prior state results. "
    "A completed or canceled task or a mere topical match does not qualify. "
    'Format: {"read_now":false,"channel":"calendar",'
    '"provisional":{"action":"choose","choice":"A","task_ids":[],"channel":"NONE"}}.'
)
NO_ACTION = {"action": "choose", "choice": "A", "task_ids": [], "channel": "NONE"}


def blocks(total):
    if total < BUDGET:
        raise ValueError("At least nine steps are needed for the matched schedule")
    return [{"first": i * total // BUDGET + 1, "last": (i + 1) * total // BUDGET}
            for i in range(BUDGET)]


def read_reason(arm, index, block, spent, wants_read):
    if spent:
        return None
    if arm == "fixed":
        midpoint = (block["first"] + block["last"] + 1) // 2
        return "fixed_midpoint" if index == midpoint else None
    if wants_read:
        return "requested"
    return "block_end" if index == block["last"] else None


def plan_from_reply(bench, raw, channels):
    try:
        result = bench.parse_json_payload(raw)
        if (not isinstance(result, dict) or set(result) != {"read_now", "channel", "provisional"}
                or type(result["read_now"]) is not bool or result["channel"] not in channels):
            raise ValueError("expected a boolean read_now, a listed channel and a provisional action")
        return result, None
    except (ValueError, TypeError) as error:
        return {"read_now": False, "channel": "clock", "provisional": NO_ACTION}, str(error)


def choose_from_reply(bench, result, menu):
    try:
        ids = validate_choose(bench, result, menu)
        canonical = {"action": "choose", "choice": result["choice"],
                     "task_ids": result["task_ids"], "channel": "NONE"}
        return canonical, ids, None
    except ValueError as error:
        return NO_ACTION, [], str(error)


def get_reply(row, key, prompt, seed, api, checkpoint):
    digest = hashlib.sha256(compact(prompt).encode("utf-8")).hexdigest()
    if key not in row:
        if api is None:
            raise ValueError(f"Incomplete run: missing {key} at {row['step_id']}")
        row[key] = {"prompt_sha256": digest, **sample(api, prompt, seed)}
        checkpoint()
    elif row[key]["prompt_sha256"] != digest:
        raise ValueError(f"Resume prompt drift at {row['step_id']} {key}")
    return row[key]["raw_output"]


def native_action(row, choice, ids, read):
    channels = {} if read is None else {read["channel"]: 1}
    return {"day": row["day"], "step_id": row["step_id"], "choice": choice["choice"], "task_ids": ids,
            "state_queries": channels, "check_time": channels.get("clock", 0)}


def match_cue(bench, step, channel, cue, read):
    if read is None or channel != read["channel"]:
        return []
    if channel == "clock":
        return [item for item in read["items"] if item["id"] == "clock" and item["value"] == step["time"]]
    current = [bench.normalize_state_event_item(item) for item in step.get("state_events", {}).get(channel, [])]
    return [item for item in read["items"] if item["id"] == cue and item in current]


def evidence_at_step(bench, due, states, visibility, step, read, chosen):
    result = []
    for task in sorted(due):
        current = states[task]["current"]
        hidden = bench.requires_state_monitoring(current, visibility)
        channel = bench.required_monitor_channel(current) if hidden else None
        cue = ("clock" if channel == "clock" else current["cue_id"]) if hidden else None
        witnessed = match_cue(bench, step, channel, cue, read) if hidden else []
        cause = None
        if hidden:
            if witnessed:
                cause = "cue_in_reply"
            elif read is None:
                cause = "no_read"
            elif read["channel"] != channel:
                cause = "other_channel"
            else:
                cause = "required_channel_without_cue"
        result.append({"task": task, "required_channel": channel, "cue_id": cue,
                       "matched_return_items": witnessed, "evidence_status": cause, "acted_on_time": task in chosen})
    return result


def play_week(bench, scenario, run, schedule, api, checkpoint, *, observe=None):
    """Each yielded step completes both policy decisions and the native action."""
    channels = bench.normalize_state_channels(scenario)
    visibility = bench.normalize_state_visibility(scenario)
    if any(visibility.values()):
        raise ValueError("This comparison expects the released all-hidden v9 scenario")
    history = [{"role": "system", "content": bench.build_llm_system_prompt(False, sorted(channels))},
               {"role": "user", "content": "\n".join(bench.DAILY_TASK_HEADER_LINES)}]
    updates = bench.build_updates_by_day(scenario)
    cursor, spent = 0, False
    index = 0
    for day in scenario["days"]:
        name = day["name"]
        states = {task["id"]: bench.init_task_state(task) for task in day["tasks"]}
        encoding = {task["id"]: bench.normalize_encoding(task["encoding"]) for task in day["tasks"]}
        active = {task for task, (mode, _) in encoding.items() if mode == "start"}
        for task in active:
            states[task]["active"] = True
        update_states(bench, states, updates[name].get("pre", []))
        lures = bench.normalize_lure_catalog(day.get("lures", []))
        handles, _ = bench.build_day_handle_maps(states, lures, seed_key=f"{name}:handles")
        history.append({"role": "user", "content": "\n".join([f"=== {name} ===", *day.get("start_instructions", [])])})
        last_query, snapshot = {}, {}
        start_minutes = bench.build_day_start_minutes(day)
        for day_index, step in enumerate(day["steps"]):
            index += 1
            block = schedule[cursor]
            step_id = step["id"]
            update_states(bench, states, updates[name].get("by_step", {}).get(step_id, []))
            for task, (mode, start) in encoding.items():
                if mode == "step" and start == step_id:
                    active.add(task)
                    states[task]["active"] = True

            menu, lookup = bench.build_step_action_menu(states, active, lures, handles, name, step_id)
            visible = "\n".join([step["text"], *step["options"], bench.format_action_menu(menu, header="Step action menu")])
            history.append({"role": "user", "content": visible})
            if len(run["steps"]) < index:
                run["steps"].append({"day": name, "step_id": step_id, "index": index, "block": cursor + 1})
            row = run["steps"][index - 1]
            if (row["day"], row["step_id"], row["index"], row["block"]) != (name, step_id, index, cursor + 1):
                raise ValueError(f"Resume step drift at {step_id}")
            seed = SEED + index - 1
            plan_prompt = [*history, {"role": "user", "content": PLAN_INSTRUCTION}]
            raw = get_reply(row, "plan_reply", plan_prompt, seed, api, checkpoint)
            plan, plan_error = plan_from_reply(bench, raw, channels)
            reason = read_reason(run["arm"], index, block, spent, plan["read_now"])
            read = None
            if reason:
                channel = plan["channel"]
                items = bench.resolve_state_query_items(channel, day["steps"], day_index, name, start_minutes,
                                                        channels, last_query, snapshot)
                read = bench.build_state_query_response(channel, items, name, step_id)
                if "read" in row and row["read"] != read:
                    raise ValueError(f"Resume read drift at {step_id}")
                history.extend(query_turn(bench, channel, items))
                spent = True
                prompt = [*history, {"role": "user", "content": CHOOSE_INSTRUCTION}]
                final_raw = get_reply(row, "after_read_reply", prompt, seed, api, checkpoint)
                try:
                    proposed = bench.parse_json_payload(final_raw)
                except ValueError:
                    proposed = None
            else:
                proposed = plan["provisional"]

            choice, ids, action_error = choose_from_reply(bench, proposed, lookup)
            minutes = bench.time_to_minutes(step["time"])
            due = bench.compute_due_now(states, active, step, day_index, minutes, start_minutes)
            chosen = set(ids)
            if "native_action" in row and row["native_action"] != native_action(row, choice, ids, read):
                raise ValueError(f"Resume action drift at {step_id}")
            row.update({"clock_for_audit_only": step["time"], "plan": plan, "plan_error": plan_error,
                        "timing_reason": reason, "read": read,
                        "delivered_text": bench.format_state_query_display(read["channel"], read["items"]) if read else None,
                        "choice": choice, "action_error": action_error,
                        "native_action": native_action(row, choice, ids, read),
                        "due": evidence_at_step(bench, due, states, visibility, step, read, chosen),
                        "missed_due": sorted(due - chosen), "non_due_actions": sorted(chosen - due)})
            if observe is not None:
                def read_at_step(channel):
                    return bench.resolve_state_query_items(channel, day["steps"], day_index, name, start_minutes,
                                                            channels, last_query.copy(), snapshot.copy())

                observe(step, history, lookup, row, read_at_step)
            history.append({"role": "assistant", "content": compact(choice)})
            bench.apply_runtime_completions(states, ids, due, step, day_index, minutes, start_minutes)
            if index == block["last"]:
                if not spent:
                    raise AssertionError("Read budget not used in its assigned block")
                cursor += 1
                spent = False
            checkpoint()
            yield row


def audit_week(bench, scenario, per_day, rows):
    by_step = {row["step_id"]: row for row in rows}
    updates = bench.build_updates_by_day(scenario)
    for day in scenario["days"]:
        name = day["name"]
        actions = [by_step[step["id"]]["native_action"] for step in day["steps"]]

        def verify(step, states, active, due, action, replies, read_at_step):
            row = by_step[step["id"]]
            expected = [bench.build_state_query_response(channel, items, name, step["id"])
                        for channel, results in replies.items() for items in results]
            actual = [] if row["read"] is None else [row["read"]]
            if actual != expected or set(x["task"] for x in row["due"]) != set(due):
                raise ValueError(f"Native response or due set differs at {step['id']}")
            if actual and row["delivered_text"] != bench.format_state_query_display(actual[0]["channel"], actual[0]["items"]):
                raise ValueError(f"Model-visible response differs at {step['id']}")

        witnessed, exact = day_audit(bench, scenario, day, actions, updates[name], verify)
        for field, count in exact.items():
            if count != per_day[name]["metrics"][field]:
                raise ValueError(f"Official scorer and replay differ in {name} {field}")
        for item in witnessed:
            row = by_step[item["step_id"]]
            actual = next(x for x in row["due"] if x["task"] == item["task"])
            replayed = [match for reply in item["replayed_required_replies"] for match in reply
                        if match["id"] == item["cue_id"]]
            if actual["matched_return_items"] and not all(item in replayed for item in actual["matched_return_items"]):
                raise ValueError(f"Official query replay differs at {row['step_id']}")


def score_run(bench, scenario, run):
    actions = [row["native_action"] for row in run["steps"]]
    summary, per_day, steps = bench.score_log(scenario, actions)
    due_steps = [row for row in run["steps"] if row["due"]]
    reads = [row for row in run["steps"] if row["read"] is not None]
    audit_week(bench, scenario, per_day, run["steps"])
    counts = Counter()
    for row in due_steps:
        for item in row["due"]:
            if item["required_channel"]:
                counts["hidden_due"] += 1
                counts["hidden_due_with_cue"] += bool(item["matched_return_items"])
                counts["hidden_due_with_cue_and_hit"] += bool(item["matched_return_items"]) and item["acted_on_time"]
                counts["hidden_due_hit_without_same_step_cue"] += not item["matched_return_items"] and item["acted_on_time"]

    replies = [row[key] for row in run["steps"] for key in ("plan_reply", "after_read_reply") if key in row]
    if len(reads) != BUDGET or summary["state_query_calls"] != BUDGET:
        raise ValueError("Run did not execute the matched nine-read schedule")
    if len(replies) != steps + BUDGET or any(reply["usage"] is None for reply in replies):
        raise ValueError("Incomplete API usage for the paired design")
    tp, fp, fn = (summary[key] for key in ("set_tp", "set_fp", "set_fn"))
    return {"native": summary, "monitoring": bench.aggregate_monitoring_counts(per_day),
            "monitoring_by_channel": bench.aggregate_monitoring_channel_counts(per_day),
            "read_counts_by_channel": dict(Counter(row["read"]["channel"] for row in reads)),
            "set_f1": 2 * tp / (2 * tp + fp + fn), "steps": steps,
            "all_due_completed_steps": sum(not row["missed_due"] for row in due_steps), "due_steps": len(due_steps),
            "due_steps_exact": sum(not row["missed_due"] and not row["non_due_actions"] for row in due_steps),
            "evidence": dict(counts), "evidence_statuses": dict(Counter(
                item["evidence_status"] for row in due_steps for item in row["due"] if item["required_channel"])),
            "requests": len(replies),
            "prompt_tokens": sum(reply["usage"]["prompt_tokens"] for reply in replies),
            "completion_tokens": sum(reply["usage"]["completion_tokens"] for reply in replies),
            "transport_retries": sum(len(reply["retry_errors"]) for reply in replies),
            "plan_errors": sum(bool(row["plan_error"]) for row in run["steps"]),
            "actor_errors": sum(bool(row["action_error"]) for row in run["steps"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-utils", type=Path)
    resume = parser.add_mutually_exclusive_group()
    resume.add_argument("--resume", action="store_true")
    resume.add_argument("--verify-only", action="store_true", help="Recompute a completed run without any API requests")
    args = parser.parse_args()
    existing = args.resume or args.verify_only
    if args.output.exists() != existing:
        parser.error("Use a new output path, or --resume or --verify-only with an existing run")
    if not args.verify_only and args.api_utils is None:
        parser.error("--api-utils is required to execute model requests")
    upstream = args.upstream.resolve()
    ref = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if ref != REVISION:
        parser.error(f"Expected PM-Bench {REVISION}, got {ref}")
    sys.path.insert(0, str(upstream / "sim"))
    import pm_bench as bench

    scenario_path = upstream / "data/synthetic_week_v9.json"
    scenario = bench.load_scenario(scenario_path)
    total = sum(len(day["steps"]) for day in scenario["days"])
    schedule = blocks(total)
    protocol = {"revision": REVISION, "scenario_sha256": hashlib.sha256(scenario_path.read_bytes()).hexdigest(),
                "scenario": scenario["scenario_name"], "model": MODEL, "sampling": SAMPLING,
                "first_seed": SEED, "budget": BUDGET, "schedule": schedule,
                "plan_instruction": PLAN_INSTRUCTION, "post_read_instruction": CHOOSE_INSTRUCTION}
    result = {"protocol": protocol, "runs": [{"arm": arm, "steps": []} for arm in ARMS]}
    if existing:
        result = json.loads(args.output.read_text(encoding="utf-8"))
        if result["protocol"] != protocol or [run["arm"] for run in result["runs"]] != list(ARMS):
            raise ValueError("Cannot resume a run from a different protocol")
    before = compact(result) if args.verify_only else None
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def checkpoint():
        if not args.verify_only:
            save_artifact(args.output, result)

    api = None if args.verify_only else QwenAPI(args.api_utils)
    checkpoint()
    runners = {run["arm"]: play_week(bench, scenario, run, schedule, api, checkpoint) for run in result["runs"]}
    for offset in range(total):
        # Alternate which arm reaches the common virtual step first.
        order = ARMS if offset % 2 == 0 else ARMS[::-1]
        for arm in order:
            row = next(runners[arm])
            if row["read"] or (offset + 1) % 10 == 0:
                channel = row["read"]["channel"] if row["read"] else None
                print(arm, offset + 1, row["step_id"], row["timing_reason"], channel, flush=True)
    for run in result["runs"]:
        run["score"] = score_run(bench, scenario, run)
    if before is not None and before != compact(result):
        raise ValueError("Stored run differs from deterministic replay and scoring")
    checkpoint()
    print({run["arm"]: run["score"] for run in result["runs"]}, flush=True)


if __name__ == "__main__":
    main()
