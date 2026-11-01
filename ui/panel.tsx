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

const C_ON = "#5b7cfa"
const C_ON_SOFT = "#93a9fb"
const C_OFF = "#c3cad8"
const C_OFF_DEEP = "#8f9bb3"

export default function Panel(props: any) {
  const pushed = props.state || {}
  const api = props.api
  // 宿主的 dashboard 轮询**只在面板聚焦时跑**，切走就停 —— 所以面板自己按
  // 5 秒间隔调 presence_snapshot 拉同一个快照，切回来也不用等宿主那一拍。
  const [snap, setSnap] = useState<any>(null)

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
  // 年历热力图：一月一块月历，每块 7 列（周一..周日）× 最多 6 行。
  // 布局在渲染前一次算好，不用 IIFE —— 宿主的 hosted-tsx 编译器未必支持。
  const now = new Date()
  const todayKey = ymd(now)
  const yearGrid = buildYear(calendar, now)
  const yearLabel = now.getFullYear() + " 年"
  // 年度总览：只数「有记录且陪过」的天，和面板其它地方一样不与覆盖率混算。
  let yearDays = 0
  let yearTotal = 0
  for (const mo of yearGrid) {
    for (const row of mo.rows) {
      for (const c of row) {
        if (c && c.no_data !== true && c.sec > 0) {
          yearDays += 1
          yearTotal += c.sec
        }
      }
    }
  }
  const cur = st.current || {}
  const paused = st.paused === true

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
        <span className="np-title">她的一天</span>
        {paused ? <span className="np-pill">已暂停记录</span> : null}
        <span className="np-live">{stateWord(cur.state)}</span>
        <button className="np-btn" onClick={() => togglePause()}>
          {paused ? "恢复记录" : "暂停记录"}
        </button>
      </div>

      <div className="np-greet">{greet(num(today.first_seen))}</div>

      <div className="np-metrics">
        <div className="np-metric">
          <div className="np-metric-label">陪了你</div>
          <div className="np-metric-value">{dur(companion)}</div>
          <div className="np-metric-sub">{span > 0 ? `跨度 ${dur(span)}` : ""}</div>
        </div>
        <div className="np-metric">
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
                className="np-seg"
                title={m.kind === "on" ? "在场" : "离开"}
                style={{
                  left: `${left}%`,
                  width: `${Math.max(width, 0.12)}%`,
                  background: m.kind === "on" ? C_ON : C_OFF_DEEP,
                }}
              />
            )
          })}
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
        <div style={{ marginTop: 16 }}>
          <Card title="最近几天">
            {recent
              .slice()
              .reverse()
              .map((d: any) => (
                <div className="np-dayrow" key={String(d.date)}>
                  <span className="np-day-date">{shortDate(d.date)}</span>
                  <span className="np-day-bar">
                    <span
                      className="np-day-fill"
                      style={{
                        display: "block",
                        width: `${Math.max(0, Math.min(100, num(d.coverage) * 100))}%`,
                      }}
                    />
                  </span>
                  <span className="np-day-num">{dur(num(d.companion_sec))}</span>
                  <span className="np-day-num2">
                    {num(d.waiting_sec) >= 60 ? dur(num(d.waiting_sec)) : "—"}
                  </span>
                </div>
              ))}
          </Card>
        </div>
      ) : null}

      {yearGrid.length > 0 ? (
        <div className="np-block">
          <Card title={yearLabel}>
            {yearDays > 0 ? (
              <div className="np-cal-sub">
                今年有 {yearDays} 天记下了你的在场，累计 {dur(yearTotal)}
              </div>
            ) : null}
            <div className="np-cal">
              {yearGrid.map((mo: any) => (
                <div className="np-cal-month" key={mo.month}>
                  <div className="np-cal-title">{mo.month} 月</div>
                  <div className="np-cal-row np-cal-dow">
                    {["一", "二", "三", "四", "五", "六", "日"].map(
                      (w: string, wi: number) => (
                        <span key={wi}>{w}</span>
                      )
                    )}
                  </div>
                  {mo.rows.map((row: any[], ri: number) => (
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
                                ? "那时还没开始记录"
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
              ))}
            </div>
            <div className="np-heat-legend">
              <span className="np-heat-lbl">没有记录</span>
              <span
                className="np-heat-cell"
                style={{ background: "#f6f8fb", boxShadow: "inset 0 0 0 1px #dde3ee" }}
              />
              <span className="np-heat-lbl">少</span>
              <span className="np-heat-cell" style={{ background: heatColor(1800) }} />
              <span className="np-heat-cell" style={{ background: heatColor(7200) }} />
              <span className="np-heat-cell" style={{ background: heatColor(14400) }} />
              <span className="np-heat-cell" style={{ background: heatColor(25200) }} />
              <span className="np-heat-lbl">多</span>
            </div>
            <div className="np-note">每一格是一天，数字是日期；颜色越深，她陪你越久。</div>
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

// 年历热力图：一个月一块月历，列 = 周一..周日，行 = 周。
// 每块返回 rows（7 个一组），渲染时按行铺 —— 不用 flex-wrap，
// 因为 7 格 × 18px 正好贴住容器宽度，浮点误差会让第 7 格掉到下一行。
// 未来日期给 null（透明占位），和「还没开始记录」严格分开。
// 注意 hosted-tsx 不支持 SVG，这里全靠 div + CSS，别改回 <svg>。
function buildYear(cal: any[], now: Date): any[] {
  const map: Record<string, any> = {}
  for (const row of cal) map[String(row.d)] = row

  const year = now.getFullYear()
  const today = new Date(year, now.getMonth(), now.getDate())
  const out: any[] = []

  for (let m = 0; m < 12; m++) {
    if (m > now.getMonth()) break // 今年还没到的月份不画
    const first = new Date(year, m, 1)
    const days = new Date(year, m + 1, 0).getDate() // 当月天数
    const lead = (first.getDay() + 6) % 7 // 周一开头，前面空几格

    const cells: any[] = []
    for (let i = 0; i < lead; i++) cells.push(null)
    for (let d = 1; d <= days; d++) {
      const key = year + "-" + pad2(m + 1) + "-" + pad2(d)
      if (new Date(year, m, d).getTime() > today.getTime()) {
        cells.push(null)
        continue
      }
      const rec = map[key]
      // calendar 里没有这一天 = 那天插件还没开始记录，不是「陪着但 0 分钟」。
      // 两者语义不同，视觉上也压淡，否则前期一大片灰会把真数据淹没。
      cells.push(
        rec
          ? { date: key, day: d, sec: num(rec.s), no_data: false }
          : { date: key, day: d, sec: 0, no_data: true }
      )
    }
    while (cells.length % 7 !== 0) cells.push(null) // 补齐整周

    const rows: any[][] = []
    for (let i = 0; i < cells.length; i += 7) rows.push(cells.slice(i, i + 7))
    out.push({ month: m + 1, rows: rows })
  }
  return out
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
