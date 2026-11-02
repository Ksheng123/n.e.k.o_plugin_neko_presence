// 她的一天 · 面板（hosted-tsx，单文件组件 + 一个 CSS 注入）
//
// hosted-tsx 约束（踩过坑，别改）：
//  · 不支持 SVG —— 时间轴用 div + left/width 百分比定位，绝不用 <svg>/<rect>
//  · export 必须排在任何 JSX 闭合标签之前；辅助组件放文件尾部靠函数声明提升
//  · export const 的类型注解不能含顶层逗号（泛型用 type 别名绕开）
//  · **布局一律用 className + 注入 CSS，不用内联 style** ——
//    宿主运行时对内联样式的 `gap` / `flex: 1` 简写支持不完整，
//    实测会导致图例文字粘连、进度条宽度塌成 0（2026-10-29 真机踩到）
//
// 口径纪律（DESIGN.md §7.1，三本账）：
//  · 「陪了你」用 duration 账，「你安静过」用 peak_w 账，**分两行，绝不相加**
//  · 「没有记录」是第三种状态（浅灰底），不能画成「离开」—— 把关机画成离开就是编造
//  · 覆盖率 = covered / span，< 1 时界面要主动解释自己
import { Card } from "@neko/plugin-ui"
import { useEffect, useState } from "@neko/plugin-ui"
import { PANEL_CSS } from "./styles"

// 色块改用 CSS 类（.np-seg-on / .np-seg-off）画渐变，这里只留图例小方块要用的两个
const C_ON = "#5b7cfa"
const C_OFF_DEEP = "#8f9bb3"

