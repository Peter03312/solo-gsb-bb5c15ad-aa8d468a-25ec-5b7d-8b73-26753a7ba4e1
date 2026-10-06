"""业务逻辑层：批次/节点/文物管理、读数提交与版本化评估、事件关闭、版本差异。"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

from .diff import diff_events, diff_readings
from .evaluate import METRIC_LABELS, evaluate_artifact
from .riskgroups import find_risk_groups

RISK_GROUP_RULE = (
    "同一指标的两个风险事件，起止时间闭区间相交且涉及至少一个相同运输节点时连边；"
    "取连通分量，仅覆盖至少两件文物的分量成组，经传递相连的成员同样纳入；"
    "全组共有节点为全体成员涉及节点的交集，可能为空（传递相连不要求每对成员都有共同节点）。"
)


class ValidationError(Exception):
    """输入校验失败，details 为逐条原因列表。"""

    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = details or []


class NotFoundError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------- 工具

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value):
    """解析时间戳：ISO 8601 字符串（可带时区/Z）或 Unix 秒。返回 UTC datetime。"""
    if isinstance(value, bool):
        raise ValueError("时间戳格式无效")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    s = str(value).strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)  # 可能抛 ValueError
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def to_float(value):
    """转换为浮点数；非数值（含布尔、空串、无法解析的字符串）抛 ValueError。"""
    if isinstance(value, bool) or value is None:
        raise ValueError("非数值")
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        raise ValueError("非数值")
    return float(s)  # 可能抛 ValueError


def to_int(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("非整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return int(str(value).strip())  # 可能抛 ValueError


# ---------------------------------------------------------------- 查询

def get_batch(conn, batch_id):
    row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not row:
        raise NotFoundError(f"批次 {batch_id} 不存在")
    return row


def current_version(conn, batch_id):
    return conn.execute(
        "SELECT * FROM versions WHERE batch_id=? ORDER BY version_no DESC LIMIT 1",
        (batch_id,),
    ).fetchone()


def get_version(conn, batch_id, version_no):
    row = conn.execute(
        "SELECT * FROM versions WHERE batch_id=? AND version_no=?",
        (batch_id, version_no),
    ).fetchone()
    if not row:
        raise NotFoundError(f"批次 {batch_id} 的版本 {version_no} 不存在")
    return row


def list_nodes(conn, batch_id):
    return conn.execute(
        "SELECT * FROM nodes WHERE batch_id=? ORDER BY seq", (batch_id,)
    ).fetchall()


def list_artifacts(conn, batch_id):
    return conn.execute(
        "SELECT * FROM artifacts WHERE batch_id=? ORDER BY id", (batch_id,)
    ).fetchall()


def list_versions(conn, batch_id):
    return conn.execute(
        "SELECT * FROM versions WHERE batch_id=? ORDER BY version_no DESC",
        (batch_id,),
    ).fetchall()


def _event_dict(row, artifact_names):
    d = dict(row)
    d["nodes"] = json.loads(d["nodes"])
    d["metric_label"] = METRIC_LABELS.get(d["metric"], d["metric"])
    d["artifact_name"] = artifact_names.get(d["artifact_id"], str(d["artifact_id"]))
    return d


def _gap_dict(row, artifact_names):
    d = dict(row)
    d["artifact_name"] = artifact_names.get(d["artifact_id"], str(d["artifact_id"]))
    return d


def _reading_dict(row):
    return dict(row)


def version_events(conn, version_id, artifact_names):
    rows = conn.execute(
        "SELECT * FROM events WHERE version_id=? ORDER BY start_ts, artifact_id",
        (version_id,),
    ).fetchall()
    return [_event_dict(r, artifact_names) for r in rows]


def version_gaps(conn, version_id, artifact_names):
    rows = conn.execute(
        "SELECT * FROM gaps WHERE version_id=? ORDER BY start_ts", (version_id,)
    ).fetchall()
    return [_gap_dict(r, artifact_names) for r in rows]


def version_readings(conn, version_id):
    rows = conn.execute(
        """SELECT r.*, a.name AS artifact_name, n.name AS node_name, n.seq AS node_seq
           FROM readings r
           JOIN artifacts a ON a.id = r.artifact_id
           JOIN nodes n ON n.id = r.node_id
           WHERE r.version_id=? ORDER BY r.artifact_id, r.ts""",
        (version_id,),
    ).fetchall()
    return [_reading_dict(r) for r in rows]


def _artifact_names(conn, batch_id):
    return {a["id"]: a["name"] for a in list_artifacts(conn, batch_id)}


def batch_summary(conn, batch_id):
    """批次风险摘要（基于当前评估版本）。"""
    batch = get_batch(conn, batch_id)
    names = _artifact_names(conn, batch_id)
    v = current_version(conn, batch_id)
    summary = {
        "batch_id": batch["id"],
        "name": batch["name"],
        "current_version": None,
        "versions": 0,
        "readings": 0,
        "open_events": 0,
        "closed_events": 0,
        "open_by_metric": {"temperature": 0, "humidity": 0},
        "gaps": 0,
        "artifacts_with_open_events": [],
    }
    summary["versions"] = conn.execute(
        "SELECT COUNT(*) c FROM versions WHERE batch_id=?", (batch_id,)
    ).fetchone()["c"]
    if not v:
        return summary
    events = version_events(conn, v["id"], names)
    open_events = [e for e in events if e["status"] == "open"]
    closed_events = [e for e in events if e["status"] == "closed"]
    summary.update({
        "current_version": v["version_no"],
        "readings": conn.execute(
            "SELECT COUNT(*) c FROM readings WHERE version_id=?", (v["id"],)
        ).fetchone()["c"],
        "open_events": len(open_events),
        "closed_events": len(closed_events),
        "open_by_metric": {
            "temperature": sum(1 for e in open_events if e["metric"] == "temperature"),
            "humidity": sum(1 for e in open_events if e["metric"] == "humidity"),
        },
        "gaps": conn.execute(
            "SELECT COUNT(*) c FROM gaps WHERE version_id=?", (v["id"],)
        ).fetchone()["c"],
        "artifacts_with_open_events": sorted({e["artifact_name"] for e in open_events}),
    })
    return summary


def list_batches(conn):
    rows = conn.execute("SELECT * FROM batches ORDER BY id").fetchall()
    return [batch_summary(conn, r["id"]) for r in rows]


def version_detail(conn, batch_id, version_no):
    v = get_version(conn, batch_id, version_no)
    names = _artifact_names(conn, batch_id)
    events = version_events(conn, v["id"], names)
    return {
        "version": dict(v),
        "readings": version_readings(conn, v["id"]),
        "events": events,
        "gaps": version_gaps(conn, v["id"], names),
        "risk_groups": find_risk_groups(events),
    }


def version_risk_groups(conn, batch_id, version_no=None):
    """跨文物共因风险组。version_no 为空时取当前版本；无任何版本时返回空列表。

    分组按版本从事件快照即时计算：补录/更正读数生成新版本后自动得到新版分组，
    旧版分组始终可查；关闭事件只改变事件状态与组内开放/已关闭计数，不改变成员。
    """
    get_batch(conn, batch_id)
    if version_no is None:
        v = current_version(conn, batch_id)
        if not v:
            return {
                "batch_id": batch_id,
                "version": None,
                "rule": RISK_GROUP_RULE,
                "groups": [],
            }
    else:
        v = get_version(conn, batch_id, version_no)
    names = _artifact_names(conn, batch_id)
    events = version_events(conn, v["id"], names)
    return {
        "batch_id": batch_id,
        "version": v["version_no"],
        "rule": RISK_GROUP_RULE,
        "groups": find_risk_groups(events),
    }


def version_diff(conn, batch_id, from_no, to_no):
    get_batch(conn, batch_id)
    names = _artifact_names(conn, batch_id)
    v_from = get_version(conn, batch_id, from_no)
    v_to = get_version(conn, batch_id, to_no)
    return {
        "from_version": from_no,
        "to_version": to_no,
        "readings": diff_readings(
            version_readings(conn, v_from["id"]), version_readings(conn, v_to["id"])
        ),
        "events": diff_events(
            version_events(conn, v_from["id"], names),
            version_events(conn, v_to["id"], names),
        ),
    }


# ---------------------------------------------------------------- 写入

def create_batch(conn, name, description=""):
    name = (name or "").strip()
    if not name:
        raise ValidationError("批次名称不能为空")
    with conn:
        cur = conn.execute(
            "INSERT INTO batches(name, description, created_at) VALUES (?,?,?)",
            (name, (description or "").strip(), now_iso()),
        )
    return dict(conn.execute("SELECT * FROM batches WHERE id=?", (cur.lastrowid,)).fetchone())


def add_node(conn, batch_id, name, seq, location=""):
    get_batch(conn, batch_id)
    name = (name or "").strip()
    if not name:
        raise ValidationError("节点名称不能为空")
    try:
        seq = to_int(seq)
    except (ValueError, TypeError):
        raise ValidationError(f"节点序号必须为整数，收到: {seq!r}")
    if seq < 0:
        raise ValidationError("节点序号不能为负数")
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO nodes(batch_id, seq, name, location) VALUES (?,?,?,?)",
                (batch_id, seq, name, (location or "").strip()),
            )
    except Exception as e:
        if "UNIQUE" in str(e):
            raise ValidationError(f"节点序号 {seq} 在该批次中已存在")
        raise
    return dict(conn.execute("SELECT * FROM nodes WHERE id=?", (cur.lastrowid,)).fetchone())


def add_artifact(conn, batch_id, name, temp_min, temp_max, hum_min, hum_max,
                 sample_interval):
    get_batch(conn, batch_id)
    name = (name or "").strip()
    if not name:
        raise ValidationError("文物名称不能为空")
    errors = []
    values = {}
    for label, key, raw in (
        ("温度下限", "temp_min", temp_min), ("温度上限", "temp_max", temp_max),
        ("湿度下限", "hum_min", hum_min), ("湿度上限", "hum_max", hum_max),
    ):
        try:
            values[key] = to_float(raw)
        except (ValueError, TypeError):
            errors.append(f"{label}必须为非空的数值，收到: {raw!r}")
    try:
        values["sample_interval"] = to_int(sample_interval)
        if values["sample_interval"] <= 0:
            errors.append("采样间隔必须为正整数（秒）")
    except (ValueError, TypeError):
        errors.append(f"采样间隔必须为正整数（秒），收到: {sample_interval!r}")
    if not errors:
        if values["temp_min"] > values["temp_max"]:
            errors.append(
                f"温度上下限无序：下限 {values['temp_min']:g} 大于上限 {values['temp_max']:g}")
        if values["hum_min"] > values["hum_max"]:
            errors.append(
                f"湿度上下限无序：下限 {values['hum_min']:g} 大于上限 {values['hum_max']:g}")
    if errors:
        raise ValidationError("文物参数校验失败", errors)
    with conn:
        cur = conn.execute(
            """INSERT INTO artifacts(batch_id, name, temp_min, temp_max, hum_min, hum_max,
               sample_interval) VALUES (?,?,?,?,?,?,?)""",
            (batch_id, name, values["temp_min"], values["temp_max"],
             values["hum_min"], values["hum_max"], values["sample_interval"]),
        )
    return dict(conn.execute("SELECT * FROM artifacts WHERE id=?", (cur.lastrowid,)).fetchone())


def submit_readings(conn, batch_id, items, note=""):
    """提交读数（补录/更正/追加），生成新的评估版本并完成评估。

    校验规则：
    - 文物、节点必须属于该批次；
    - 时间戳必须可解析；温度/湿度必须为数值；
    - 同一提交内（文物, 时间戳）不得重复；
    - 与当前版本已有（文物, 时间戳）完全相同的读数视为重复时间戳，拒绝；
      数值不同的视为更正，以新值覆盖（旧版本快照保持不变）。
    """
    get_batch(conn, batch_id)
    if not isinstance(items, list) or not items:
        raise ValidationError("读数列表不能为空")
    artifacts = {a["id"]: a for a in list_artifacts(conn, batch_id)}
    nodes = {n["id"]: n for n in list_nodes(conn, batch_id)}

    errors = []
    parsed = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append({"index": i, "reason": "读数条目必须是对象"})
            continue
        entry = {}
        try:
            entry["artifact_id"] = to_int(item.get("artifact_id"))
            if entry["artifact_id"] not in artifacts:
                errors.append({"index": i, "reason": f"文物 {entry['artifact_id']} 不属于批次 {batch_id}"})
        except (ValueError, TypeError):
            errors.append({"index": i, "reason": f"artifact_id 无效: {item.get('artifact_id')!r}"})
        try:
            entry["node_id"] = to_int(item.get("node_id"))
            if entry["node_id"] not in nodes:
                errors.append({"index": i, "reason": f"节点 {entry['node_id']} 不属于批次 {batch_id}"})
        except (ValueError, TypeError):
            errors.append({"index": i, "reason": f"node_id 无效: {item.get('node_id')!r}"})
        try:
            dt = parse_ts(item.get("ts"))
            entry["dt"] = dt
            entry["ts"] = iso(dt)
        except (ValueError, TypeError):
            errors.append({"index": i, "reason": f"时间戳格式无效（需 ISO 8601）: {item.get('ts')!r}"})
        for field, label in (("temperature", "温度"), ("humidity", "湿度")):
            try:
                entry[field] = to_float(item.get(field))
            except (ValueError, TypeError):
                errors.append({"index": i, "reason": f"{label}读数为非数值: {item.get(field)!r}"})
        parsed.append(entry)

    seen = {}
    for i, p in enumerate(parsed):
        if "artifact_id" not in p or "ts" not in p:
            continue
        key = (p["artifact_id"], p["ts"])
        if key in seen:
            errors.append({"index": i, "reason":
                           f"提交内容中存在重复时间戳：文物 {key[0]} 在 {key[1]} 出现多次"})
        seen[key] = i
    if errors:
        raise ValidationError("读数校验失败", errors)

    prev = current_version(conn, batch_id)
    prev_map = {}
    if prev:
        for r in conn.execute("SELECT * FROM readings WHERE version_id=?", (prev["id"],)):
            prev_map[(r["artifact_id"], r["ts"])] = r

    corrections = {}
    for i, p in enumerate(parsed):
        key = (p["artifact_id"], p["ts"])
        old = prev_map.get(key)
        if old is None:
            continue
        same = (old["temperature"] == p["temperature"]
                and old["humidity"] == p["humidity"]
                and old["node_id"] == p["node_id"])
        if same:
            errors.append({"index": i, "reason":
                           f"重复时间戳：文物 {key[0]} 在 {key[1]} 已存在相同读数，拒绝保存"})
        else:
            corrections[key] = old["id"]
    if errors:
        raise ValidationError("读数校验失败", errors)

    version_no = (prev["version_no"] if prev else 0) + 1
    with conn:
        cur = conn.execute(
            "INSERT INTO versions(batch_id, version_no, note, created_at) VALUES (?,?,?,?)",
            (batch_id, version_no, (note or "").strip(), now_iso()),
        )
        version_id = cur.lastrowid
        if prev:
            for key, old in prev_map.items():
                if key in corrections:
                    continue
                conn.execute(
                    """INSERT INTO readings(version_id, artifact_id, node_id, ts,
                       temperature, humidity, corrected_from) VALUES (?,?,?,?,?,?,NULL)""",
                    (version_id, old["artifact_id"], old["node_id"], old["ts"],
                     old["temperature"], old["humidity"]),
                )
        for p in parsed:
            key = (p["artifact_id"], p["ts"])
            conn.execute(
                """INSERT INTO readings(version_id, artifact_id, node_id, ts,
                   temperature, humidity, corrected_from) VALUES (?,?,?,?,?,?,?)""",
                (version_id, p["artifact_id"], p["node_id"], p["ts"],
                 p["temperature"], p["humidity"], corrections.get(key)),
            )
        _evaluate_version(conn, version_id, artifacts)

    detail = version_detail(conn, batch_id, version_no)
    result = {
        "batch_id": batch_id,
        "version": version_no,
        "accepted": len(parsed),
        "corrected": len(corrections),
        "events": detail["events"],
        "gaps": detail["gaps"],
        "readings": detail["readings"],
    }
    if prev:
        result["diff"] = version_diff(conn, batch_id, prev["version_no"], version_no)
    return result


def _evaluate_version(conn, version_id, artifacts):
    rows = conn.execute(
        """SELECT r.*, n.name AS node_name, n.seq AS node_seq
           FROM readings r JOIN nodes n ON n.id = r.node_id
           WHERE r.version_id=? ORDER BY r.artifact_id, r.ts""",
        (version_id,),
    ).fetchall()
    by_artifact = defaultdict(list)
    for r in rows:
        d = dict(r)
        d["ts"] = parse_ts(d["ts"])
        by_artifact[d["artifact_id"]].append(d)
    for artifact_id, artifact in artifacts.items():
        events, gaps = evaluate_artifact(by_artifact.get(artifact_id, []), artifact)
        for ev in events:
            conn.execute(
                """INSERT INTO events(version_id, artifact_id, metric, start_ts, end_ts,
                   reading_count, min_value, max_value, limit_lo, limit_hi, nodes,
                   max_deviation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (version_id, artifact_id, ev["metric"], iso(ev["start_ts"]),
                 iso(ev["end_ts"]), ev["reading_count"], ev["min_value"], ev["max_value"],
                 ev["limit_lo"], ev["limit_hi"],
                 json.dumps(ev["nodes"], ensure_ascii=False), ev["max_deviation"]),
            )
        for g in gaps:
            conn.execute(
                """INSERT INTO gaps(version_id, artifact_id, start_ts, end_ts,
                   expected_interval, actual_interval) VALUES (?,?,?,?,?,?)""",
                (version_id, artifact_id, iso(g["start_ts"]), iso(g["end_ts"]),
                 g["expected_interval"], g["actual_interval"]),
            )


def close_event(conn, event_id, note, evidence_no):
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if not row:
        raise NotFoundError(f"事件 {event_id} 不存在")
    note = (note or "").strip()
    evidence_no = (evidence_no or "").strip()
    if not note or not evidence_no:
        raise ValidationError("关闭事件必须填写处置说明及证据编号")
    version = conn.execute(
        "SELECT * FROM versions WHERE id=?", (row["version_id"],)).fetchone()
    cur = current_version(conn, version["batch_id"])
    if not cur or cur["id"] != version["id"]:
        raise ValidationError("仅可关闭当前评估版本的事件；历史版本的事件与结论仅可查询")
    if row["status"] == "closed":
        raise ValidationError("事件已关闭，请勿重复操作")
    with conn:
        conn.execute(
            """UPDATE events SET status='closed', close_note=?, close_evidence=?,
               closed_at=? WHERE id=?""",
            (note, evidence_no, now_iso(), event_id),
        )
    names = _artifact_names(conn, version["batch_id"])
    updated = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    return _event_dict(updated, names)
