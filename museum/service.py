"""业务逻辑层：批次/节点/文物管理、读数提交与版本化评估、事件关闭、版本差异、离线采集包上传。"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timezone

from .diff import diff_events, diff_readings
from .evaluate import METRIC_LABELS, evaluate_artifact
from .riskgroups import find_risk_groups

# 离线采集包上传在进程内串行化，配合 (批次, 包编号) 唯一约束保证并发安全与幂等。
_package_lock = threading.RLock()

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


class ConflictError(Exception):
    """采集包合并冲突或同编号重试内容不一致（HTTP 409）。"""

    def __init__(self, message, payload=None):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}


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
    parsed = _parse_readings(items, batch_id, artifacts, nodes)
    result = _apply_version(conn, batch_id, parsed, note, artifacts)
    result["accepted"] = len(parsed)
    return result


def _parse_readings(items, batch_id, artifacts, nodes):
    """校验并规范化读数条目；任一不合法即抛 ValidationError（details 逐条原因）。

    规范化：时间戳统一为 UTC ISO 字符串，温湿度转 float，id 转 int。
    """
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
    return parsed


def _version_reading_map(conn, version_id):
    return {(r["artifact_id"], r["ts"]): r
            for r in conn.execute("SELECT * FROM readings WHERE version_id=?",
                                  (version_id,)).fetchall()}


def _check_duplicates(parsed, prev_map):
    """与当前版本比对：完全相同（节点/温湿度一致）→ 重复时间戳拒绝；
    键相同但内容不同 → 更正。返回 corrections: {key: 旧读数 id}。"""
    errors = []
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
    return corrections


def _create_version_rows(conn, batch_id, parsed, note, artifacts):
    """在当前版本之上合并规范化读数并写入新版本（不含事务，供调用方包裹）。

    返回 (version_no, prev_row, corrections)。调用方须在同一事务内完成其余写入。
    """
    prev = current_version(conn, batch_id)
    prev_map = _version_reading_map(conn, prev["id"]) if prev else {}
    corrections = _check_duplicates(parsed, prev_map)

    version_no = (prev["version_no"] if prev else 0) + 1
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
    return version_no, prev, corrections


def _build_version_result(conn, batch_id, version_no, prev, corrections):
    detail = version_detail(conn, batch_id, version_no)
    result = {
        "batch_id": batch_id,
        "version": version_no,
        "corrected": len(corrections),
        "events": detail["events"],
        "gaps": detail["gaps"],
        "readings": detail["readings"],
    }
    if prev:
        result["diff"] = version_diff(conn, batch_id, prev["version_no"], version_no)
    return result


def _apply_version(conn, batch_id, parsed, note, artifacts):
    """在当前版本之上合并规范化读数并重新评估，生成一个新版本。

    调用方负责输入校验；本函数只处理“相对当前版”的追加/更正与版本创建。
    返回版本结果（不含 accepted，由调用方补充）。
    """
    with conn:
        version_no, prev, corrections = _create_version_rows(
            conn, batch_id, parsed, note, artifacts)
    return _build_version_result(conn, batch_id, version_no, prev, corrections)


# ---------------------------------------------------------------- 离线采集包

def _parse_package_no(value):
    if isinstance(value, bool) or value is None:
        raise ValidationError(f"包编号无效: {value!r}")
    if isinstance(value, int):
        text = str(value)
    else:
        text = str(value).strip()
    if not text:
        raise ValidationError("包编号不能为空")
    if len(text) > 128:
        raise ValidationError("包编号长度不能超过 128 个字符")
    return text


def _parse_base_version(value):
    try:
        base = to_int(value)
    except (ValueError, TypeError):
        raise ValidationError(f"基准版本号必须为非负整数: {value!r}")
    if base < 0:
        raise ValidationError(f"基准版本号不能为负数: {base}")
    return base


def _canonical_readings(parsed):
    """规范化读数用于幂等指纹/存档（与提交顺序无关，按文物、时间戳排序）。"""
    return [
        {
            "artifact_id": p["artifact_id"],
            "node_id": p["node_id"],
            "ts": p["ts"],
            "temperature": p["temperature"],
            "humidity": p["humidity"],
        }
        for p in sorted(parsed, key=lambda p: (p["artifact_id"], p["ts"]))
    ]


def _package_fingerprint(base_version, note, canonical):
    return json.dumps(
        {"base_version": base_version, "note": (note or "").strip(),
         "readings": canonical},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _reading_side(row, artifact_names, node_names):
    return {
        "artifact_id": row["artifact_id"],
        "artifact_name": artifact_names.get(row["artifact_id"], str(row["artifact_id"])),
        "node_id": row["node_id"],
        "node_name": node_names.get(row["node_id"], str(row["node_id"])),
        "ts": row["ts"],
        "temperature": row["temperature"],
        "humidity": row["humidity"],
    }


def _conflict_payload(conn, batch_id, base_version, current_no, parsed,
                      base_map, cur_map, artifacts, nodes):
    """构造基准版与当前版不一致的冲突清单。"""
    artifact_names = {a["id"]: a["name"] for a in artifacts.values()}
    node_names = {n["id"]: n["name"] for n in nodes.values()}
    conflicts = []
    for p in sorted(parsed, key=lambda p: (p["artifact_id"], p["ts"])):
        key = (p["artifact_id"], p["ts"])
        base_row = base_map.get(key)
        cur_row = cur_map.get(key)
        # 两版都没有该键 → 纯粹新增时间戳，任何人都可追加，不算冲突
        if base_row is None and cur_row is None:
            continue
        # 两版都在且节点/温湿度完全一致 → 设备所见仍然有效，允许更正/追加
        if base_row is not None and cur_row is not None:
            same = (base_row["node_id"] == cur_row["node_id"]
                    and base_row["temperature"] == cur_row["temperature"]
                    and base_row["humidity"] == cur_row["humidity"])
            if same:
                continue
        # 其余情况（两版值不同、或离线期间他人新增了同一时间戳）→ 冲突
        conflicts.append({
            "key": {"artifact_id": key[0],
                    "artifact_name": artifact_names.get(key[0], str(key[0])),
                    "ts": key[1]},
            "base_version": base_version,
            "base": _reading_side(base_row, artifact_names, node_names) if base_row else None,
            "current_version": current_no,
            "current": _reading_side(cur_row, artifact_names, node_names) if cur_row else None,
            "package": {
                "artifact_id": p["artifact_id"],
                "artifact_name": artifact_names.get(p["artifact_id"], str(p["artifact_id"])),
                "node_id": p["node_id"],
                "node_name": node_names.get(p["node_id"], str(p["node_id"])),
                "ts": p["ts"],
                "temperature": p["temperature"],
                "humidity": p["humidity"],
            },
        })
    return {
        "error": (f"采集包基准版本 v{base_version} 已前进到当前版本 v{current_no}，"
                  "部分（文物, 时间戳）在两版间不一致，整包未写入"),
        "batch_id": batch_id,
        "base_version": base_version,
        "current_version": current_no,
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
    }


def upload_package(conn, batch_id, package_no, base_version, readings, note=""):
    """上传离线暂存的采集包，成功合并只生成一个新评估版本。

    - base_version=0 表示采集时尚无任何评估版本；
    - 当前版本已前进时，仅当包内每个（文物, 时间戳）在基准版与当前版的读数
      （节点、温湿度）完全一致才合并，否则返回冲突清单，整包不写入；
    - 同（批次, 包编号）重试：基准版本、备注、读数完全一致 → 返回首次结果；
      任一不同 → 409 拒绝。并发上传同样遵守（进程锁 + 唯一约束）。

    返回 (status, response, replay)。
    """
    get_batch(conn, batch_id)
    pkg_no = _parse_package_no(package_no)
    base_no = _parse_base_version(base_version)
    if not isinstance(readings, list) or not readings:
        raise ValidationError("读数列表不能为空")
    note = (note or "").strip()

    artifacts = {a["id"]: a for a in list_artifacts(conn, batch_id)}
    nodes = {n["id"]: n for n in list_nodes(conn, batch_id)}
    # 非法读数、跨批次文物/节点、包内重复时间戳 → 明确报错，不产生版本/不留记录
    parsed = _parse_readings(readings, batch_id, artifacts, nodes)
    canonical = _canonical_readings(parsed)
    fingerprint = _package_fingerprint(base_no, note, canonical)

    with _package_lock:
        existing = get_package_record(conn, batch_id, pkg_no)
        if existing is not None:
            replay = json.loads(existing["response_json"])
            if existing["fingerprint"] != fingerprint:
                raise ConflictError(
                    f"批次 {batch_id} 的包 {pkg_no} 已上传但内容不同"
                    f"（原基准版本 v{existing['base_version']}），拒绝重复编号",
                    {
                        "error": f"包编号 {pkg_no} 已存在且内容不一致，拒绝写入",
                        "batch_id": batch_id,
                        "package_no": pkg_no,
                        "original": {
                            "base_version": existing["base_version"],
                            "note": existing["note"],
                            "status": existing["status"],
                            "result_version": existing["result_version"],
                        },
                    },
                )
            return (409 if existing["status"] == "conflict" else 201,
                    replay, True)

        # 基准版本必须真实存在（0 表示尚无版本，仅当当前确实无版本时合法）
        cur = current_version(conn, batch_id)
        current_no = cur["version_no"] if cur else 0
        if base_no != 0:
            get_version(conn, batch_id, base_no)
        elif current_no != 0:
            raise ValidationError(
                f"基准版本号为 0（采集时无版本），但批次当前已存在版本 v{current_no}，"
                "请以实际基准版本重新上传")
        if base_no > current_no:
            raise ValidationError(
                f"基准版本 v{base_no} 高于当前版本 v{current_no}，无法合并")

        status, response = "merged", None
        if base_no < current_no:
            base_ver = get_version(conn, batch_id, base_no)
            base_map = _version_reading_map(conn, base_ver["id"])
            cur_map = _version_reading_map(conn, cur["id"])
            payload = _conflict_payload(
                conn, batch_id, base_no, current_no, parsed,
                base_map, cur_map, artifacts, nodes)
            if payload["conflicts"]:
                status, response = "conflict", payload

        if status == "conflict":
            try:
                record_id = _insert_package_record(
                    conn, batch_id, pkg_no, base_no, note, fingerprint, canonical,
                    "conflict", None, response)
            except sqlite3.IntegrityError:
                # 并发兜底：另一进程已写入同编号包
                existing = get_package_record(conn, batch_id, pkg_no)
                if existing is not None and existing["fingerprint"] != fingerprint:
                    raise ConflictError(
                        f"批次 {batch_id} 的包 {pkg_no} 已上传但内容不同，拒绝重复编号",
                        {"error": f"包编号 {pkg_no} 已存在且内容不一致，拒绝写入",
                         "batch_id": batch_id, "package_no": pkg_no})
                if existing is not None:
                    return 409, json.loads(existing["response_json"]), True
                raise
            response["package_no"] = pkg_no
            response["package_id"] = record_id
            # 冻结首次冲突结果，供同内容重放
            _update_package_response(conn, record_id, response)
            return 409, response, False

        # 合并沿用现有校验（重复时间戳/更正）与风险评估规则；只生成一个新版本。
        # 新版本与采集包记录在同一事务落库，保证“成功必有记录、失败整包回滚”。
        try:
            with conn:
                version_no, prev, corrections = _create_version_rows(
                    conn, batch_id, parsed, note, artifacts)
                result = _build_version_result(
                    conn, batch_id, version_no, prev, corrections)
                result["accepted"] = len(parsed)
                result["package_no"] = pkg_no
                result["base_version"] = base_no
                cur_pkg = conn.execute(
                    """INSERT INTO upload_packages(batch_id, package_no, base_version,
                       note, fingerprint, readings_json, status, result_version,
                       response_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (batch_id, pkg_no, base_no, note, fingerprint,
                     json.dumps(canonical, ensure_ascii=False), "merged",
                     version_no, json.dumps(result, ensure_ascii=False), now_iso()),
                )
                record_id = cur_pkg.lastrowid
                result["package_id"] = record_id
                # 以含 package_id 的最终结果回填冻结响应，保证重放内容一致
                conn.execute("UPDATE upload_packages SET response_json=? WHERE id=?",
                             (json.dumps(result, ensure_ascii=False), record_id))
        except sqlite3.IntegrityError:
            # 并发兜底：同编号包已由另一请求落库，按幂等规则重放/拒绝
            existing = get_package_record(conn, batch_id, pkg_no)
            if existing is not None and existing["fingerprint"] != fingerprint:
                raise ConflictError(
                    f"批次 {batch_id} 的包 {pkg_no} 已上传但内容不同，拒绝重复编号",
                    {"error": f"包编号 {pkg_no} 已存在且内容不一致，拒绝写入",
                     "batch_id": batch_id, "package_no": pkg_no})
            if existing is not None:
                return 201, json.loads(existing["response_json"]), True
            raise
        return 201, result, False