export default function Panel(props: any) {
  const pushed = props.state || {}
  const api = props.api
  // 宿主的 dashboard 轮询**只在面板聚焦时跑**，切走就停 —— 所以面板自己按
  // 5 秒间隔调 presence_snapshot 拉同一个快照，切回来也不用等宿主那一拍。
  const [snap, setSnap] = useState<any>(null)
  // 年历按月翻页：偏移量相对「本月」，0 = 本月，-1 = 上月。
  // 用偏移而不是绝对月份，这样跨月/跨年时「本月」永远跟着系统时钟走。
  const [monthOffset, setMonthOffset] = useState(0)

  useEffect(() => {
    let alive = true
    let timer: any = null

    async function pull() {
      if (!api || typeof api.call !== "function") return
      try {
        const raw = await api.call("presence_snapshot", {})
        const data = unwrap(raw)
        // 只认带 today 的载荷，别把错误信封当成数据渲染成空白
        if (alive && data && data.today) setSnap(data)
      } catch (e) {
        // 拉失败（宿主重启、插件重载）就等下一轮，不在界面上报错
      }
    }

    pull()
    timer = setInterval(pull, 5000)

    // 切回窗口立刻补一拍，否则要等下一轮 5 秒
    const wake = () => {
      if (!document.hidden) pull()
    }
    window.addEventListener("focus", wake)
    document.addEventListener("visibilitychange", wake)

    return () => {
      alive = false
      if (timer) clearInterval(timer)
      window.removeEventListener("focus", wake)
      document.removeEventListener("visibilitychange", wake)
    }
  }, [])

  const st = snap || pushed
  const today = st.today || {}
  const timeline: any[] = Array.isArray(st.timeline) ? st.timeline : []
  const recent: any[] = Array.isArray(st.recent) ? st.recent : []
  const calendar: any[] = Array.isArray(st.calendar) ? st.calendar : []
  const now = new Date()
  const todayKey = ymd(now)
  // 左边界 = 初识月（后端 since），右边界 = 本月，翻不出去。
  // 初识日不是本插件第一条记录：6-19 就认识了，10-25 才开始数。
  const sinceStr =
    typeof st.since === "string" && st.since.length >= 7 ? st.since : todayKey
  const curIdx = now.getFullYear() * 12 + now.getMonth()
  const minIdx =
    Number(sinceStr.slice(0, 4)) * 12 + (Number(sinceStr.slice(5, 7)) - 1)
  let shownIdx = curIdx + monthOffset
  if (shownIdx > curIdx) shownIdx = curIdx
  if (shownIdx < minIdx) shownIdx = minIdx
  const shownY = Math.floor(shownIdx / 12)
  const shownM = shownIdx % 12
  const atMin = shownIdx <= minIdx
  const atMax = shownIdx >= curIdx
  // 单月月历在渲染前算好，不用 IIFE —— 宿主的 hosted-tsx 编译器未必支持。
  const monthView = buildMonth(calendar, shownY, shownM, now)
  const cur = st.current || {}
  const paused = st.paused === true
  // 在场的两种状态点亮状态灯；离开、锁屏、短暂离席、没有记录都不亮
  const onNow = cur.state === "interactive" || cur.state === "fullscreen"
  // 「现在」在 24 小时轴上的位置，画一根竖线，否则看不出色带哪头是"此刻"
  const nowPct =
    ((now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds()) / 86400) * 100
  // 「最近几天」画折线，不再画覆盖率条形 —— 条长画覆盖率、数字写陪伴时长，
  // 两者对不上，会被读成「陪得久不久」。覆盖率只留在 tooltip 里。
  // recent 已经是「旧 → 新」（列表展示时才 reverse 成最新在上），
  // 折线要时间从左往右走，直接用原序，别再 reverse。
  const series = recent.slice()
  const nPt = series.length
  let vmax = 1
  let sumC = 0
  let sumW = 0
  for (let i = 0; i < nPt; i++) {
    const v = num(series[i].companion_sec)
    if (v > vmax) vmax = v
    sumC += v
    sumW += num(series[i].waiting_sec)
  }
  let topIdx = 0
  for (let i = 0; i < nPt; i++) {
    if (num(series[i].companion_sec) >= num(series[topIdx].companion_sec)) topIdx = i
  }
  const pts = series.map((d: any, i: number) => ({
    x: nPt > 1 ? (i / (nPt - 1)) * 100 : 50,
    y: 93 - (num(d.companion_sec) / vmax) * 86,
  }))
  // 面积多边形：从左上开始依次过每个点，再回到底边两角闭合
  const areaPoly =
    pts.map((p: any) => `${p.x.toFixed(2)}% ${p.y.toFixed(2)}%`).join(",") +
    ",100% 100%,0% 100%"

  async function togglePause() {
    if (!api || typeof api.call !== "function") return
    const next = !paused
    try {
      await api.call("presence_pause", { paused: next })
      // 乐观更新：按钮立刻变字，正式数据由 5 秒轮询和 refresh_context 兜底
      if (snap) setSnap({ ...snap, paused: next })
    } catch (e) {
      // 调用失败等下一轮轮询纠正，界面不弹错误
    }
  }

  const companion = num(today.companion_sec)
  const waitTotal = num(today.waiting_total_sec)
  const waitCount = num(today.waiting_count)
  const longest = num(today.longest_wait_sec)
  const coverage = num(today.coverage)
  const span = num(today.span_sec)

  return (
    <div className="np-root">
      <style>{PANEL_CSS}</style>

      <div className="np-head">
        <div className="np-title">
          她的一天
          {paused ? <span className="np-pill">已暂停记录</span> : null}
        </div>
        <div className="np-greet">{greet(num(today.first_seen))}</div>
        <div className="np-bar">
          <span className="np-live">
            <span className={"np-dot" + (onNow ? " np-dot-on" : "")} />
            {stateWord(cur.state)}
          </span>
          <button className="np-btn" onClick={() => togglePause()}>
            {paused ? "恢复记录" : "暂停记录"}
          </button>
        </div>
      </div>

      <div className="np-metrics">
        <div className="np-metric">
          <div className="np-metric-label">陪了你</div>
          <div className="np-metric-value">{dur(companion)}</div>
          <div className="np-metric-sub">{span > 0 ? `跨度 ${dur(span)}` : ""}</div>
        </div>
        <div className="np-metric np-metric-quiet">
          <div className="np-metric-label">你安静过</div>
          <div className="np-metric-value">{waitCount > 0 ? `${waitCount} 次` : "—"}</div>
          <div className="np-metric-sub">{waitCount > 0 ? `最长 ${dur(longest)}` : ""}</div>
        </div>
      </div>

      <Card title="今天">
        <div className="np-track">
          {mergeSegs(timeline).map((m: any, i: number) => {
            const left = (m.o / 86400) * 100
            const width = (m.len / 86400) * 100
            return (
              <div
                key={i}
                className={"np-seg " + (m.kind === "on" ? "np-seg-on" : "np-seg-off")}
                title={m.kind === "on" ? "在场" : "离开"}
                style={{
                  left: `${left}%`,
                  width: `${Math.max(width, 0.12)}%`,
                }}
              />
            )
          })}
          <div className="np-now" style={{ left: `${nowPct}%` }}>
            <span className="np-now-top" />
          </div>
        </div>
        <div className="np-axis">
          <span>00</span>
          <span>06</span>
          <span>12</span>
          <span>18</span>
          <span>24</span>
        </div>
        <div className="np-legend">
          <span className="np-legend-item">
            <span className="np-legend-dot" style={{ background: C_ON }} />
            在场
          </span>
          <span className="np-legend-item">
            <span className="np-legend-dot" style={{ background: C_OFF_DEEP }} />
            离开
          </span>
          <span className="np-legend-item">
            <span className="np-legend-dot" style={{ background: "#e9edf4" }} />
            没有记录
          </span>
        </div>
        <div className="np-note">{coverageNote(span, coverage, waitTotal, timeline.length)}</div>
      </Card>

      {recent.length > 0 ? (
        <div className="np-block">
          <Card title="最近几天">
            <div className="np-chart">
              <div className="np-area" style={{ clipPath: `polygon(${areaPoly})` }} />
              {pts.map((p: any, i: number) => (
                <span
                  key={i}
                  className={"np-pt" + (i === topIdx ? " np-pt-top" : "")}
                  style={{ left: `${p.x}%`, top: `${p.y}%` }}
                  title={`${series[i].date} · 陪了 ${dur(
                    num(series[i].companion_sec)
                  )} · 记录完整度 ${Math.round(num(series[i].coverage) * 100)}%`}
                />
              ))}
            </div>
            <div className="np-xaxis">
              {series.map((d: any, i: number) => (
                <span key={i}>{shortDate(d.date)}</span>
              ))}
            </div>
            <div className="np-chart-sum">
              最近 {nPt} 天，她陪了你 {dur(sumC)}
              {sumW >= 60 ? ` · 累计安静 ${dur(sumW)}` : ""}
            </div>
          </Card>
        </div>
      ) : null}

      {calendar.length > 0 ? (
        <div className="np-block">
          <Card title="这些天">
            <div className="np-cal-nav">
              <button
                className={"np-cal-btn" + (atMin ? " np-cal-btn-off" : "")}
                onClick={() => {
                  if (!atMin) setMonthOffset(monthOffset - 1)
                }}
              >
                ‹
              </button>
              <span className="np-cal-navlbl">
                {shownY} 年 {shownM + 1} 月
              </span>
              <button
                className={"np-cal-btn" + (atMax ? " np-cal-btn-off" : "")}
                onClick={() => {
                  if (!atMax) setMonthOffset(monthOffset + 1)
                }}
              >
                ›
              </button>
            </div>
            <div className="np-cal-sub">
              {monthView.days > 0
                ? `这个月有 ${monthView.days} 天记下了你在，累计 ${dur(monthView.total)}`
                : "这个月还没有记录"}
            </div>
            <div className="np-cal">
              <div className="np-cal-month">
                <div className="np-cal-row np-cal-dow">
                  {["一", "二", "三", "四", "五", "六", "日"].map(
                    (w: string, wi: number) => (
                      <span key={wi}>{w}</span>
                    )
                  )}
                </div>
                {monthView.rows.map((row: any[], ri: number) => (
                  <div className="np-cal-row" key={ri}>
                    {row.map((d: any, ci: number) =>
                      d ? (
                        <div
                          className={
                            "np-cal-cell" +
                            (d.date === todayKey ? " np-cal-today" : "") +
                            (d.no_data ? " np-cal-nodata" : "")
                          }
                          key={ci}
                          title={`${d.date} · ${
                            d.no_data
                              ? "那时她还没开始数"
                              : d.sec > 0
                              ? dur(d.sec)
                              : "没有记录"
                          }`}
                          style={{
                            background: d.no_data ? "#f6f8fb" : heatColor(d.sec),
                            color: d.no_data ? "#bfc7d5" : heatText(d.sec),
                          }}
                        >
                          {d.day}
                        </div>
                      ) : (
                        <div className="np-cal-cell np-cal-void" key={ci} />
                      )
                    )}
                  </div>
                ))}
              </div>
            </div>
            <div className="np-heat-legend">
              <span className="np-heat-lbl">没有记录</span>
              <span
                className="np-heat-cell"
                style={{ background: "#eef1f6", boxShadow: "inset 0 0 0 1px #d9e0ec" }}
              />
              <span className="np-heat-lbl">少</span>
              <span className="np-heat-cell" style={{ background: heatColor(1800) }} />
              <span className="np-heat-cell" style={{ background: heatColor(7200) }} />
              <span className="np-heat-cell" style={{ background: heatColor(14400) }} />
              <span className="np-heat-cell" style={{ background: heatColor(25200) }} />
              <span className="np-heat-lbl">多</span>
            </div>
            <div className="np-note">
              每一格是一天，数字是日期；颜色越深，她陪你越久。
            </div>
          </Card>
        </div>
      ) : null}

      <div className="np-foot">
        「安静」指这段时间没有键盘或鼠标操作，不是断定你不在。
        没有记录的时段（关机、睡眠、插件没运行）单独标灰，不计入任何时长。
      </div>
    </div>
  )
}

