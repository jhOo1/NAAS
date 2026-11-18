"""Inventory the public native timelines and scenarios for Direction B."""

import argparse
from collections import Counter
from datetime import date, datetime, time
import hashlib
import json
from pathlib import Path

import yaml


DECLARED_SIGNALS = {"user_message", "notification", "world"}
ADJUDICATIONS = {
    "verified",
    "single_decision_or_one_outcome",
    "evidence_already_visible",
    "separate_reads",
    "setup_or_generic_reminder",
    "shared_read_without_budget_tradeoff",
    "unresolved",
}


def event_kind(event):
    return event["type"] if "type" in event else event["kind"]


def event_time(event):
    value = event["time"]
    if isinstance(value, datetime):
        return value
    iso = value.replace("Z", "+00:00")
    if len(iso) <= 8:
        # Some timelines only give time of day. Gaps are compared within a stage.
        return datetime.combine(date.min, time.fromisoformat(iso))
    return datetime.fromisoformat(iso)


def inspect_timeline(path):
    stages = yaml.safe_load(path.read_text(encoding="utf-8"))["stages"]
    counts = Counter()
    mutation_rows = []
    for stage, events in sorted(stages.items(), key=lambda item: int(item[0])):
        ordered = sorted(events, key=lambda event: (event_time(event), event["id"]))
        next_signal = None
        for event in reversed(ordered):
            kind = event_kind(event)
            counts[kind] += 1
            if kind == "mutation":
                gap = ((event_time(next_signal) - event_time(event)).total_seconds()
                       if next_signal else None)
                mutation_rows.append({
                    "task": path.parent.name,
                    "stage": int(stage),
                    "mutation": event["id"],
                    "next_declared_signal": next_signal["id"] if next_signal else None,
                    "gap_seconds": gap,
                })
            if kind in DECLARED_SIGNALS and not event.get("silent"):
                next_signal = event
    return counts, sorted(mutation_rows, key=lambda row: (row["stage"], row["mutation"]))


def digest(root, paths):
    records = [f"{path.relative_to(root)} {hashlib.sha256(path.read_bytes()).hexdigest()}"
               for path in paths]
    return hashlib.sha256("\n".join(records).encode()).hexdigest()


def validate_adjudications(vibelife, mutation_rows, path):
    candidates = {}
    for row in mutation_rows:
        if row["gap_seconds"] and row["gap_seconds"] > 0:
            key = (row["task"], row["stage"], row["next_declared_signal"])
            candidates.setdefault(key, set()).add(row["mutation"])

    counts = Counter()
    verified_tasks = set()
    verified_windows = set()
    task_paths = {path.parent.name: path.parent for path in vibelife.glob("eval_set/*/*/event.yaml")}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        record = json.loads(line)
        key = (record["task"], record["stage"], record["signal"])
        if record["index"] != number or set(record["mutations"]) != candidates.pop(key, None):
            raise ValueError(f"Adjudication {number} does not match a unique indexed source candidate")
        outcome = record["outcome"]
        expected_rubric = task_paths[record["task"]] / "rubrics" / f"stage_{record['stage']}.py"
        if (outcome not in ADJUDICATIONS or not record["reason"].strip()
                or not expected_rubric.is_file() or vibelife / record["rubric"] != expected_rubric):
            raise ValueError(f"Adjudication {number} lacks a reason or has an invalid rubric or outcome")
        counts[outcome] += 1
        if outcome in {"verified", "shared_read_without_budget_tradeoff"}:
            proof = record["proof"]
            if len(set(proof["independent_decisions"])) < 2 or not proof["read"] or not proof["score_anchor"]:
                raise ValueError(f"Adjudication {number} lacks an evidence read or two score-linked decisions")
        if outcome == "shared_read_without_budget_tradeoff" and not proof["unfiltered_read"]:
            raise ValueError(f"Adjudication {number} lacks the read that removes the budget trade-off")
        if outcome == "verified":
            if not proof["competing_read"]:
                raise ValueError(f"Adjudication {number} lacks a competing read")
            native_window = (record["task"], record["stage"], tuple(record["native_visible_batch"]))
            if native_window in verified_windows:
                raise ValueError(f"Adjudication {number} double-counts a verified native window")
            verified_windows.add(native_window)
            verified_tasks.add(record["task"])

    if candidates:
        raise ValueError(f"Missing adjudications for {len(candidates)} candidates")
    return {
        "rows": sum(counts.values()),
        "outcomes": dict(sorted(counts.items())),
        "verified_windows": len(verified_windows),
        "verified_tasks": len(verified_tasks),
        "launch_gate_met": len(verified_windows) >= 10 and len(verified_tasks) >= 5,
    }


def inventory(vibelife, sentinel, include_windows=False):
    timelines = sorted(vibelife.glob("eval_set/*/*/event.yaml"))
    scenarios = sorted(path for path in sentinel.glob("scenarios/*/*.json") if path.name != "dev.json")
    if not timelines or not scenarios:
        raise ValueError("Provide the VibeLifeBench and SentinelBench source roots")

    vibe_counts = Counter()
    windows = []
    for path in timelines:
        counts, rows = inspect_timeline(path)
        vibe_counts.update(counts)
        windows.extend(rows)

    sentinel_records = [json.loads(path.read_text(encoding="utf-8")) for path in scenarios]
    if len({record["id"] for record in sentinel_records}) != len(sentinel_records):
        raise ValueError("Duplicate SentinelBench scenario IDs")
    positive_gaps = [row for row in windows if row["gap_seconds"] and row["gap_seconds"] > 0]
    result = {
        "vibelife": {
            "native_tasks": len(timelines),
            "events_by_kind": dict(sorted(vibe_counts.items())),
            "mutations_with_later_same_stage_signal": len(positive_gaps),
            "distinct_same_stage_signals_after_mutations": len({
                (row["task"], row["stage"], row["next_declared_signal"]) for row in positive_gaps
            }),
            "source_manifest_sha256": digest(vibelife, timelines),
        },
        "sentinel": {
            "native_scenarios": len(sentinel_records),
            "environments": len({record["environment"] for record in sentinel_records}),
            "no_op_scenarios": sum(record["condition_at"] is None for record in sentinel_records),
            "source_manifest_sha256": digest(sentinel, scenarios),
        },
    }
    if include_windows:
        result["vibelife"]["mutation_rows"] = windows
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vibelife", required=True, type=Path)
    parser.add_argument("--sentinel", required=True, type=Path)
    parser.add_argument("--windows", action="store_true", help="Include mutation and signal pairs")
    parser.add_argument("--adjudications", type=Path, help="Validate the per-window audit ledger")
    args = parser.parse_args()
    result = inventory(args.vibelife, args.sentinel, args.windows or args.adjudications is not None)
    if args.adjudications:
        mutation_rows = result["vibelife"]["mutation_rows"]
        result["adjudications"] = validate_adjudications(args.vibelife, mutation_rows, args.adjudications)
        if not args.windows:
            del result["vibelife"]["mutation_rows"]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
