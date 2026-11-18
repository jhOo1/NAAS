"""Replay native flight and calendar reads and inspect their score contracts."""

import argparse
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import yaml


REVISION = "e55744876a4928fa36772f64952bbe3581ed16ca"


def load_checks(task, stage):
    package = f"_naas_rubrics_{task.name}"
    spec = importlib.util.spec_from_file_location(package, task / "rubrics/__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    spec.loader.exec_module(module)
    checks = importlib.import_module(f"{package}.stage_{stage}").CHECKS
    return {name: check for name, check, _ in checks}


def replay_updates(task, stop_event, server, apply_sql, observe):
    task_seed = task / "envs" / server / task.name / "init.sql"
    apply_sql(task_seed)
    stages = yaml.safe_load((task / "event.yaml").read_text(encoding="utf-8"))["stages"]
    for stage in sorted(stages, key=int):
        for event in sorted(stages[stage], key=lambda row: (str(row["time"]), row["id"])):
            if event["id"] == stop_event:
                previous = observe()
            for entry in event.get("apply", []):
                if entry.get("server") in {server, f"{server}_mock"}:
                    apply_sql(task / entry["sql_file"])
            if event["id"] == stop_event:
                return previous, observe()
    raise ValueError(f"Mutation not found: {stop_event}")


def departure_booking(backend):
    route_numbers = ("CX368", "CX315", "IB6453", "AV1632")
    # Hold a feasible booking fixed. This fixture stands in for earlier agent writes.
    segments = []
    for number in route_numbers:
        leg = backend.get_flight_any_date(number)
        segments.append({"flight_no": number, "origin": leg["origin"], "dest": leg["dest"],
                         "date": leg["depart_dt"][:10]})
    return {"pnr": "fixture", "status": "ticketed", "segments": segments,
            "passengers": ["LIN QIAO", "XU WEN CHENG"]}


def score_departure(check, booking, note, status):
    calls = []

    def call_tool(tool, **kwargs):
        calls.append(tool)
        if tool == "get_flight_status":
            assert kwargs == {"flight_no": "CX368", "date": "2026-08-14"}
        return {"list_bookings": [{"pnr": "fixture"}], "get_booking": booking,
                "get_flight_status": status}[tool]

    def read_file(path):
        return note if path.endswith("/incident_log.md") else ""

    env = SimpleNamespace(flight_booking_mock=SimpleNamespace(call_tool=call_tool),
                          workspace=SimpleNamespace(fs=SimpleNamespace(read_file=read_file)))
    return {"pass": check(env), "scorer_calls": calls}


def probe_departure(task, backend, service):
    flight_status = lambda: service.get_flight_status("CX368", "2026-08-14")
    previous, latest = replay_updates(
        task, "S19_mutation_departure_day_delay", "flight_booking", backend.apply_init_sql, flight_status
    )
    booking = departure_booking(backend)
    name = "s19_departure_delay_response"
    check = load_checks(task, 19)[name]

    def score(note, status):
        return score_departure(check, booking, note, status)

    generic = "复查延误。"
    outcomes = {
        "current_fact": score(f"复查：首段延误 {latest['delay_min']} 分钟。", latest),
        "generic_claim_with_current_backend": score(generic, latest),
        "generic_claim_with_pre_mutation_backend": score(generic, previous),
        "empty_log_with_current_backend": score("", latest),
    }
    return {"check": name, "before": previous, "after": latest, "scores": outcomes}


def score_gate(check, events, status):
    def read_status(tool, **kwargs):
        assert tool == "get_flight_status" and kwargs == {"flight_no": "CA1856", "date": "2026-04-21"}
        return status

    def read_calendar(tool, **kwargs):
        assert tool == "list_events"
        return {"events": events}

    env = SimpleNamespace(flight_booking_mock=SimpleNamespace(call_tool=read_status),
                          calendar_mock=SimpleNamespace(call_tool=read_calendar))
    return check(env)


def probe_gate(task, backend, service):
    date, flight = "2026-04-21", "CA1856"
    flight_status = lambda: service.get_flight_status(flight, date)
    previous, latest = replay_updates(
        task, "evt_s18_mut_gate_change", "flight_booking", backend.apply_init_sql, flight_status
    )
    name = "s18_gate_update"
    check = load_checks(task, 18)[name]

    def score(events, status):
        return score_gate(check, events, status)

    old_event = {"id": "gate-event", "summary": flight, "description": previous["gate"]}
    new_event = {**old_event, "description": latest["gate"]}
    outcomes = {
        "no_calendar_write": score([], latest),
        "generic_calendar_write": score([{**old_event, "description": "已复查登机口"}], latest),
        "stale_gate_write": score([old_event], latest),
        "current_gate_write": score([new_event], latest),
        "duplicate_current_gate_write": score([new_event, {**new_event, "id": "duplicate"}], latest),
        "current_and_stale_event": score([new_event, {**old_event, "id": "duplicate"}], latest),
        "negated_current_gate": score([{**new_event, "description": f"不是 {latest['gate']}"}], latest),
        "current_gate_write_with_stale_backend": score([new_event], previous),
    }
    return {"check": name, "before": previous, "after": latest, "scores": outcomes}


def probe_rental(task):
    from calendar_mock.backends.db import SCHEMA_SQL
    from calendar_mock.services.event_service import EventService

    name = "s10_calendar_conflict_found"
    check = load_checks(task, 10)[name]
    event_id = "evt_conflict_viewing_0808"
    in_window = {"time_min": "2026-08-08T00:00:00+08:00", "time_max": "2026-08-10T00:00:00+08:00"}
    outside_window = {"time_min": "2026-08-10T00:00:00+08:00", "time_max": "2026-08-11T00:00:00+08:00"}

    db = sqlite3.connect(":memory:")
    try:
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA_SQL)
        service = EventService(db)

        def apply_sql(path):
            db.executescript(path.read_text(encoding="utf-8"))

        def observe():
            return [row for row in service.list_events(**in_window) if row["event_id"] == event_id]

        before, after = replay_updates(task, "ev10_mut", "calendar", apply_sql, observe)

        def score(arguments=None):
            trace = [] if arguments is None else [{"name": "calendar_list_events", "arguments": arguments}]
            fs = SimpleNamespace(exists=lambda path: path.endswith("/stage_10.json"),
                                 read_file=lambda _: json.dumps(trace).encode("utf-8"))
            cap = SimpleNamespace(call_tool=lambda tool, **args: getattr(service, tool)(**args))
            return check(SimpleNamespace(workspace=SimpleNamespace(fs=fs), calendar_mock=cap))

        return {"check": name, "before": before, "after": after, "scores": {
            "no_read": score(),
            "in_window_read": score(in_window),
            "out_of_window_read": score(outside_window),
            "out_of_window_result_contains_target": any(
                row["event_id"] == event_id for row in service.list_events(**outside_window)
            ),
        }}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    args = parser.parse_args()
    upstream = args.upstream.resolve()
    revision = subprocess.check_output(["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True).strip()
    if revision != REVISION:
        parser.error(f"Expected VibeLifeBench {REVISION}, got {revision}")

    sys.path[:0] = [str(upstream / "servers" / server / "src")
                    for server in ("flight_booking_mock", "calendar_mock")]
    from flight_booking_mock.backends.sqlite_backend import SQLiteBackend
    from flight_booking_mock.services.status_service import StatusService

    output = {"upstream": revision}
    for task_name, probe in (("galapagos_no_us_transit", probe_departure),
                             ("east_china_bereavement_docs_reissue", probe_gate)):
        backend = SQLiteBackend(":memory:")
        try:
            output[task_name] = probe(upstream / "eval_set/travel" / task_name,
                                      backend, StatusService(backend))
        finally:
            backend.conn.close()
    rental = "cross_city_remote_viewing_rental"
    output[rental] = probe_rental(upstream / "eval_set/rental" / rental)
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
