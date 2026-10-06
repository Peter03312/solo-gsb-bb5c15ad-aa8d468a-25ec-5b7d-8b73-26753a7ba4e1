"""HTML 页面渲染（中文界面，仅标准库字符串拼接）。"""
from html import escape

CSS = """
*{box-sizing:border-box}
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#f5f6f8;color:#222}
header{background:#1f3a5f;color:#fff;padding:14px 24px;display:flex;align-items:baseline;gap:16px}
header a{color:#fff;text-decoration:none}
header .sub{font-size:13px;opacity:.8}
main{max-width:1150px;margin:20px auto;padding:0 16px}
.card{background:#fff;border:1px solid #e2e5ea;border-radius:8px;padding:16px;margin-bottom:16px}
table{border-collapse:collapse;width:100%;font-size:14px;background:#fff}
th,td{border:1px solid #e2e5ea;padding:6px 10px;text-align:left;vertical-align:top}
th{background:#f0f2f5;white-space:nowrap}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;white-space:nowrap}
.badge.open{background:#fde2e1;color:#c0392b}
.badge.closed{background:#e6f4ea;color:#1e7e34}
.badge.gap{background:#fff3cd;color:#8a6d3b}
.badge.ver{background:#e8eef7;color:#1f3a5f}
.summary{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px}
.summary .item{background:#fff;border:1px solid #e2e5ea;border-radius:8px;padding:12px 18px;min-width:130px}
.summary .num{font-size:24px;font-weight:600}
.summary .num.danger{color:#c0392b}
.summary .lbl{font-size:13px;color:#6b7280}
.alert{padding:10px 14px;border-radius:6px;margin-bottom:12px;font-size:14px}
.alert.err{background:#fde2e1;color:#a93226;border:1px solid #f5b7b1}
.alert.ok{background:#e6f4ea;color:#1e7e34;border:1px solid #a9dfbf}
input,select,textarea{padding:6px 8px;border:1px solid #ccd1d9;border-radius:4px;font-size:14px;margin:2px 4px 2px 0}
button{background:#1f3a5f;color:#fff;border:none;border-radius:4px;padding:7px 14px;cursor:pointer;font-size:14px}
button:hover{background:#2a4f7f}
form.inline{display:inline}
.muted{color:#6b7280;font-size:13px}
h2{font-size:18px;margin:0 0 12px}
h3{font-size:15px;margin:18px 0 8px}
a{color:#1f5fb4;text-decoration:none}
a:hover{text-decoration:underline}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.diff-add{background:#e6f4ea}
.diff-del{background:#fde2e1}
.diff-chg{background:#fff3cd}
"""


def page(title, body):
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · 借展运输环境监控</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <a href="/"><strong>博物馆借展运输环境监控</strong></a>
  <span class="sub">批次 · 节点 · 文物 · 温湿度风险事件 · 评估版本</span>
</header>
<main>{body}</main>
</body>
</html>"""


def _banner(msg, err):
    out = ""
    if err:
        out += f'<div class="alert err">✗ {escape(err)}</div>'
    if msg:
        out += f'<div class="alert ok">✓ {escape(msg)}</div>'
    return out


def _fmt_ts(ts):
    return escape(str(ts).replace("T", " ").replace("+00:00", " UTC"))


def _fmt_num(v):
    return f"{v:g}"


def _status_badge(status):
    if status == "closed":
        return '<span class="badge closed">已关闭</span>'
    return '<span class="badge open">开放中</span>'


def error_page(status, message):
    body = f"""
<div class="card">
  <h2>请求失败（{status}）</h2>
  <div class="alert err">{escape(message)}</div>
  <p><a href="javascript:history.back()">返回上一页</a> · <a href="/">回到首页</a></p>
</div>"""
    return page("请求失败", body)


# ---------------------------------------------------------------- 首页

def home(batches, msg=None, err=None):
    rows = ""
    for b in batches:
        ver = f'<span class="badge ver">v{b["current_version"]}</span>' if b["current_version"] else '<span class="muted">未评估</span>'
        rows += f"""<tr>
<td><a href="/batches/{b['batch_id']}">{escape(b['name'])}</a></td>
<td>{ver}</td>
<td>{b['readings']}</td>
<td><span class="badge open">{b['open_events']}</span></td>
<td><span class="badge closed">{b['closed_events']}</span></td>
<td>温度 {b['open_by_metric']['temperature']} / 湿度 {b['open_by_metric']['humidity']}</td>
<td><span class="badge gap">{b['gaps']}</span></td>
<td>{escape('、'.join(b['artifacts_with_open_events'])) or '<span class="muted">—</span>'}</td>
</tr>"""
    if not rows:
        rows = '<tr><td colspan="8" class="muted">暂无批次，请在下方创建。</td></tr>'
    body = _banner(msg, err) + f"""
