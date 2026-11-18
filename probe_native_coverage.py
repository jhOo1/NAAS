"""Replay two disputed source/score contracts without running the agent."""

import argparse
import ast
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import yaml


def probe_onsite(upstream):
    source = upstream / "servers/notification_hub_mock/src"
    sys.path.insert(0, str(source))
    from notification_hub_mock.backends.db import SCHEMA_SQL
    from notification_hub_mock.services.notification_service import NotificationService

    task = upstream / "eval_set/team_building/factory_visit_safety_day"
    initial = task / "envs/notification_hub/factory_visit_safety_day/init.sql"
    user_id = "user_seed_tb_013"
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.executescript(initial.read_text(encoding="utf-8"))
    service = NotificationService(db)
    targets = {"ntf_013_onsite_health", "ntf_013_onsite_lang"}
    stages = yaml.safe_load((task / "event.yaml").read_text(encoding="utf-8"))["stages"]

    def ids(source_filter=None):
        notifications = service.list_notifications(user_id, source=source_filter)
        return {row["notification_id"] for row in notifications}

    for stage, events in sorted(stages.items(), key=lambda item: int(item[0])):
        for event in sorted(events, key=lambda item: (str(item["time"]), item["id"])):
            if event["id"] == "S20_event_start":
                break
            for entry in event.get("apply", []):
                if entry.get("server") != "notification_hub":
                    continue
                if event["id"] == "S20_onsite_mutation":
                    assert not (targets & ids())
                db.executescript((task / entry["sql_file"]).read_text(encoding="utf-8"))
        if int(stage) == 20:
            break
    shared = ids("onsite_desk")
    competing = ids("onsite_checkin")
    assert targets <= shared and not (targets & competing)
    attendance = "ntf_013_attendance_42"
    assert attendance in competing
    target_ids = targets | {attendance}
    broad = ids()
    assert target_ids <= broad
    return {
        "shared_read": sorted(targets & shared),
        "other_read": attendance,
        "unfiltered_read": sorted(target_ids & broad),
    }


def probe_handover_rubric(upstream):
    task = upstream / "eval_set/renovation/office_fitout_15d/rubrics"
    stage_tree = ast.parse((task / "stage_14.py").read_text(encoding="utf-8"))
    helper_tree = ast.parse((task / "_helpers.py").read_text(encoding="utf-8"))

    def extract(tree, name):
        return next(node for node in tree.body if getattr(node, "name", None) == name)

    async def empty_blob(_):
        return ""

    async def no_mail(_):
        return []

    # Exercise the actual predicate with a response that never addresses occupancy.
    text = "暂缓尾款，punch list 尚未关闭。"
    module = ast.Module(body=[extract(helper_tree, "has_any"),
                              extract(stage_tree, "chk_conditional_go_holds_final_payment")], type_ignores=[])
    namespace = {"stage_response": lambda ctx, stage: text,
                 "workspace_blob": empty_blob, "owner_facing_emails": no_mail}
    exec(compile(module, "upstream_stage_14", "exec"), namespace)
    predicate = namespace["chk_conditional_go_holds_final_payment"]
    passed = asyncio.run(predicate(SimpleNamespace()))
    assert passed
    return {"response": text, "joint_rubric_passes_without_occupancy_decision": passed}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    args = parser.parse_args()
    print(json.dumps({"factory_visit_s20": probe_onsite(args.upstream),
                      "office_fitout_d14": probe_handover_rubric(args.upstream)}, ensure_ascii=False, indent=2))
