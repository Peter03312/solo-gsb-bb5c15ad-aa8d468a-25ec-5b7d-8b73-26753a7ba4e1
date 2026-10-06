"""风险事件评估：按时间轴核验读数。

规则：
- 同一文物同一指标（温度/湿度）的连续越界读数合并为一个风险事件，合并只按时间轴
  进行，与所在节点无关，因此事件可跨节点；
- 相邻两条读数的时间间隔超过约定采样间隔时，记为缺测区间，并中断正在进行的事件
  （事件在缺测前的最后一条越界读数处结束）。
"""
from __future__ import annotations

# (读数字段, 下限字段, 上限字段, 中文名)
METRICS = (
    ("temperature", "temp_min", "temp_max", "温度"),
    ("humidity", "hum_min", "hum_max", "湿度"),
)

METRIC_LABELS = {"temperature": "温度", "humidity": "湿度"}


def evaluate_artifact(readings, artifact):
    """评估单件文物的全部读数。

    readings: 按 ts 升序的字典列表，键含 ts(datetime)、temperature、humidity、
              node_id、node_name、node_seq。
    artifact: 含 temp_min/temp_max/hum_min/hum_max/sample_interval（秒）的映射。
    返回 (events, gaps)。
    """
    interval = artifact["sample_interval"]
    readings = sorted(readings, key=lambda r: r["ts"])

    gaps = []
    for prev, cur in zip(readings, readings[1:]):
        delta = (cur["ts"] - prev["ts"]).total_seconds()
        if delta > interval:
            gaps.append({
                "start_ts": prev["ts"],
                "end_ts": cur["ts"],
                "expected_interval": interval,
                "actual_interval": delta,
            })

    events = []
    for metric, lo_key, hi_key, _label in METRICS:
        lo, hi = artifact[lo_key], artifact[hi_key]
        run = None
        prev = None
        for r in readings:
            if prev is not None and (r["ts"] - prev["ts"]).total_seconds() > interval:
                # 缺测区间：中断进行中的事件
                if run:
                    events.append(_finalize(run, metric, lo, hi))
                    run = None
            value = r[metric]
            if value < lo or value > hi:
                if run is None:
                    run = []
                run.append(r)
            else:
                if run:
                    events.append(_finalize(run, metric, lo, hi))
                    run = None
            prev = r
        if run:
            events.append(_finalize(run, metric, lo, hi))
    return events, gaps


def _finalize(run, metric, lo, hi):
    values = [r[metric] for r in run]
    nodes = []
    seen = set()
    for r in sorted(run, key=lambda x: (x["node_seq"], x["ts"])):
        if r["node_id"] not in seen:
            seen.add(r["node_id"])
            nodes.append({"id": r["node_id"], "name": r["node_name"]})
    deviation = max((lo - v) if v < lo else (v - hi) for v in values)
    return {
        "metric": metric,
        "start_ts": run[0]["ts"],
        "end_ts": run[-1]["ts"],
        "reading_count": len(run),
        "min_value": min(values),
        "max_value": max(values),
        "limit_lo": lo,
        "limit_hi": hi,
        "nodes": nodes,
        "max_deviation": round(deviation, 6),
    }