<div class="card">
  <h2>批次风险摘要</h2>
  <table>
    <tr><th>批次</th><th>当前版本</th><th>读数</th><th>开放事件</th><th>已关闭事件</th>
        <th>开放事件（按指标）</th><th>缺测区间</th><th>涉险文物</th></tr>
    {rows}
  </table>
</div>
<div class="card">
  <h2>新建批次</h2>
  <form method="post" action="/batches">
    <input name="name" placeholder="批次名称，如：2026 春季书画巡展" required>
    <input name="description" placeholder="备注（可选）" size="40">
    <button type="submit">创建批次</button>
  </form>
</div>"""
    return page("批次列表", body)


# ---------------------------------------------------------------- 批次详情

def _event_rows(events, batch_id, closable):
    rows = ""
    for e in events:
        nodes = " → ".join(escape(n["name"]) for n in e["nodes"])
        action = ""
        if e["status"] == "open" and closable:
            action = f"""<form method="post" action="/events/{e['id']}/close">
<input type="hidden" name="next" value="/batches/{batch_id}">
<input name="note" placeholder="处置说明（必填）" required>
<input name="evidence_no" placeholder="证据编号（必填）" required>
<button type="submit">关闭事件</button></form>"""
        elif e["status"] == "closed":
            action = (f"处置：{escape(e['close_note'] or '')}<br>"
                      f"证据：{escape(e['close_evidence'] or '')}<br>"
                      f"<span class='muted'>{_fmt_ts(e['closed_at'])}</span>")
        rows += f"""<tr>
<td>{escape(e['artifact_name'])}</td>
<td>{escape(e['metric_label'])}</td>
<td>{_fmt_ts(e['start_ts'])}<br>至 {_fmt_ts(e['end_ts'])}</td>
<td>{e['reading_count']}</td>
<td>{_fmt_num(e['min_value'])} ~ {_fmt_num(e['max_value'])}</td>
<td>[{_fmt_num(e['limit_lo'])}, {_fmt_num(e['limit_hi'])}]</td>
<td>{_fmt_num(e['max_deviation'])}</td>
<td>{nodes}</td>
<td>{_status_badge(e['status'])}</td>
<td>{action}</td>
</tr>"""
    return rows or '<tr><td colspan="10" class="muted">无风险事件。</td></tr>'


def _risk_groups_card(groups, title, hint=None):
    if not groups:
        rows = '<tr><td colspan="7" class="muted">无跨文物共因风险组（需同一指标、时间闭区间相交且共享运输节点，并覆盖至少两件文物）。</td></tr>'
    else:
        rows = ""
        for g in groups:
            arts = "、".join(
                f'{escape(a["name"])}（#{a["id"]}）' for a in g["artifacts"])
            common = "、".join(escape(n["name"]) for n in g["common_nodes"])
            common = common or '<span class="muted">空（成员仅经传递相连，无全体共有节点）</span>'
            link_desc = "；".join(
                "事件 " + " — ".join(str(x) for x in lk["events"])
                + " 共享：" + "、".join(escape(n["name"]) for n in lk["shared_nodes"])
                for lk in g["links"]) or '<span class="muted">—</span>'
            rows += f"""<tr>
<td>{escape(g['group_id'])}</td>
<td>{escape(g['metric_label'])}</td>
<td>{_fmt_ts(g['start_ts'])}<br>至 {_fmt_ts(g['end_ts'])}</td>
<td>{arts}<br><span class="muted">{g['artifact_count']} 件文物 / {g['event_count']} 个事件</span></td>
<td><span class="badge open">{g['open_events']}</span> / <span class="badge closed">{g['closed_events']}</span></td>
<td>{common}</td>
<td>{link_desc}</td>
</tr>"""

    hint_html = f'<p class="muted">{escape(hint)}</p>' if hint else ""
    return f"""<div class="card">
  <h2>{escape(title)}</h2>
  {hint_html}
  <table>
    <tr><th>组号</th><th>指标</th><th>组起止时间</th><th>文物 / 事件</th>
        <th>开放 / 已关闭</th><th>全组共有节点</th><th>连边（传递路径，不代表每对成员都有共同节点）</th></tr>
    {rows}
  </table>
