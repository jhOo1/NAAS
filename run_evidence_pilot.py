"""Run a local, paired model replay over three pinned VibeLifeBench windows."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
from time import monotonic, perf_counter_ns, sleep
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

from probe_evidence import (
    REVISION,
    departure_booking,
    load_checks,
    replay_updates,
    score_departure,
    score_gate,
)


MODEL = "qwen3.5-27b"
SEEDS = (19, 41, 67)
ARMS = ("original", "key", "unrelated")
SAMPLING = {"temperature": 0.4, "top_p": 0.9, "max_tokens": 256,
            "enable_thinking": False, "stream": False}
SYSTEM = (
    "你是个人助理，正执行一次模拟决策。工具消息里的返回是该时点已获得的记录。"
    "只处理所列内部动作，核对记录的对象和日期，不把旧值、别的对象或推测写成当前事实。"
    "本轮没有额外读取。仅输出一个 JSON 对象，使用说明中给出的格式。"
)


@dataclass
class Case:
    task: str
    stage: int
    wake: dict
    history: str
    instruction: str
    initial: dict | None
    expected: dict
    reads: dict
    native_check: str
    native_score: object


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class QwenAPI:
    """Use the Chat Completions route defined by the supplied distill/utils.py."""

    def __init__(self, utils):
        tree = ast.parse(utils.read_text(encoding="utf-8"))
        function = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "annotate_once")
        fields = {"base_url", "api_key", "app", "quota_id", "user_id", "access_key"}
        cfg = {arg.arg: ast.literal_eval(default)
               for arg, default in zip(function.args.kwonlyargs, function.args.kw_defaults)
               if arg.arg in fields}
        self.base_url = cfg.pop("base_url").rstrip("/")
        self.api_key = cfg.pop("api_key")
        self.extra = cfg
        self.last_start = 0.0

    def call(self, request):
        body = {**request, **self.extra}
        encoded = compact(body).encode("utf-8")
        request = Request(self.base_url + "/chat/completions", data=encoded, headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + self.api_key,
        })
        transient = []
        for attempt in range(2):
            sleep(max(0.0, self.last_start + 3.05 - monotonic()))
            self.last_start = monotonic()
            try:
                with urlopen(request, timeout=45) as response:
                    data = json.load(response)
                if data.get("model", "").casefold() != MODEL:
                    raise RuntimeError("The endpoint did not report the requested model")
                return data, transient
            except HTTPError as error:
                transient.append(f"HTTP {error.code}")
                if error.code not in {429, 500, 502, 503, 504} or attempt == 1:
                    raise RuntimeError(f"Qwen gateway returned HTTP {error.code}") from None
            except (URLError, TimeoutError) as error:
                transient.append(type(error).__name__)
                if attempt == 1:
                    raise RuntimeError(f"Qwen gateway transport failed: {type(error).__name__}") from None


def timed_read(tool, args, call):
    start = perf_counter_ns()
    result = call(**args)
    return {"tool": tool, "arguments": args, "result": result,
            "service_us": round((perf_counter_ns() - start) / 1000, 1)}


def same_stage_wake(task, stage, change_id, wake_id):
    stages = yaml.safe_load((task / "event.yaml").read_text(encoding="utf-8"))["stages"]
    events = next(events for number, events in stages.items() if int(number) == stage)
    events = sorted(events, key=lambda event: (str(event["time"]), event["id"]))
    ids = [event["id"] for event in events]
    before, after = ids.index(change_id), ids.index(wake_id)
    if after <= before or any(event.get("apply") for event in events[before + 1:after + 1]):
        raise ValueError(f"Cannot use the post-mutation snapshot at {wake_id}")
    wake = events[after]
    timestamp = wake["time"].isoformat() if hasattr(wake["time"], "isoformat") else wake["time"]
    return {"id": wake_id, "time": timestamp, "body": wake["body"].strip()}


def flight_case(task, gate):
    from flight_booking_mock.backends.sqlite_backend import SQLiteBackend
    from flight_booking_mock.services.status_service import StatusService

    if gate:
        stage = 18
        change, signal = "evt_s18_mut_gate_change", "evt_s18_flight_watch"
        flight, date, other = "CA1856", "2026-04-21", "HU7290"
        check_name = "s18_gate_update"
    else:
        stage = 19
        change, signal = "S19_mutation_departure_day_delay", "S19_notification_departure_day"
        flight, date, other = "CX368", "2026-08-14", "UA858"
        check_name = "s19_departure_delay_response"
    wake = same_stage_wake(task, stage, change, signal)
    check = load_checks(task, stage)[check_name]

    backend = SQLiteBackend(":memory:")
    try:
        service = StatusService(backend)
        previous, key = replay_updates(
            task, change, "flight_booking", backend.apply_init_sql,
            lambda: timed_read("flight_booking.get_flight_status", {"flight_no": flight, "date": date},
                               service.get_flight_status),
        )
        control = timed_read("flight_booking.get_flight_status", {"flight_no": other, "date": date},
                             service.get_flight_status)
        booking = None if gate else departure_booking(backend)
    finally:
        backend.conn.close()

    status = key["result"]
    identity = {"flight_no": flight, "date": date}
    if gate:
        old_gate = previous["result"]["gate"]
        initial = {**identity, "gate": old_gate}
        history = f"返京机票为 {date} 宁波至北京 {flight}。既有内部日历记录暂存旧登机口 {old_gate}。"
        instruction = (
            '如能核实当前登机口，更新既有内部日历记录。value 格式为 '
            '{"flight_no":"航班号","date":"YYYY-MM-DD","gate":"登机口"}。'
        )
        expected = {**identity, "gate": status["gate"]}

        def native_score(state, read):
            event = {"id": "return-flight", "summary": state["flight_no"], "description": state["gate"]}
            return {"pass": score_gate(check, [event], status)}
    else:
        initial = None
        history = f"已出票行程的首段为 {date} 上海至香港 {flight}。出发复查日志目前无这段的实时状态。"
        instruction = (
            '如能核实首航段当前状态，将状态与延误分钟数写入内部复查日志，不改订机票。value 格式为 '
            '{"flight_no":"航班号","date":"YYYY-MM-DD","status":"状态","delay_min":0}。'
            'delay_min 是整数，按实际返回填写。'
        )
        expected = {**identity, "status": status["status"], "delay_min": status["delay_min"]}

        def native_score(state, read):
            note = "" if state is None else (
                f"复查 {state['flight_no']} {state['date']}：{state['status']}，延误分钟数 {state['delay_min']}。"
            )
            return score_departure(check, booking, note, status)

    return Case(task.name, stage, wake, history, instruction, initial, expected,
                {"original": None, "key": key, "unrelated": control}, check_name, native_score)


def rental_case(task):
    from calendar_mock.backends.db import SCHEMA_SQL
    from calendar_mock.services.event_service import EventService

    stage = 10
    wake = same_stage_wake(task, stage, "ev10_mut", "ev10_user")
    check_name = "s10_calendar_conflict_found"
    check = load_checks(task, stage)[check_name]
    event_id = "evt_conflict_viewing_0808"
    window = {"time_min": "2026-08-08T00:00:00+08:00", "time_max": "2026-08-10T00:00:00+08:00"}
    db = sqlite3.connect(":memory:")
    try:
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA_SQL)
        service = EventService(db)
        before, after = replay_updates(
            task, "ev10_mut", "calendar", lambda path: db.executescript(path.read_text(encoding="utf-8")),
            lambda: service.list_events(**window),
        )
        if any(event["event_id"] == event_id for event in before) or not any(
            event["event_id"] == event_id for event in after
        ):
            raise ValueError("Conflict must become readable at the native mutation")
        key = timed_read("calendar.get_event", {"event_id": event_id}, service.get_event)
        control = timed_read("calendar.get_event", {"event_id": "evt_pool_0001"}, service.get_event)
        calendar_rows = service.list_events(max_results=500)
    finally:
        db.close()

    def native_score(state, read):
        trace = [] if read is None else [{"name": read["tool"], "arguments": read["arguments"]}]
        fs = SimpleNamespace(exists=lambda path: path.endswith("/stage_10.json"),
                             read_file=lambda _: compact(trace).encode("utf-8"))
        cap = SimpleNamespace(call_tool=lambda tool, **kwargs: calendar_rows)
        env = SimpleNamespace(workspace=SimpleNamespace(fs=fs), calendar_mock=cap)
        return {"pass": check(env), "scorer_checks_observation_only": True}

    history = "8 月 8 日至 9 日是唯一能线下看房的周末。内部草案在 8 月 8 日上午预留了看房时段。"
    instruction = (
        '如能核实 HR 日历冲突，将已占用的精确时段写入内部看房草案并标为待本人确认。'
        '不替用户预约、发材料或改 HR 会议。value 格式为 '
        '{"conflict_start":"含时区的 ISO 8601","conflict_end":"含时区的 ISO 8601",'
        '"viewing":"pending_user_confirmation"}。'
    )
    event = key["result"]
    expected = {"conflict_start": event["start"]["dateTime"], "conflict_end": event["end"]["dateTime"],
                "viewing": "pending_user_confirmation"}
    initial = {"viewing": "provisional"}
    return Case(task.name, stage, wake, history, instruction, initial, expected,
                {"original": None, "key": key, "unrelated": control}, check_name, native_score)


def messages(case, read):
    prefix = (
        f"决策时点：{case.wake['time']}\n已知历史：{case.history}\n"
        f"刚收到的通知：{case.wake['body']}\n本次内部动作：{case.instruction}\n"
        '能核实则输出 {"action":"apply","value":{...},"reason":"依据"}。'
        '证据不足则输出 {"action":"defer","reason":"缺什么"}。'
    )
    if read is not None:
        evidence = {key: read[key] for key in ("tool", "arguments", "result")}
        prefix += "\n<tool_response>\n" + compact(evidence) + "\n</tool_response>"
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prefix}]


def execute(raw, case):
    """Persist one proposed value in a fresh simulated record for every trial."""
    output = raw.strip()
    if output.startswith("```json\n") and output.endswith("```"):
        output = output[len("```json\n"):-3]
    try:
        action = json.loads(output)
        if not isinstance(action, dict) or not isinstance(action.get("reason"), str):
            raise ValueError("expected an object with a textual reason")
        if action["action"] == "defer" and set(action) == {"action", "reason"}:
            return action, case.initial, None
        value = action.get("value")
        if (action["action"] != "apply" or set(action) != {"action", "value", "reason"}
                or not isinstance(value, dict) or value.keys() != case.expected.keys()
                or any(type(value[key]) is not type(expected) for key, expected in case.expected.items())):
            raise ValueError("unsupported action or invalid value schema")
        return action, value, None
    except (ValueError, KeyError) as error:
        return None, case.initial, str(error)


def model_trial(case, arm, seed, api):
    read = case.reads[arm]
    request = {"model": MODEL, "messages": messages(case, read), "seed": seed, **SAMPLING}
    start = perf_counter_ns()
    response, transient = api.call(request)
    choice = response["choices"][0]
    raw = choice["message"].get("content") or ""
    action, state, error = execute(raw, case)
    strict = action is not None and action["action"] == "apply" and state == case.expected
    return {"arm": arm, "seed": seed, "tool_calls": int(read is not None),
            "raw_output": raw, "action": action, "final_record": state, "error": error,
            "strict_action": strict, "native": case.native_score(state, read),
            "actual_model": response["model"], "request_id": response.get("id"),
            "usage": response.get("usage"), "retry_errors": transient,
            "finish_reason": choice["finish_reason"], "wall_ms": round((perf_counter_ns() - start) / 1e6, 1)}


def build_cases(upstream):
    revision = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if revision != REVISION:
        raise ValueError(f"Expected VibeLifeBench {REVISION}, got {revision}")
    sys.path[:0] = [str(upstream / "servers" / server / "src")
                    for server in ("flight_booking_mock", "calendar_mock")]
    data = upstream / "eval_set"
    return [flight_case(data / "travel/east_china_bereavement_docs_reissue", gate=True),
            flight_case(data / "travel/galapagos_no_us_transit", gate=False),
            rental_case(data / "rental/cross_city_remote_viewing_rental")]


def save_artifact(path, artifact):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(artifact, file, ensure_ascii=False, indent=2)
        file.write("\n")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    parser.add_argument("--api-utils", type=Path, help="Path to distill/utils.py with annotate_once routing defaults")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Continue a partial run using its verified case definitions")
    args = parser.parse_args()
    if args.output.exists() != args.resume or (args.resume and args.prepare_only):
        parser.error("Use a new output path, or --resume an existing experiment")
    if not args.prepare_only and args.api_utils is None:
        parser.error("--api-utils is required to run the model")
    cases = build_cases(args.upstream.resolve())
    artifact = {"upstream": REVISION, "model": MODEL, "seeds": list(SEEDS), "sampling": SAMPLING, "cases": []}
    for case in cases:
        record = {"task": case.task, "stage": case.stage, "wake": case.wake, "initial": case.initial,
                  "expected": case.expected, "common_messages": messages(case, None), "reads": case.reads,
                  "native_check": case.native_check, "trials": []}
        artifact["cases"].append(record)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.resume:
        previous = json.loads(args.output.read_text(encoding="utf-8"))
        for old, fresh in zip(previous["cases"], artifact["cases"]):
            fresh["trials"] = old["trials"]
            for arm in ("key", "unrelated"):
                fresh["reads"][arm]["service_us"] = old["reads"][arm]["service_us"]
        if previous != artifact:
            raise ValueError("The existing artifact does not match this replay design")
    else:
        save_artifact(args.output, artifact)
    if args.prepare_only:
        return

    api = QwenAPI(args.api_utils)
    for case, record in zip(cases, artifact["cases"]):
        completed = {(item["seed"], item["arm"]) for item in record["trials"]}
        for seed in SEEDS:
            arms = list(ARMS)
            random.Random(seed).shuffle(arms)
            for arm in arms:
                if (seed, arm) in completed:
                    continue
                item = model_trial(case, arm, seed, api)
                record["trials"].append(item)
                save_artifact(args.output, artifact)
                print(f"{case.task} S{case.stage} {seed} {arm:<9} strict={item['strict_action']} "
                      f"native={item['native']['pass']} error={item['error']}", flush=True)


if __name__ == "__main__":
    main()
