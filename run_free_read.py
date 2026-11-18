"""Measure one freely chosen native read before the frozen evidence-pilot actor."""

import argparse
from contextlib import closing
from datetime import date, timedelta
import json
from pathlib import Path
import random
import sqlite3

from probe_evidence import REVISION, replay_updates
from run_evidence_pilot import (
    MODEL, SAMPLING, SEEDS, QwenAPI, build_cases, compact, decide, messages,
    sample, save_artifact, timed_read,
)


CHOICE_SYSTEM = (
    "你是个人助理。你有一次读取机会，须从候选中选一项获取当前决策所需的证据。"
    "请根据对象、日期和读取操作的返回范围作出判断。先选择，随后由另一步处理实际回包。"
    '仅输出 {"choice":候选编号整数,"reason":"简短理由"}。'
)

WINDOWS = {
    "east_china_bereavement_docs_reissue": ("travel", "evt_s18_mut_gate_change", ["HU7290", "CA1542"]),
    "galapagos_no_us_transit": ("travel", "S19_mutation_departure_day_delay", ["UA858", "KL896"]),
    "cross_city_remote_viewing_rental": ("rental", "ev10_mut", []),
}


def calendar_window(first, days):
    end = date.fromisoformat(first) + timedelta(days=days)
    return {"time_min": f"{first}T00:00:00+08:00", "time_max": f"{end}T00:00:00+08:00"}


def candidate(tool, arguments):
    return {"tool": tool, "arguments": arguments}


def candidates(case):
    if case.task == "cross_city_remote_viewing_rental":
        weekend = calendar_window("2026-08-08", 2)
        today = calendar_window(case.wake["time"][:10], 2)
        return [candidate("calendar.list_events", {**weekend, "max_results": 20}),
                candidate("calendar.search_events", {**weekend, "query": "材料", "max_results": 20}),
                candidate("calendar.list_events", {**today, "max_results": 20}),
                candidate("calendar.search_events", {**weekend, "query": "看房", "max_results": 20})]
    expected_flight = case.expected["flight_no"]
    flight_date = case.expected["date"]
    alternatives = WINDOWS[case.task][2]
    return [candidate("flight_booking.get_flight_status", {"flight_no": flight, "date": flight_date})
            for flight in (expected_flight, *alternatives)] + [
                candidate("calendar.list_events", {**calendar_window(flight_date, 1), "max_results": 20})
            ]


def choice_messages(case, options):
    task = (f"决策时点：{case.wake['time']}\n已知历史：{case.history}\n"
            f"刚收到的通知：{case.wake['body']}\n本次内部动作：{case.instruction}\n"
            "get_flight_status 返回所指定航班和日期的运行状态。list_events 返回与时段重叠的事件；"
            "search_events 在指定时段按子串匹配标题、描述、地点和参与者。\n可选的一次读取：\n")
    task += "\n".join(f"{i}. {compact(option)}" for i, option in enumerate(options, 1))
    return [{"role": "system", "content": CHOICE_SYSTEM}, {"role": "user", "content": task}]


def choose(raw, options):
    raw = raw.strip()
    if raw.startswith("```json\n") and raw.endswith("```"):
        raw = raw[len("```json\n"):-3]
    try:
        selection = json.loads(raw)
        if not isinstance(selection, dict) or set(selection) != {"choice", "reason"}:
            raise ValueError("expected exactly one choice and reason")
        index = selection["choice"]
        if type(index) is not int or not 1 <= index <= len(options) or not isinstance(selection["reason"], str):
            raise ValueError("choice is outside the candidate list or invalid")
    except ValueError as error:
        return None, str(error)
    return options[index - 1], None


def read_once(upstream, case, selection):
    """Initialize the chosen native service, then dispatch one allowlisted operation."""
    domain, change, _ = WINDOWS[case.task]
    task = upstream / "eval_set" / domain / case.task
    tool, args = selection["tool"], selection["arguments"]
    if tool == "flight_booking.get_flight_status":
        from flight_booking_mock.backends.sqlite_backend import SQLiteBackend
        from flight_booking_mock.services.status_service import StatusService

        backend = SQLiteBackend(":memory:")
        with closing(backend.conn):
            replay_updates(task, change, "flight_booking", backend.apply_init_sql, lambda: None)
            return timed_read(tool, args, StatusService(backend).get_flight_status)

    from calendar_mock.backends.db import SCHEMA_SQL
    from calendar_mock.services.event_service import EventService

    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA_SQL)
        replay_updates(task, change, "calendar", lambda path: db.executescript(path.read_text(encoding="utf-8")),
                       lambda: None)
        service = EventService(db)
        routes = {"calendar.list_events": service.list_events, "calendar.search_events": service.search_events}
        return timed_read(tool, args, routes[tool])