</div>"""


def batch_detail(batch, nodes, artifacts, summary, events, gaps, risk_groups, versions,
                 msg=None, err=None):
    node_rows = "".join(
        f"<tr><td>{n['seq']}</td><td>{escape(n['name'])}</td><td>{escape(n['location'])}</td></tr>"
        for n in nodes
    ) or '<tr><td colspan="3" class="muted">尚未定义运输节点。</td></tr>'

    art_rows = "".join(
        f"""<tr><td>{a['id']}</td><td>{escape(a['name'])}</td>
<td>{_fmt_num(a['temp_min'])} ~ {_fmt_num(a['temp_max'])} ℃</td>
<td>{_fmt_num(a['hum_min'])} ~ {_fmt_num(a['hum_max'])} %RH</td>
<td>{a['sample_interval']} 秒</td></tr>"""
        for a in artifacts
    ) or '<tr><td colspan="5" class="muted">尚未登记文物。</td></tr>'

    art_options = "".join(f'<option value="{a["id"]}">{escape(a["name"])}</option>' for a in artifacts)
    node_options = "".join(f'<option value="{n["id"]}">#{n["seq"]} {escape(n["name"])}</option>' for n in nodes)

    gap_rows = "".join(
        f"""<tr><td>{escape(g['artifact_name'])}</td><td>{_fmt_ts(g['start_ts'])} 至 {_fmt_ts(g['end_ts'])}</td>
<td>{g['expected_interval']} 秒</td><td>{_fmt_num(g['actual_interval'])} 秒</td></tr>"""
        for g in gaps
    ) or '<tr><td colspan="4" class="muted">无缺测区间。</td></tr>'

    ver_rows = ""
    for v in versions:
        diff_link = ""
        if v["version_no"] > 1:
            diff_link = (f' · <a href="/batches/{batch["id"]}/diff?from={v["version_no"] - 1}'
                         f'&to={v["version_no"]}">与上一版对比</a>')
        ver_rows += f"""<tr><td>v{v['version_no']}</td><td>{_fmt_ts(v['created_at'])}</td>
<td>{escape(v['note']) or '<span class="muted">—</span>'}</td>
<td><a href="/batches/{batch['id']}/versions/{v['version_no']}">查看快照</a>{diff_link}</td></tr>"""
    if not ver_rows:
        ver_rows = '<tr><td colspan="4" class="muted">尚无评估版本，提交读数后自动生成。</td></tr>'

    cur_ver = summary["current_version"]
    event_title = f"当前版本（v{cur_ver}）风险事件" if cur_ver else "风险事件（尚无版本）"

    body = _banner(msg, err) + f"""
<p><a href="/">← 返回批次列表</a></p>
<div class="card">
  <h2>批次：{escape(batch['name'])}</h2>
  <p class="muted">{escape(batch['description'])}</p>
  <div class="summary">
    <div class="item"><div class="num">{cur_ver if cur_ver else '—'}</div><div class="lbl">当前评估版本</div></div>
    <div class="item"><div class="num danger">{summary['open_events']}</div><div class="lbl">开放风险事件</div></div>
    <div class="item"><div class="num">{summary['closed_events']}</div><div class="lbl">已关闭事件</div></div>
    <div class="item"><div class="num">{summary['open_by_metric']['temperature']} / {summary['open_by_metric']['humidity']}</div><div class="lbl">开放事件（温度/湿度）</div></div>
    <div class="item"><div class="num">{summary['gaps']}</div><div class="lbl">缺测区间</div></div>
    <div class="item"><div class="num">{summary['readings']}</div><div class="lbl">当前版本读数</div></div>
  </div>
  <p class="muted">涉险文物：{escape('、'.join(summary['artifacts_with_open_events'])) or '无'}</p>
</div>

<div class="grid">
<div class="card">
  <h2>运输节点（按顺序）</h2>
  <table><tr><th>序号</th><th>名称</th><th>地点</th></tr>{node_rows}</table>
  <h3>添加节点</h3>
  <form method="post" action="/batches/{batch['id']}/nodes">
    <input name="seq" placeholder="序号" size="5" required>
    <input name="name" placeholder="节点名称" required>
    <input name="location" placeholder="地点（可选）">
    <button type="submit">添加</button>
  </form>
</div>
<div class="card">
  <h2>文物与环境限值</h2>
  <table><tr><th>ID</th><th>名称</th><th>温度范围</th><th>湿度范围</th><th>采样间隔</th></tr>{art_rows}</table>
  <h3>登记文物</h3>
  <form method="post" action="/batches/{batch['id']}/artifacts">
    <input name="name" placeholder="文物名称" required><br>
    <input name="temp_min" placeholder="温度下限 ℃" size="9" required>
    <input name="temp_max" placeholder="温度上限 ℃" size="9" required><br>
    <input name="hum_min" placeholder="湿度下限 %RH" size="10" required>
    <input name="hum_max" placeholder="湿度上限 %RH" size="10" required><br>
    <input name="sample_interval" placeholder="采样间隔（秒）" size="10" required>
    <button type="submit">登记</button>
  </form>
