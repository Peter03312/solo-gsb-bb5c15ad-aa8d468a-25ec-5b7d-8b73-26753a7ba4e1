"""评估版本间差异计算。"""


def _reading_key(r):
    return (r["artifact_id"], r["ts"])


def diff_readings(old, new):
    """读数差异：新增 / 删除 / 更正（同一文物同一时间戳但数值或节点不同）。"""
    old_map = {_reading_key(r): r for r in old}
    new_map = {_reading_key(r): r for r in new}
    added = [new_map[k] for k in new_map.keys() - old_map.keys()]
    removed = [old_map[k] for k in old_map.keys() - new_map.keys()]
    corrected = []
    for k in old_map.keys() & new_map.keys():
        o, n = old_map[k], new_map[k]
        if (o["temperature"] != n["temperature"]
                or o["humidity"] != n["humidity"]
                or o["node_id"] != n["node_id"]):
            corrected.append({"before": o, "after": n})
    return {"added": added, "removed": removed, "corrected": corrected}


def _event_key(e):
    return (e["artifact_id"], e["metric"], e["start_ts"])


def diff_events(old, new):
    """事件差异：新增 / 消失 / 起止或读数个数变化 / 关闭状态未继承。"""
    old_map = {_event_key(e): e for e in old}
    new_map = {_event_key(e): e for e in new}
    added = [new_map[k] for k in new_map.keys() - old_map.keys()]
    removed = [old_map[k] for k in old_map.keys() - new_map.keys()]
    changed = []
    closure_not_inherited = []
    for k in old_map.keys() & new_map.keys():
        o, n = old_map[k], new_map[k]
        if o["end_ts"] != n["end_ts"] or o["reading_count"] != n["reading_count"]:
            changed.append({"before": o, "after": n})
        if o["status"] == "closed" and n["status"] != "closed":
            closure_not_inherited.append({"before": o, "after": n})
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "closure_not_inherited": closure_not_inherited,
    }