def key_arrived(case, read):
    if read is None:
        return False
    reference = case.reads["key"]["result"]
    actual = read["result"]
    if case.task == "cross_city_remote_viewing_rental":
        keys = ("event_id", "status", "start", "end", "updated_at")
        return isinstance(actual, list) and any(all(row.get(key) == reference[key] for key in keys) for row in actual)
    return (read["tool"] == "flight_booking.get_flight_status"
            and all(actual.get(key) == value for key, value in case.expected.items())
            and actual["last_updated"] == reference["last_updated"])


def start_run(cases, pilot):
    if (pilot["upstream"], pilot["model"], pilot["seeds"], pilot["sampling"]) != (REVISION, MODEL, list(SEEDS), SAMPLING):
        raise ValueError("The key-evidence comparison does not use this run's frozen protocol")
    frozen = {entry["task"]: entry for entry in pilot["cases"]}
    result = []
    for case in cases:
        reference = frozen[case.task]
        common = messages(case, None)
        key = case.reads["key"]
        if (reference["wake"] != case.wake or reference["expected"] != case.expected
                or reference["initial"] != case.initial or reference["common_messages"] != common
                or any(reference["reads"]["key"][field] != key[field] for field in ("tool", "arguments", "result"))):
            raise ValueError(f"Native snapshot drift for {case.task}")
        oracle = [trial for trial in reference["trials"] if trial["arm"] == "key"]
        if sorted(trial["seed"] for trial in oracle) != sorted(SEEDS):
            raise ValueError(f"Missing paired key trials for {case.task}")
        result.append({"task": case.task, "stage": case.stage, "wake": case.wake, "initial": case.initial,
                       "expected": case.expected, "actor_common_messages": common, "candidates": candidates(case),
                       "key_reference_read": reference["reads"]["key"], "key_reference_trials": oracle, "trials": []})
    return {"upstream": REVISION, "model": MODEL, "seeds": list(SEEDS), "sampling": SAMPLING,
            "read_limit": 1, "cases": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    parser.add_argument("--api-utils", type=Path, required=True)
    parser.add_argument("--key-reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.output.exists() != args.resume:
        parser.error("Use a new output path, or --resume an existing experiment")
    upstream = args.upstream.resolve()
    cases = build_cases(upstream)
    result = start_run(cases, json.loads(args.key_reference.read_text(encoding="utf-8")))
    if args.resume:
        old = json.loads(args.output.read_text(encoding="utf-8"))
        for previous, record in zip(old["cases"], result["cases"]):
            record["trials"] = previous["trials"]
        if old != result:
            raise ValueError("The existing run uses a different design")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    save_artifact(args.output, result)

    api = QwenAPI(args.api_utils)
    for case, record in zip(cases, result["cases"]):
        trials = {trial["seed"]: trial for trial in record["trials"]}
        for seed in SEEDS:
            trial = trials.get(seed)
            if trial is None:
                options = record["candidates"].copy()
                random.Random(seed).shuffle(options)
                prompt = choice_messages(case, options)
                choice = sample(api, prompt, seed)
                selection, error = choose(choice["raw_output"], options)
                trial = {"seed": seed, "choice_messages": prompt, "options": options, "choice": choice,
                         "selected": selection, "choice_error": error, "action": None}
                record["trials"].append(trial)
                save_artifact(args.output, result)
            if "read" not in trial:
                trial["read"] = None if trial["selected"] is None else read_once(upstream, case, trial["selected"])
                trial["key_arrived"] = key_arrived(case, trial["read"])
                save_artifact(args.output, result)
            if trial["action"] is None:
                trial["action"] = decide(case, trial["read"], seed, api)
                save_artifact(args.output, result)
                print(f"{case.task} S{case.stage} seed={seed} read={trial['selected']} "
                      f"key={trial['key_arrived']} strict={trial['action']['strict_action']}", flush=True)


if __name__ == "__main__":
    main()
