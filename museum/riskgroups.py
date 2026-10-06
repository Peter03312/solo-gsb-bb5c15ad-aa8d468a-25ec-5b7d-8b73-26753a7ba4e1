"""跨文物共因风险组识别（按评估版本）。

连边规则（两个事件须同时满足）：
1. 属于同一指标（温度/湿度）；
2. 起止时间构成的**闭区间**相交（端点相接也算，即 start_a <= end_b 且 start_b <= end_a）；
3. 涉及至少一个相同的运输节点。

取全部连边的**连通分量**：只有覆盖至少两件不同文物的分量才成组；仅经传递相连
（A-B 有共同节点、B-C 有共同节点，但 A-C 没有）的成员同样纳入同一组，此时
"全组共有节点"（全体成员节点集合的交集）可能为空——并不要求每对成员都有共同节点。
"""
from __future__ import annotations

from datetime import datetime, timezone

METRIC_LABELS = {"temperature": "温度", "humidity": "湿度"}


def _parse_ts(value):
    """兼容 ISO 8601 字符串与 datetime，统一为可比较的 UTC datetime。"""
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _node_ref(node):
    """事件 nodes 列表元素支持 {"id":..,"name":..} 字典或裸节点 id。"""
    if isinstance(node, dict):
        return node["id"], node.get("name")
    return node, None


def _closed_intervals_overlap(a_start, a_end, b_start, b_end):
    """闭区间相交：端点恰好相接也视为相交。"""
    return a_start <= b_end and b_start <= a_end


def find_risk_groups(events):
    """按上述规则识别共因风险组。

    events: 事件字典列表，每个事件含：
            id、artifact_id、metric、start_ts、end_ts、status、
            nodes（[{"id","name"}] 或裸 id）；
            start_ts/end_ts 可为 ISO 字符串或 datetime；
            artifact_name 可选（用于输出）。
    返回成组列表（已过滤单文物分量），每组含：
      group_id、metric、metric_label、start_ts、end_ts、
      artifact_count、event_count、open_events、closed_events、
      artifacts、events、common_nodes、links。
    """
    events = sorted(events, key=lambda e: e["id"])
    n = len(events)

    starts, ends, node_sets, node_names = [], [], [], {}
    for e in events:
        starts.append(_parse_ts(e["start_ts"]))
        ends.append(_parse_ts(e["end_ts"]))
        ns = set()
        for raw in e.get("nodes") or []:
            nid, nname = _node_ref(raw)
            ns.add(nid)
            if nname and nid not in node_names:
                node_names[nid] = nname
        node_sets.append(ns)

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # 记录每条连边及其共同节点（供输出展示传递路径）
    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            if events[i]["metric"] != events[j]["metric"]:
                continue
            if not _closed_intervals_overlap(
                    starts[i], ends[i], starts[j], ends[j]):
                continue
            shared = node_sets[i] & node_sets[j]
            if not shared:
                continue
            union(i, j)
            edges.append((i, j, shared))

    components = {}
    for i in range(n):
        components.setdefault(find(i), []).append(i)

    groups = []
    for idxs in components.values():
        artifacts = {}
        for i in idxs:
            e = events[i]
            artifacts.setdefault(
                e["artifact_id"],
                e.get("artifact_name") or str(e["artifact_id"]),
            )
        if len(artifacts) < 2:
            continue  # 只覆盖一件文物的分量不成组

        members = sorted(idxs, key=lambda i: (starts[i], events[i]["id"]))
        group_start = min(starts[i] for i in idxs)
        group_end = max(ends[i] for i in idxs)
        member_set = set(idxs)

        common_ids = None
        for i in idxs:
            common_ids = node_sets[i] if common_ids is None else common_ids & node_sets[i]
        common_ids = common_ids or set()

        def node_obj(nid):
            return {"id": nid, "name": node_names.get(nid, str(nid))}

        member_events = [{
            "id": events[i]["id"],
            "artifact_id": events[i]["artifact_id"],
            "artifact_name": artifacts[events[i]["artifact_id"]],
            "metric": events[i]["metric"],
            "start_ts": events[i]["start_ts"],
            "end_ts": events[i]["end_ts"],
            "status": events[i].get("status", "open"),
            "nodes": [node_obj(nid) for nid in sorted(node_sets[i])],
        } for i in members]

        statuses = [events[i].get("status", "open") for i in idxs]
        links = [{
            "events": [events[i]["id"], events[j]["id"]],
            "shared_nodes": [node_obj(nid) for nid in sorted(shared)],
        } for i, j, shared in sorted(
            edges, key=lambda t: (events[t[0]]["id"], events[t[1]]["id"]))
            if i in member_set and j in member_set]

        metric = events[idxs[0]]["metric"]
        groups.append({
            "group_id": None,  # 排序后统一编号
            "metric": metric,
            "metric_label": METRIC_LABELS.get(metric, metric),
            "start_dt": group_start,
            "end_dt": group_end,
            "start_ts": events[members[0]]["start_ts"],
            "end_ts": _iso(group_end),
            "artifact_count": len(artifacts),
            "event_count": len(idxs),
            "open_events": sum(1 for s in statuses if s != "closed"),
            "closed_events": sum(1 for s in statuses if s == "closed"),
            "artifacts": [{"id": aid, "name": artifacts[aid]}
                          for aid in sorted(artifacts)],
            "events": member_events,
            "common_nodes": [node_obj(nid) for nid in sorted(common_ids)],
            "links": links,
        })

    groups.sort(key=lambda g: (g["start_dt"], min(e["id"] for e in g["events"])))
    for no, g in enumerate(groups, 1):
        g["group_id"] = f"G{no}"
        del g["start_dt"]
        del g["end_dt"]
    return groups

def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
