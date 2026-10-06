#!/usr/bin/env bash
# 端到端演示：创建批次 → 节点 → 文物 → 读数（跨节点越界 + 缺测）→ 关闭事件 →
# 更正读数生成新版本 → 查看版本差异 → 查询跨文物共因风险组。
# 用法：BASE=http://localhost:8000 ./scripts/demo.sh
set -euo pipefail
BASE="${BASE:-http://localhost:8000}"
JSON='Content-Type: application/json'

echo "== 1. 创建批次 =="
curl -s -X POST "$BASE/api/batches" -H "$JSON" \
  -d '{"name":"2026 春季书画巡展","description":"本馆 → 巡展场馆"}'
echo

echo "== 2. 添加有序运输节点 =="
curl -s -X POST "$BASE/api/batches/1/nodes" -H "$JSON" -d '{"seq":1,"name":"库房","location":"本馆"}'
curl -s -X POST "$BASE/api/batches/1/nodes" -H "$JSON" -d '{"seq":2,"name":"运输车"}'
curl -s -X POST "$BASE/api/batches/1/nodes" -H "$JSON" -d '{"seq":3,"name":"展厅","location":"巡展场馆"}'
echo

echo "== 3. 登记文物（温湿度上下限 + 采样间隔 600 秒）=="
curl -s -X POST "$BASE/api/batches/1/artifacts" -H "$JSON" \
  -d '{"name":"青花瓷瓶","temp_min":18,"temp_max":24,"hum_min":45,"hum_max":60,"sample_interval":600}'
echo
curl -s -X POST "$BASE/api/batches/1/artifacts" -H "$JSON" \
  -d '{"name":"绢画","temp_min":18,"temp_max":24,"hum_min":45,"hum_max":60,"sample_interval":600}'
echo

echo "== 4. 提交读数：文物 1 温度跨节点连续越界后缺测；文物 2 在运输车同期越界 =="
curl -s -X POST "$BASE/api/batches/1/readings" -H "$JSON" -d '{
  "note": "首批数据",
  "readings": [
    {"artifact_id":1,"node_id":1,"ts":"2026-10-01T08:00:00Z","temperature":20,"humidity":50},
    {"artifact_id":1,"node_id":1,"ts":"2026-10-01T08:10:00Z","temperature":26,"humidity":50},
    {"artifact_id":1,"node_id":2,"ts":"2026-10-01T08:20:00Z","temperature":27,"humidity":50},
    {"artifact_id":1,"node_id":3,"ts":"2026-10-01T08:30:00Z","temperature":25,"humidity":50},
    {"artifact_id":1,"node_id":3,"ts":"2026-10-01T08:40:00Z","temperature":21,"humidity":50},
    {"artifact_id":1,"node_id":3,"ts":"2026-10-01T09:40:00Z","temperature":26,"humidity":50},
    {"artifact_id":1,"node_id":3,"ts":"2026-10-01T09:50:00Z","temperature":21,"humidity":50},
    {"artifact_id":2,"node_id":2,"ts":"2026-10-01T08:15:00Z","temperature":26,"humidity":50},
    {"artifact_id":2,"node_id":2,"ts":"2026-10-01T08:25:00Z","temperature":26,"humidity":50},
    {"artifact_id":2,"node_id":3,"ts":"2026-10-01T08:35:00Z","temperature":21,"humidity":50}
  ]}'
echo

echo "== 5. 校验拒绝示例：重复时间戳 / 非数值 / 无序上下限 =="
curl -s -X POST "$BASE/api/batches/1/readings" -H "$JSON" \
  -d '{"readings":[{"artifact_id":1,"node_id":1,"ts":"2026-10-01T08:00:00Z","temperature":20,"humidity":50}]}'
curl -s -X POST "$BASE/api/batches/1/readings" -H "$JSON" \
  -d '{"readings":[{"artifact_id":1,"node_id":1,"ts":"2026-10-01T10:00:00Z","temperature":"偏高","humidity":50}]}'
curl -s -X POST "$BASE/api/batches/1/artifacts" -H "$JSON" \
  -d '{"name":"错误示例","temp_min":30,"temp_max":10,"hum_min":45,"hum_max":60,"sample_interval":600}'
echo

echo "== 6. 关闭事件（须填写处置说明及证据编号）=="
EVENT_ID=$(curl -s "$BASE/api/batches/1/events?status=open" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
curl -s -X POST "$BASE/api/events/$EVENT_ID/close" -H "$JSON" \
  -d '{"note":"已调整车厢温控并复查记录","evidence_no":"EV-2026-0001"}'
echo

echo "== 7. 更正读数 → 生成新评估版本（新版事件不继承关闭状态）=="
curl -s -X POST "$BASE/api/batches/1/readings" -H "$JSON" \
  -d '{"note":"更正运输车读数","readings":[{"artifact_id":1,"node_id":2,"ts":"2026-10-01T08:20:00Z","temperature":22,"humidity":50}]}'
echo

echo "== 8. 版本差异 v1 → v2 =="
curl -s "$BASE/api/batches/1/diff?from=1&to=2"
echo

echo "== 9. 批次风险摘要 =="
curl -s "$BASE/api/batches/1/summary"
echo

echo "== 10. 跨文物共因风险组：v1 文物1/文物2 在运输车同期温度越界（成组）；当前 v2 更正后共因消失 =="
echo "-- 当前版本（v2，更正后）--"
curl -s "$BASE/api/batches/1/risk-groups" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("当前版 v%d 组数: %d" % (d["version"], len(d["groups"])))'
echo "-- 历史版本 v1 --"
curl -s "$BASE/api/batches/1/risk-groups?version=1"
echo

echo "== 11. 旧版 v1 分组保持可查，关闭事件只更新计数（成员仍为 2 个事件）=="
curl -s "$BASE/api/batches/1/risk-groups?version=1" | python3 -c 'import json,sys; d=json.load(sys.stdin); g=d["groups"][0]; print("v1 组:", g["group_id"], g["metric_label"], "文物数", g["artifact_count"], "事件数", g["event_count"], "共有节点", [n["name"] for n in g["common_nodes"]], "开放/关闭", g["open_events"], "/", g["closed_events"])'