</div>
</div>

<div class="card">
  <h2>录入读数（每次提交生成新评估版本）</h2>
  <form method="post" action="/batches/{batch['id']}/readings">
    <select name="artifact_id">{art_options}</select>
    <select name="node_id">{node_options}</select>
    <input type="datetime-local" name="ts" step="1" required>
    <input name="temperature" placeholder="温度 ℃" size="8" required>
    <input name="humidity" placeholder="湿度 %RH" size="8" required>
    <input name="note" placeholder="版本备注（可选）" size="16">
    <button type="submit">提交并评估</button>
  </form>
  <p class="muted">补录历史时刻或更正已有读数同样走此表单；更正指同一文物同一时间戳但数值不同。</p>
</div>

<div class="card">
  <h2>{event_title}</h2>
  <table>
    <tr><th>文物</th><th>指标</th><th>起止时间</th><th>读数</th><th>实测范围</th>
        <th>限值</th><th>最大偏离</th><th>涉及节点</th><th>状态</th><th>处置 / 结论</th></tr>
    {_event_rows(events, batch['id'], closable=True)}
  </table>
</div>

{_risk_groups_card(risk_groups, f"跨文物共因风险组（当前版本 v{cur_ver}）" if cur_ver else "跨文物共因风险组",
                   "同指标、起止闭区间相交且共享至少一个运输节点的事件连边，取连通分量；仅覆盖至少两件文物的分量成组。可调用 /api/batches/" + str(batch['id']) + "/risk-groups 查询，历史版本用 /risk-groups?version=n。")}

<div class="card">
  <h2>缺测区间（当前版本）</h2>
  <table><tr><th>文物</th><th>区间</th><th>约定采样间隔</th><th>实际间隔</th></tr>{gap_rows}</table>
</div>

<div class="card">
  <h2>评估版本</h2>
  <table><tr><th>版本</th><th>生成时间</th><th>备注</th><th>操作</th></tr>{ver_rows}</table>
</div>"""
    return page(f"批次 {batch['name']}", body)


# ---------------------------------------------------------------- 版本快照

def version_page(batch, detail):
    v = detail["version"]
    reading_rows = ""
    for r in detail["readings"]:
        mark = ' <span class="badge gap">更正</span>' if r["corrected_from"] else ""
        reading_rows += f"""<tr>
<td>{escape(r['artifact_name'])}</td><td>{escape(r['node_name'])}</td>
<td>{_fmt_ts(r['ts'])}</td><td>{_fmt_num(r['temperature'])}</td>
<td>{_fmt_num(r['humidity'])}</td><td>{mark}</td></tr>"""
    if not reading_rows:
        reading_rows = '<tr><td colspan="6" class="muted">该版本无读数。</td></tr>'

    gap_rows = "".join(
        f"""<tr><td>{escape(g['artifact_name'])}</td><td>{_fmt_ts(g['start_ts'])} 至 {_fmt_ts(g['end_ts'])}</td>
<td>{g['expected_interval']} 秒</td><td>{_fmt_num(g['actual_interval'])} 秒</td></tr>"""
        for g in detail["gaps"]
    ) or '<tr><td colspan="4" class="muted">无缺测区间。</td></tr>'

    body = f"""
<p><a href="/batches/{batch['id']}">← 返回批次</a></p>
<div class="card">
  <h2>批次「{escape(batch['name'])}」评估版本 v{v['version_no']}</h2>
  <p class="muted">生成时间：{_fmt_ts(v['created_at'])}　备注：{escape(v['note']) or '—'}</p>
  <p class="muted">历史版本为只读快照：读数、事件与关闭结论均保持当时状态。</p>
</div>
<div class="card">
  <h2>风险事件（含关闭结论）</h2>
  <table>
    <tr><th>文物</th><th>指标</th><th>起止时间</th><th>读数</th><th>实测范围</th>
        <th>限值</th><th>最大偏离</th><th>涉及节点</th><th>状态</th><th>结论</th></tr>
    {_event_rows(detail['events'], batch['id'], closable=False)}
  </table>
</div>
{_risk_groups_card(detail.get('risk_groups', []), f"跨文物共因风险组（v{v['version_no']} 快照）",
                   "按该版本事件快照计算，补录/更正读数生成新版后重算；旧版分组保持可查，关闭事件只更新组内状态计数，不改变成员。")}