// api.call 的返回值是宿主信封 {plugin_id, action_id, result: <入口载荷>}，
// 有些路径还会再套一层 Ok 的 {ok, value}。两种都拆，拿到真正的快照对象。
function unwrap(payload: any): any {
  let p = payload
  if (p && typeof p === "object" && "result" in p && "action_id" in p) p = p.result
  if (p && typeof p === "object" && "ok" in p && "value" in p) p = p.value
  return p
}

function num(v: any): number {
  const n = typeof v === "number" ? v : parseFloat(String(v))
  return Number.isFinite(n) ? n : 0
}

function dur(sec: number): string {
  const s = Math.max(0, Math.round(sec))
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} 分钟`
  const h = Math.floor(m / 60)
  const mm = m % 60
  return mm > 0 ? `${h} 小时 ${mm} 分` : `${h} 小时`
}

function clock(ts: number): string {
  if (!ts) return "—"
  const d = new Date(ts * 1000)
  const hh = String(d.getHours()).padStart(2, "0")
  const mi = String(d.getMinutes()).padStart(2, "0")
  return `${hh}:${mi}`
}

function greet(firstSeen: number): string {
  if (!firstSeen) return "今天还没有记录"
  return `她今天 ${clock(firstSeen)} 见到你`
}

function shortDate(date: string): string {
  return String(date || "").slice(5)
}

function stateWord(s: string): string {
  const map: Record<string, string> = {
    interactive: "你在操作",
    fullscreen: "全屏在看",
    away_fullscreen: "画面挂着",
    short_break: "短暂离席",
    away: "你离开了",
    locked: "锁屏",
    // 适配层拿不到信号（非 Windows 且宿主快照不可用等）：
    // 这不是一种「离开」，界面上只能诚实说没有记录
    unknown: "没有记录",
  }
  return map[String(s || "")] || ""
}

// 时间轴只分两类：在场（操作 + 全屏）与离开（短暂离席 + 静默 + 锁屏）。
// 真实数据里 `interactive` 与 `fullscreen` 会 30 秒一交替，
// 逐段画出来是斑马纹 —— 相邻同类合并，并把 60 秒内的缝当作连续
// （那段缝通常是采样边界，不是真的走了）。
function kindOf(s: string): string {
  const st = String(s || "")
  return st === "interactive" || st === "fullscreen" ? "on" : "off"
}

function mergeSegs(segs: any[]): any[] {
  const out: any[] = []
  for (let i = 0; i < segs.length; i++) {
    const s = segs[i]
    const len = num(s.len)
    if (len <= 0) continue
    const start = num(s.o)
    const kind = kindOf(s.s)
    if (out.length > 0) {
      const last = out[out.length - 1]
      if (last.kind === kind && start <= last.o + last.len + 60) {
        const end = Math.max(last.o + last.len, start + len)
        last.len = end - last.o
        continue
      }
    }
    out.push({ kind, o: start, len })
  }
  return out
}

function coverageNote(span: number, coverage: number, wait: number, segs: number): string {
  if (span <= 0) return "今天还没有记录。"
  const pct = Math.round(Math.max(0, Math.min(1, coverage)) * 100)
  const parts: string[] = []
  if (segs <= 0) {
    parts.push("时间轴从本次更新开始记录，之前的时段没有分段数据")
  }
  parts.push(
    pct >= 95 ? `记录完整（跨度 ${dur(span)}）` : `有 ${pct}% 的时间在记录（跨度 ${dur(span)}）`
  )
  if (pct < 95) parts.push(`${dur(span * (1 - coverage))} 没有记录`)
  if (wait > 0) parts.push(`安静累计 ${dur(wait)}`)
  return parts.join(" · ")
}

// 单月月历：列 = 周一..周日，行 = 周，每行 7 格单独一个 div。
// 不用 flex-wrap —— 7 格 × 18px 正好贴住容器宽度，浮点误差会让第 7 格掉到下一行。
// 未来日期给 null（透明占位），和「那时她还没开始数」严格分开。
// 注意 hosted-tsx 不支持 SVG，这里全靠 div + CSS，别改回 <svg>。
function buildMonth(cal: any[], year: number, month0: number, now: Date): any {
  const map: Record<string, any> = {}
  for (const row of cal) map[String(row.d)] = row

  const days = new Date(year, month0 + 1, 0).getDate() // 当月天数
  const lead = (new Date(year, month0, 1).getDay() + 6) % 7 // 周一开头，前面空几格
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())

  const cells: any[] = []
  for (let i = 0; i < lead; i++) cells.push(null)
  let days_with = 0
  let total = 0
  for (let d = 1; d <= days; d++) {
    const key = year + "-" + pad2(month0 + 1) + "-" + pad2(d)
    if (new Date(year, month0, d).getTime() > today.getTime()) {
      cells.push(null)
      continue
    }
    const rec = map[key]
    if (rec) {
      const sec = num(rec.s)
      days_with += 1
      total += sec
      cells.push({ date: key, day: d, sec: sec, no_data: false })
    } else {
      // calendar 里没有这一天 = 那天插件还没开始记录，不是「陪着但 0 分钟」。
      // 两者语义不同，视觉上也压淡，否则空月份会把真数据淹没。
      cells.push({ date: key, day: d, sec: 0, no_data: true })
    }
  }
  while (cells.length % 7 !== 0) cells.push(null) // 补齐整周

  const rows: any[][] = []
  for (let i = 0; i < cells.length; i += 7) rows.push(cells.slice(i, i + 7))
  return { rows: rows, days: days_with, total: total }
}

function pad2(n: number): string {
  return n < 10 ? "0" + n : String(n)
}

function ymd(d: Date): string {
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate())
}

// 格子里的日期数字：浅色底用深灰蓝，深色底必须转白，否则看不见。
function heatText(sec: number): string {
  if (sec <= 0) return "#a9b2c4"
  return sec / 3600 < 3 ? "#3b4a63" : "#ffffff"
}

// 色阶按「那天陪了多久」分四档。0 秒 = 最浅的灰，和「没有记录」共用一种视觉，
// 但 tooltip 会写清是哪一种。时间轴上这两种状态是分开的，这里不合并语义。
// 五档之间刻意拉开明度（225/203/160/120/62），保证小格子上也能一眼分级。
function heatColor(sec: number): string {
  if (sec <= 0) return "#eceff5"
  const h = sec / 3600
  if (h < 1) return "#c7d7fe"
  if (h < 3) return "#93b0fd"
  if (h < 6) return "#5b83f8"
  return "#2f56e0"
}