def _insert_package_record(conn, batch_id, pkg_no, base_no, note, fingerprint,
                           canonical, status, result_version, response):
    try:
        with conn:
            cur = conn.execute(
                """INSERT INTO upload_packages(batch_id, package_no, base_version, note,
                   fingerprint, readings_json, status, result_version, response_json,
                   created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (batch_id, pkg_no, base_no, note, fingerprint,
                 json.dumps(canonical, ensure_ascii=False), status, result_version,
                 json.dumps(response, ensure_ascii=False), now_iso()),
            )
            return cur.lastrowid
    except sqlite3.IntegrityError:
        # 并发下另一请求已写入同编号包：交由上层按幂等规则重放/拒绝（防御性兜底）
        existing = get_package_record(conn, batch_id, pkg_no)
        if existing is None or existing["fingerprint"] == fingerprint:
            raise
        raise ConflictError(
            f"批次 {batch_id} 的包 {pkg_no} 已上传但内容不同，拒绝重复编号",
            {"error": f"包编号 {pkg_no} 已存在且内容不一致，拒绝写入",
             "batch_id": batch_id, "package_no": pkg_no})


def _update_package_response(conn, record_id, response):
    with conn:
        conn.execute("UPDATE upload_packages SET response_json=? WHERE id=?",
                     (json.dumps(response, ensure_ascii=False), record_id))


def get_package_record(conn, batch_id, package_no):
    return conn.execute(
        "SELECT * FROM upload_packages WHERE batch_id=? AND package_no=?",
        (batch_id, str(package_no)),
    ).fetchone()


def list_packages(conn, batch_id):
    """列出批次的采集包上传记录（不含冻结响应全文，含存档读数）。"""
    get_batch(conn, batch_id)
    rows = conn.execute(
        "SELECT * FROM upload_packages WHERE batch_id=? ORDER BY id",
        (batch_id,),
    ).fetchall()
    return [
        {
            "package_id": r["id"],
            "package_no": r["package_no"],
            "base_version": r["base_version"],
            "note": r["note"],
            "status": r["status"],
            "result_version": r["result_version"],
            "readings": json.loads(r["readings_json"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def get_package_status(conn, batch_id, package_no):
    get_batch(conn, batch_id)
    r = get_package_record(conn, batch_id, str(package_no))
    if r is None:
        raise NotFoundError(f"批次 {batch_id} 的采集包 {package_no} 不存在")
    return {
        "package_id": r["id"],
        "package_no": r["package_no"],
        "batch_id": batch_id,
        "base_version": r["base_version"],
        "note": r["note"],
        "status": r["status"],
        "result_version": r["result_version"],
        "readings": json.loads(r["readings_json"]),
        "response": json.loads(r["response_json"]),
        "created_at": r["created_at"],
    }


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