<div class="card">
  <h2>缺测区间</h2>
  <table><tr><th>文物</th><th>区间</th><th>约定采样间隔</th><th>实际间隔</th></tr>{gap_rows}</table>
</div>
<div class="card">
  <h2>读数快照（{len(detail['readings'])} 条）</h2>
  <table><tr><th>文物</th><th>节点</th><th>时间</th><th>温度 ℃</th><th>湿度 %RH</th><th>标记</th></tr>
  {reading_rows}</table>
</div>"""
    return page(f"版本 v{v['version_no']}", body)


# ---------------------------------------------------------------- 版本差异

def _reading_line(r):
    return (f"文物 {r['artifact_name']} @ {_fmt_ts(r['ts'])}："
            f"温度 {_fmt_num(r['temperature'])} ℃，湿度 {_fmt_num(r['humidity'])} %RH"
            f"（节点 {escape(r['node_name'])}）")


def _event_line(e):
    return (f"{escape(e['artifact_name'])} · {escape(e['metric_label'])} · "
            f"{_fmt_ts(e['start_ts'])} 至 {_fmt_ts(e['end_ts'])} · "
            f"{e['reading_count']} 条读数 · 最大偏离 {_fmt_num(e['max_deviation'])} · "
            f"{_status_badge(e['status'])}")


def diff_page(batch, diff):
    rd = diff["readings"]
    ed = diff["events"]

    def rows(items, cls, render):
        if not items:
            return f'<tr><td class="muted">无</td></tr>'
        return "".join(f'<tr class="{cls}"><td>{render(i)}</td></tr>' for i in items)

    corrected_rows = ""
    for c in rd["corrected"]:
        corrected_rows += (f'<tr class="diff-chg"><td>{_reading_line(c["before"])}</td>'
                           f'<td>{_reading_line(c["after"])}</td></tr>')
    if not corrected_rows:
        corrected_rows = '<tr><td colspan="2" class="muted">无</td></tr>'

    changed_rows = ""
    for c in ed["changed"]:
        changed_rows += (f'<tr class="diff-chg"><td>{_event_line(c["before"])}</td>'
                         f'<td>{_event_line(c["after"])}</td></tr>')
    if not changed_rows:
        changed_rows = '<tr><td colspan="2" class="muted">无</td></tr>'

    closure_rows = ""
    for c in ed["closure_not_inherited"]:
        closure_rows += (f'<tr class="diff-del"><td>{_event_line(c["before"])}<br>'
                         f'<span class="muted">旧版结论：{escape(c["before"]["close_note"] or "")}'
                         f'（证据 {escape(c["before"]["close_evidence"] or "")}）</span></td>'
                         f'<td>{_event_line(c["after"])}<br>'
                         f'<span class="muted">新版未继承关闭状态，需重新处置</span></td></tr>')
    if not closure_rows:
        closure_rows = '<tr><td colspan="2" class="muted">无</td></tr>'

    body = f"""
<p><a href="/batches/{batch['id']}">← 返回批次</a></p>
<div class="card">
  <h2>批次「{escape(batch['name'])}」版本差异：v{diff['from_version']} → v{diff['to_version']}</h2>
</div>
<div class="card">
  <h2>读数差异</h2>
  <h3>新增读数（{len(rd['added'])}）</h3>
  <table>{rows(rd['added'], 'diff-add', _reading_line)}</table>
  <h3>更正读数（{len(rd['corrected'])}）</h3>
  <table><tr><th>旧值（v{diff['from_version']}）</th><th>新值（v{diff['to_version']}）</th></tr>
  {corrected_rows}</table>
  <h3>移除读数（{len(rd['removed'])}）</h3>
  <table>{rows(rd['removed'], 'diff-del', _reading_line)}</table>
</div>
<div class="card">
  <h2>事件差异</h2>
  <h3>新增事件（{len(ed['added'])}）</h3>
  <table>{rows(ed['added'], 'diff-add', _event_line)}</table>
  <h3>消失事件（{len(ed['removed'])}）</h3>
  <table>{rows(ed['removed'], 'diff-del', _event_line)}</table>
  <h3>变化事件（{len(ed['changed'])}）</h3>
  <table><tr><th>v{diff['from_version']}</th><th>v{diff['to_version']}</th></tr>{changed_rows}</table>
  <h3>关闭状态未继承（{len(ed['closure_not_inherited'])}）</h3>
  <table><tr><th>v{diff['from_version']}（已关闭）</th><th>v{diff['to_version']}（需重新处置）</th></tr>
  {closure_rows}</table>
</div>"""
    return page("版本差异", body)
