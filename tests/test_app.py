"""评估逻辑与 HTTP API 的端到端测试（仅标准库）。"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from museum.evaluate import evaluate_artifact
from museum.riskgroups import find_risk_groups

BASE = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)


def _ev(eid, aid, metric, s, e, node_ids, status="open"):
    """构造风险组算法输入（s/e 为相对 BASE 的分钟数）。"""
    def ts(m):
        return (BASE + timedelta(minutes=m)).isoformat()
    return {
        "id": eid, "artifact_id": aid, "artifact_name": f"文物{aid}",
        "metric": metric, "start_ts": ts(s), "end_ts": ts(e),
        "status": status,
        "nodes": [{"id": nid, "name": f"节点{nid}"} for nid in node_ids],
    }


def make_reading(minutes, temp, hum, node_id=1, node_name="节点", node_seq=1):
    return {
        "ts": BASE + timedelta(minutes=minutes),
        "temperature": temp,
        "humidity": hum,
        "node_id": node_id,
        "node_name": node_name,
        "node_seq": node_seq,
    }


ARTIFACT = {"temp_min": 18.0, "temp_max": 24.0, "hum_min": 45.0, "hum_max": 60.0,
            "sample_interval": 600}


class EvaluateTest(unittest.TestCase):
    def test_merge_across_nodes(self):
        """同一指标的连续越界读数跨节点合并为一个事件。"""
        readings = [
            make_reading(0, 20, 50, 1, "库房", 1),
            make_reading(10, 26, 50, 1, "库房", 1),   # 越界开始
            make_reading(20, 27, 50, 2, "运输车", 2),  # 跨节点仍连续
            make_reading(30, 25, 50, 3, "展厅", 3),
            make_reading(40, 21, 50, 3, "展厅", 3),   # 恢复正常
        ]
        events, gaps = evaluate_artifact(readings, ARTIFACT)
        self.assertEqual(len(events), 1)
        self.assertEqual(gaps, [])
        ev = events[0]
        self.assertEqual(ev["metric"], "temperature")
        self.assertEqual(ev["reading_count"], 3)
        self.assertEqual([n["name"] for n in ev["nodes"]], ["库房", "运输车", "展厅"])
        self.assertAlmostEqual(ev["max_deviation"], 3.0)

    def test_gap_interrupts_event(self):
        """相邻读数间隔超过采样间隔：记缺测区间并中断事件。"""
        readings = [
            make_reading(0, 26, 50),    # 越界
            make_reading(10, 27, 50),   # 越界
            make_reading(60, 28, 50),   # 间隔 50 分钟 > 10 分钟 → 缺测，事件中断
            make_reading(70, 21, 50),
        ]
        events, gaps = evaluate_artifact(readings, ARTIFACT)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["expected_interval"], 600)
        self.assertEqual(gaps[0]["actual_interval"], 3000)
        temp_events = [e for e in events if e["metric"] == "temperature"]
        self.assertEqual(len(temp_events), 2)
        self.assertEqual(temp_events[0]["reading_count"], 2)
        self.assertEqual(temp_events[1]["reading_count"], 1)

    def test_single_outlier_is_event(self):
        readings = [make_reading(0, 20, 50), make_reading(10, 20, 75)]
        events, _ = evaluate_artifact(readings, ARTIFACT)
        hum = [e for e in events if e["metric"] == "humidity"]
        self.assertEqual(len(hum), 1)
        self.assertEqual(hum[0]["reading_count"], 1)

    def test_all_in_range(self):
        readings = [make_reading(i * 10, 21, 50) for i in range(5)]
        events, gaps = evaluate_artifact(readings, ARTIFACT)
        self.assertEqual(events, [])
        self.assertEqual(gaps, [])


class RiskGroupAlgorithmTest(unittest.TestCase):
    def test_basic_group_same_metric_overlap_shared_node(self):
        """同指标、闭区间相交、共享节点 → 成组。"""
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 30, [1, 2]),
            _ev(2, 2, "temperature", 20, 60, [2, 3]),
        ])
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g["artifact_count"], 2)
        self.assertEqual(g["event_count"], 2)
        self.assertEqual(g["open_events"], 2)
        self.assertEqual(g["closed_events"], 0)
        self.assertEqual({a["id"] for a in g["artifacts"]}, {1, 2})
        self.assertEqual([n["id"] for n in g["common_nodes"]], [2])
        self.assertEqual(len(g["links"]), 1)
        self.assertEqual(g["links"][0]["events"], [1, 2])
        self.assertEqual([n["id"] for n in g["links"][0]["shared_nodes"]], [2])

    def test_closed_interval_touching_endpoints_overlap(self):
        """闭区间：一个事件 end 恰好等于另一个 start → 相交。"""
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 20, [1]),
            _ev(2, 2, "temperature", 20, 40, [1]),
        ])
        self.assertEqual(len(groups), 1)

    def test_different_metric_no_edge(self):
        """指标不同（温度 vs 湿度）即使时间重叠且共享节点也不连边。"""
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 30, [1]),
            _ev(2, 2, "humidity", 10, 20, [1]),
        ])
        self.assertEqual(groups, [])

    def test_no_overlap_no_edge(self):
        """闭区间不相交（首尾错开）不连边。"""
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 10, [1]),
            _ev(2, 2, "temperature", 20, 30, [1]),
        ])
        self.assertEqual(groups, [])

    def test_overlap_but_no_shared_node_no_edge(self):
        """时间相交但无共同运输节点不连边。"""
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 30, [1]),
            _ev(2, 2, "temperature", 10, 20, [2]),
        ])
        self.assertEqual(groups, [])

    def test_single_artifact_component_not_a_group(self):
        """连通分量只覆盖一件文物（即便有多个事件）不成组。"""
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 30, [1]),
            _ev(2, 1, "temperature", 20, 60, [1]),
        ])
        self.assertEqual(groups, [])

    def test_transitive_chain_includes_all_common_nodes_may_be_empty(self):
        """A-B 共享节点 1、B-C 共享节点 2，A-C 无共同节点：传递相连全纳入，
        全组共有节点为空，且不得误判 A-C 有共同节点。"""
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 100, [1]),
            _ev(2, 2, "temperature", 0, 100, [1, 2]),
            _ev(3, 3, "temperature", 0, 100, [2]),
        ])
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g["artifact_count"], 3)
        self.assertEqual(g["event_count"], 3)
        self.assertEqual(g["common_nodes"], [])  # 无全体共有节点
        pairs = {tuple(sorted(lk["events"])) for lk in g["links"]}
        self.assertEqual(pairs, {(1, 2), (2, 3)})  # (1,3) 不是连边

    def test_status_counts(self):
        groups = find_risk_groups([
            _ev(1, 1, "temperature", 0, 30, [1], status="closed"),
            _ev(2, 2, "temperature", 10, 40, [1], status="open"),
        ])
        g = groups[0]
        self.assertEqual(g["open_events"], 1)
        self.assertEqual(g["closed_events"], 1)


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls.tmp.name, "test.db")
        cls.server = app.make_server("127.0.0.1", 0, db_path)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_full_flow(self):
        # 创建批次
        st, batch = self.req("POST", "/api/batches",
                             {"name": "2026 春季书画巡展", "description": "测试"})
        self.assertEqual(st, 201)
        bid = batch["id"]

        # 有序节点
        for seq, name in [(1, "库房"), (2, "运输车"), (3, "展厅")]:
            st, _ = self.req("POST", f"/api/batches/{bid}/nodes",
                             {"seq": seq, "name": name})
            self.assertEqual(st, 201)
        # 重复节点序号 → 拒绝
        st, err = self.req("POST", f"/api/batches/{bid}/nodes",
                           {"seq": 1, "name": "重复"})
        self.assertEqual(st, 400)
        self.assertIn("已存在", err["error"])

        # 无序上下限 → 拒绝并说明原因
        st, err = self.req("POST", f"/api/batches/{bid}/artifacts",
                           {"name": "坏文物", "temp_min": 25, "temp_max": 18,
                            "hum_min": 45, "hum_max": 60, "sample_interval": 600})
        self.assertEqual(st, 400)
        self.assertIn("无序", json.dumps(err, ensure_ascii=False))

        # 正常文物
        st, art = self.req("POST", f"/api/batches/{bid}/artifacts",
                           {"name": "青花瓷瓶", "temp_min": 18, "temp_max": 24,
                            "hum_min": 45, "hum_max": 60, "sample_interval": 600})
        self.assertEqual(st, 201)
        aid = art["id"]
        _, detail = self.req("GET", f"/api/batches/{bid}")
        n1, n2, n3 = [n["id"] for n in detail["nodes"]]

        # 非数值读数 → 拒绝并说明原因
        st, err = self.req("POST", f"/api/batches/{bid}/readings",
                           {"readings": [{"artifact_id": aid, "node_id": n1,
                                          "ts": "2026-10-01T08:00:00Z",
                                          "temperature": "abc", "humidity": 50}]})
        self.assertEqual(st, 400)
        self.assertIn("非数值", json.dumps(err, ensure_ascii=False))

        # 第一批读数：温度在库房→运输车→展厅连续越界（跨节点合并），
        # 之后出现超过采样间隔的缺测，再出现一次越界
        readings = [
            {"artifact_id": aid, "node_id": n1, "ts": "2026-10-01T08:00:00Z",
             "temperature": 20, "humidity": 50},
            {"artifact_id": aid, "node_id": n1, "ts": "2026-10-01T08:10:00Z",
             "temperature": 26, "humidity": 50},
            {"artifact_id": aid, "node_id": n2, "ts": "2026-10-01T08:20:00Z",
             "temperature": 27, "humidity": 50},
            {"artifact_id": aid, "node_id": n3, "ts": "2026-10-01T08:30:00Z",
             "temperature": 25, "humidity": 50},
            {"artifact_id": aid, "node_id": n3, "ts": "2026-10-01T08:40:00Z",
             "temperature": 21, "humidity": 50},
            # 缺测：08:40 → 09:40 间隔 60 分钟 > 10 分钟
            {"artifact_id": aid, "node_id": n3, "ts": "2026-10-01T09:40:00Z",
             "temperature": 26, "humidity": 50},
            {"artifact_id": aid, "node_id": n3, "ts": "2026-10-01T09:50:00Z",
             "temperature": 21, "humidity": 50},
        ]
        st, res = self.req("POST", f"/api/batches/{bid}/readings",
                           {"readings": readings, "note": "首批数据"})
        self.assertEqual(st, 201)
        self.assertEqual(res["version"], 1)
        temp_events = [e for e in res["events"] if e["metric"] == "temperature"]
        self.assertEqual(len(temp_events), 2)  # 缺测中断 → 两个事件
        self.assertEqual(temp_events[0]["reading_count"], 3)
        self.assertEqual([n["name"] for n in temp_events[0]["nodes"]],
                         ["库房", "运输车", "展厅"])
        self.assertEqual(len(res["gaps"]), 1)

        # 重复时间戳（相同数值）→ 拒绝
        st, err = self.req("POST", f"/api/batches/{bid}/readings",
                           {"readings": [readings[0]]})
        self.assertEqual(st, 400)
        self.assertIn("重复时间戳", json.dumps(err, ensure_ascii=False))

        # 同一提交内重复时间戳 → 拒绝
        st, err = self.req("POST", f"/api/batches/{bid}/readings",
                           {"readings": [
                               {"artifact_id": aid, "node_id": n1,
                                "ts": "2026-10-01T10:00:00Z",
                                "temperature": 20, "humidity": 50},
                               {"artifact_id": aid, "node_id": n1,
                                "ts": "2026-10-01T10:00:00Z",
                                "temperature": 22, "humidity": 55}]})
        self.assertEqual(st, 400)
        self.assertIn("重复时间戳", json.dumps(err, ensure_ascii=False))

        # 关闭事件：缺少处置说明/证据编号 → 拒绝
        ev_id = temp_events[0]["id"]
        st, err = self.req("POST", f"/api/events/{ev_id}/close", {"note": "已处置"})
        self.assertEqual(st, 400)
        self.assertIn("证据编号", err["error"])
        # 填写完整 → 关闭成功
        st, closed = self.req("POST", f"/api/events/{ev_id}/close",
                              {"note": "已调整车厢温控并复查", "evidence_no": "EV-2026-001"})
        self.assertEqual(st, 200)
        self.assertEqual(closed["status"], "closed")

        # 更正读数（同一时间戳、不同数值）→ 生成新版本
        st, res2 = self.req("POST", f"/api/batches/{bid}/readings",
                            {"readings": [{"artifact_id": aid, "node_id": n2,
                                           "ts": "2026-10-01T08:20:00Z",
                                           "temperature": 22, "humidity": 50}],
                             "note": "更正运输车读数"})
        self.assertEqual(st, 201)
        self.assertEqual(res2["version"], 2)
        self.assertEqual(res2["corrected"], 1)

        # 新版事件不继承关闭状态
        v2_events = res2["events"]
        self.assertTrue(all(e["status"] == "open" for e in v2_events))

        # 旧版读数与关闭结论保持可查
        st, v1 = self.req("GET", f"/api/batches/{bid}/versions/1")
        self.assertEqual(st, 200)
        old_event = [e for e in v1["events"] if e["id"] == ev_id][0]
        self.assertEqual(old_event["status"], "closed")
        self.assertEqual(old_event["close_evidence"], "EV-2026-001")
        old_reading = [r for r in v1["readings"]
                       if r["ts"].startswith("2026-10-01T08:20")][0]
        self.assertEqual(old_reading["temperature"], 27)  # 旧值保留

        # 版本差异
        st, diff = self.req("GET", f"/api/batches/{bid}/diff?from=1&to=2")
        self.assertEqual(st, 200)
        self.assertEqual(len(diff["readings"]["corrected"]), 1)
        self.assertGreaterEqual(len(diff["events"]["closure_not_inherited"]), 1)

        # 历史版本事件不可再关闭
        st, err = self.req("POST", f"/api/events/{ev_id}/close",
                           {"note": "x", "evidence_no": "y"})
        self.assertEqual(st, 400)

        # 批次风险摘要
        st, summary = self.req("GET", f"/api/batches/{bid}/summary")
        self.assertEqual(st, 200)
        self.assertEqual(summary["current_version"], 2)
        self.assertEqual(summary["gaps"], 1)
        self.assertGreaterEqual(summary["open_events"], 1)

        # 页面可访问
        for path in ("/", f"/batches/{bid}", f"/batches/{bid}/versions/1",
                     f"/batches/{bid}/diff?from=1&to=2"):
            url = f"http://127.0.0.1:{self.port}{path}"
            with urllib.request.urlopen(url) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", resp.headers["Content-Type"])

class RiskGroupApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls.tmp.name, "test-rg.db")
        cls.server = app.make_server("127.0.0.1", 0, db_path)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_risk_groups_across_versions(self):
        st, batch = self.req("POST", "/api/batches", {"name": "共因风险测试批次"})
        self.assertEqual(st, 201)
        bid = batch["id"]
        for seq, name in [(1, "库房"), (2, "运输车"), (3, "展厅")]:
            self.req("POST", f"/api/batches/{bid}/nodes",
                     {"seq": seq, "name": name})
        _, a1 = self.req("POST", f"/api/batches/{bid}/artifacts",
                         {"name": "文物甲", "temp_min": 18, "temp_max": 24,
                          "hum_min": 45, "hum_max": 60, "sample_interval": 3600})
        _, a2 = self.req("POST", f"/api/batches/{bid}/artifacts",
                         {"name": "文物乙", "temp_min": 18, "temp_max": 24,
                          "hum_min": 45, "hum_max": 60, "sample_interval": 3600})
        _, det = self.req("GET", f"/api/batches/{bid}")
        n1, n2, n3 = [n["id"] for n in det["nodes"]]
        id1, id2 = a1["id"], a2["id"]

        def r(aid, node, ts, t, h=50):
            return {"artifact_id": aid, "node_id": node, "ts": ts,
                    "temperature": t, "humidity": h}

        # v1：两文物在运输车（n2）同期温度越界（08:10~08:30 闭区间相交）
        readings = [
            r(id1, n1, "2026-10-01T08:00:00Z", 20),
            r(id1, n2, "2026-10-01T08:10:00Z", 27),
            r(id1, n2, "2026-10-01T08:20:00Z", 27),
            r(id1, n3, "2026-10-01T08:30:00Z", 21),
            r(id2, n2, "2026-10-01T08:10:00Z", 26),
            r(id2, n2, "2026-10-01T08:20:00Z", 26),
            r(id2, n3, "2026-10-01T08:30:00Z", 21),
        ]
        st, res = self.req("POST", f"/api/batches/{bid}/readings",
                           {"readings": readings, "note": "v1"})
        self.assertEqual(st, 201)
        self.assertEqual(res["version"], 1)
        v1_temp_events = [e for e in res["events"] if e["metric"] == "temperature"]
        self.assertEqual(len(v1_temp_events), 2)

        # 当前版风险组
        st, rg = self.req("GET", f"/api/batches/{bid}/risk-groups")
        self.assertEqual(st, 200)
        self.assertEqual(rg["version"], 1)
        self.assertEqual(len(rg["groups"]), 1)
        g = rg["groups"][0]
        self.assertEqual(g["artifact_count"], 2)
        self.assertEqual({a["id"] for a in g["artifacts"]}, {id1, id2})
        self.assertEqual([n["name"] for n in g["common_nodes"]], ["运输车"])
        self.assertEqual(g["open_events"], 2)
        self.assertEqual(g["closed_events"], 0)

        # 显式 version=1
        st, rg1 = self.req("GET", f"/api/batches/{bid}/risk-groups?version=1")
        self.assertEqual(st, 200)
        self.assertEqual(len(rg1["groups"]), 1)

        # 版本快照内含分组
        st, snap = self.req("GET", f"/api/batches/{bid}/versions/1")
        self.assertEqual(len(snap["risk_groups"]), 1)

        # 关闭其中一个事件：只更新所属版本（当前版）状态计数，成员不变
        close_target = [e for e in v1_temp_events if e["artifact_id"] == id1][0]
        st, _ = self.req("POST", f"/api/events/{close_target['id']}/close",
                         {"note": "已处置", "evidence_no": "EV-X-1"})
        self.assertEqual(st, 200)
        st, rg_closed = self.req("GET", f"/api/batches/{bid}/risk-groups")
        g2 = rg_closed["groups"][0]
        self.assertEqual(g2["event_count"], 2)
        self.assertEqual(g2["open_events"], 1)
        self.assertEqual(g2["closed_events"], 1)

        # 补录/更正读数生成 v2：把文物甲运输车读数更正为正常值 → 共因消失，重算分组
        st, res2 = self.req("POST", f"/api/batches/{bid}/readings", {
            "note": "更正文物甲运输车读数",
            "readings": [r(id1, n2, "2026-10-01T08:10:00Z", 21),
                         r(id1, n2, "2026-10-01T08:20:00Z", 21)],
        })
        self.assertEqual(st, 201)
        self.assertEqual(res2["version"], 2)
        st, rg2 = self.req("GET", f"/api/batches/{bid}/risk-groups")
        self.assertEqual(rg2["version"], 2)
        self.assertEqual(rg2["groups"], [])

        # 旧版分组保持可查（含关闭后的计数）
        st, old = self.req("GET", f"/api/batches/{bid}/risk-groups?version=1")
        self.assertEqual(st, 200)
        self.assertEqual(len(old["groups"]), 1)
        self.assertEqual(old["groups"][0]["closed_events"], 1)

        # 错误：批次不存在 / 版本不存在（中文错误）
        st, err = self.req("GET", "/api/batches/9999/risk-groups")
        self.assertEqual(st, 404)
        self.assertIn("不存在", err["error"])
        st, err = self.req("GET", f"/api/batches/{bid}/risk-groups?version=99")
        self.assertEqual(st, 404)
        self.assertIn("版本 99 不存在", err["error"])
        st, err = self.req("GET", f"/api/batches/{bid}/versions/99/risk-groups")
        self.assertEqual(st, 404)
        self.assertIn("版本 99 不存在", err["error"])

        # 页面包含风险组区块
        for path in (f"/batches/{bid}", f"/batches/{bid}/versions/1"):
            url = f"http://127.0.0.1:{self.port}{path}"
            with urllib.request.urlopen(url) as resp:
                html = resp.read().decode()
            self.assertIn("跨文物共因风险组", html)


if __name__ == "__main__":
    unittest.main()
