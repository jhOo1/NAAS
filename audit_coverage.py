"""Inventory the public native timelines and scenarios for Direction B."""

import argparse
from collections import Counter
from datetime import date, datetime, time
import hashlib
import json
from pathlib import Path

import yaml


DECLARED_SIGNALS = {"user_message", "notification", "world"}


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
    args = parser.parse_args()
    print(json.dumps(inventory(args.vibelife, args.sentinel, args.windows), indent=2))


if __name__ == "__main__":
    main()
