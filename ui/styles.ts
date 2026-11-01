// 面板样式（CSS 字符串，由 panel.tsx 用 <style> 注入）
//
// 为什么不用内联 style：宿主的 hosted-tsx 运行时对内联样式对象支持不完整，
// 实测 `gap` 与 `flex: 1` 简写不生效 —— 图例文字粘连、进度条宽度塌成 0。
// CSS 文本没有这个问题（陪伴的面板全程用 className + 注入 CSS）。
export const PANEL_CSS = `
.np-root { padding: 16px 18px 26px; color: #2c3446; font-size: 13px; line-height: 1.5; }
.np-head { display: flex; align-items: center; margin-bottom: 6px; }
.np-title { font-size: 17px; font-weight: 600; letter-spacing: .5px; }
.np-pill { margin-left: 10px; font-size: 11px; color: #b26a00; background: #fff4e0;
  border: 1px solid #ffd9a0; border-radius: 999px; padding: 2px 8px; }
.np-live { margin-left: auto; font-size: 11px; color: #9aa4b6; }
.np-btn { font-size: 11px; color: #46536b; background: #eef2fa; border: 1px solid #d6e0f2;
  border-radius: 6px; padding: 3px 10px; cursor: pointer; margin-left: 8px; }
.np-btn:hover { background: #e2eafa; }
.np-btn:active { background: #d8e2f6; }
.np-greet { font-size: 12px; color: #7b869b; margin-bottom: 16px; }

.np-metrics { display: flex; margin: 0 -6px 16px; }
.np-metric { flex: 1 1 0; min-width: 0; margin: 0 6px; background: #f7f9fd;
  border: 1px solid #eaeff8; border-radius: 10px; padding: 12px 14px; }
.np-metric-label { font-size: 12px; color: #66718a; margin-bottom: 5px; }
.np-metric-value { font-size: 19px; font-weight: 600; color: #2c3446; }
.np-metric-sub { font-size: 10.5px; color: #9aa4b6; margin-top: 3px; min-height: 14px; }

.np-track { position: relative; height: 30px; background: #e9edf4;
  border-radius: 6px; overflow: hidden; }
.np-seg { position: absolute; top: 0; bottom: 0; }
.np-axis { display: flex; justify-content: space-between; font-size: 10px;
  color: #aab3c4; margin-top: 4px; }
.np-legend { display: flex; margin-top: 10px; font-size: 11px; color: #7b869b; }
.np-legend-item { display: flex; align-items: center; margin-right: 16px; }
.np-legend-dot { width: 9px; height: 9px; border-radius: 2px; margin-right: 5px; }
.np-note { margin-top: 10px; font-size: 11px; line-height: 1.75; color: #7b869b; }

.np-dayrow { display: flex; align-items: center; font-size: 12px; margin-bottom: 7px; }
.np-dayrow:last-child { margin-bottom: 0; }
.np-day-date { width: 48px; flex: 0 0 48px; color: #7b869b; }
.np-day-bar { flex: 1 1 0; min-width: 0; height: 8px; background: #e9edf4;
  border-radius: 4px; overflow: hidden; margin: 0 10px; }
.np-day-fill { height: 100%; background: #93a9fb; border-radius: 4px; }
.np-day-num { width: 84px; flex: 0 0 84px; text-align: right; }
.np-day-num2 { width: 76px; flex: 0 0 76px; text-align: right; color: #7b869b; }

.np-block { margin-top: 16px; }

/* 热力图：左侧星期栏 18px，每格 13px + 3px 间距 = 16px 步长，
   顶栏月份标签按列数算宽度，保证与格子左边缘严格对齐。 */
.np-heat-wrap { margin-top: 6px; }
.np-heat-head { display: flex; height: 13px; margin-bottom: 4px; }
.np-heat-gutter { width: 18px; flex: 0 0 18px; }
.np-heat-mlbl { font-size: 10px; color: #8b95a8; white-space: nowrap;
  overflow: hidden; line-height: 13px; }
.np-heat-main { display: flex; }
.np-heat-dow { width: 18px; flex: 0 0 18px; font-size: 9.5px; color: #a3acbd; }
.np-heat-dow span { display: block; height: 13px; margin-bottom: 3px;
  line-height: 13px; }
.np-heat { display: flex; }
.np-heat-col { display: flex; flex-direction: column; }
.np-heat-cell { width: 13px; height: 13px; margin: 0 3px 3px 0; border-radius: 3px;
  background: #eceff5; box-sizing: border-box; }
.np-heat-void { background: transparent; }
.np-heat-nodata { background: #f6f8fb; }
.np-heat-cell.np-heat-today { outline: 1.5px solid #2f56e0; outline-offset: 1px; }
.np-heat-cell:hover { box-shadow: 0 0 0 2px rgba(47, 86, 224, 0.22); }
.np-heat-legend { display: flex; align-items: center; margin-top: 6px;
  font-size: 10.5px; color: #9aa4b6; }
.np-heat-legend .np-heat-cell { margin: 0 3px 0 0; width: 12px; height: 12px; }
.np-heat-lbl { margin: 0 5px 0 6px; }

.np-foot { margin-top: 14px; font-size: 10.5px; color: #9aa4b6; line-height: 1.75; }
`
