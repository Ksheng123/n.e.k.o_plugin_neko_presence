// 面板样式（CSS 字符串，由 panel.tsx 用 <style> 注入）
//
// 为什么不用内联 style：宿主的 hosted-tsx 运行时对内联样式对象支持不完整，
// 实测 `gap` 与 `flex: 1` 简写不生效 —— 图例文字粘连、进度条宽度塌成 0。
// CSS 文本没有这个问题（陪伴的面板全程用 className + 注入 CSS）。
//
// 视觉规范（2026-11-02 改版）：
//  主数字 27px/700，陪伴用蓝 #2f56e0、安静用琥珀 #b8761e —— 一眼分得清两本账；
//  卡片左侧用 inset 阴影做 3px 色条（不用 ::before，宿主注入的 CSS 不保证支持伪元素）；
//  时间轴 34px，加内阴影和"现在"指针，让人知道此刻在轴上哪个位置。
export const PANEL_CSS = `
.np-root { padding: 16px 18px 26px; color: #2b3244; font-size: 13px; line-height: 1.5; }
/* 页头：标题与副标题居中成一组，状态与按钮在下方分居两侧 ——
   原来四个元素挤在一行右对齐，标题被压到最左，重心是歪的 */
.np-head { text-align: center; margin-bottom: 17px; }
.np-title { font-size: 18px; font-weight: 700; letter-spacing: .3px; color: #232a3b; }
.np-pill { display: inline-block; margin-left: 8px; vertical-align: 2px;
  font-size: 11px; font-weight: 400; color: #b26a00; background: #fff4e0;
  border: 1px solid #ffd9a0; border-radius: 999px; padding: 2px 8px; }
.np-bar { display: flex; align-items: center; justify-content: space-between;
  margin-top: 10px; }
.np-live { display: flex; align-items: center; font-size: 11px; color: #8b95a8; }
/* 状态灯：在场亮绿，其余灰。只做静态，不做呼吸动画 —— 面板 5 秒刷一次，
   动画会被重渲染打断，反而像卡住 */
.np-dot { width: 6px; height: 6px; border-radius: 50%; margin-right: 5px;
  background: #cbd3e1; flex: 0 0 6px; }
.np-dot-on { background: #22b573; box-shadow: 0 0 0 3px rgba(34, 181, 115, .14); }
.np-btn { font-size: 11px; color: #46536b; background: #eef2fa; border: 1px solid #d6e0f2;
  border-radius: 6px; padding: 3px 10px; cursor: pointer; }
.np-btn:hover { background: #e2eafa; }
.np-btn:active { background: #d8e2f6; }
.np-greet { font-size: 12px; color: #7b869b; margin-top: 3px; }

/* 两本账：左边陪伴（蓝），右边安静（琥珀）。
   左侧色条用 inset 阴影而不是 border-left，圆角不会被切出直角 */
.np-metrics { display: flex; margin: 0 -6px 18px; }
.np-metric { flex: 1 1 0; min-width: 0; margin: 0 6px; position: relative;
  background: linear-gradient(180deg, #fbfcff 0%, #f4f8ff 100%);
  border: 1px solid #e9eefb; border-radius: 12px; padding: 12px 15px 13px;
  box-shadow: inset 3px 0 0 #5b7cfa; }
.np-metric-quiet { background: linear-gradient(180deg, #fffdf9 0%, #fdf7ee 100%);
  border-color: #f4ead9; box-shadow: inset 3px 0 0 #e0a44a; }
.np-metric-label { font-size: 11.5px; color: #6b7690; margin-bottom: 3px; letter-spacing: .2px; }
.np-metric-value { font-size: 27px; font-weight: 700; color: #2f56e0;
  letter-spacing: -.8px; line-height: 1.16; }
.np-metric-quiet .np-metric-value { color: #b8761e; }
.np-metric-sub { font-size: 10.5px; color: #98a2b6; margin-top: 4px; min-height: 14px; }

/* 24 小时时间轴 */
.np-track { position: relative; height: 34px; background: #e9edf4;
  border-radius: 8px; overflow: hidden; box-shadow: inset 0 1px 3px rgba(35, 42, 59, .07); }
.np-seg { position: absolute; top: 0; bottom: 0; margin-left: -.4px; }
.np-seg-on { background: linear-gradient(180deg, #7c97ff 0%, #5b7cfa 52%, #4a6ae8 100%); }
.np-seg-off { background: #aab5c9; }
/* 「现在」指针：竖线 + 顶端圆点，画在色带之上 */
.np-now { position: absolute; top: 0; bottom: 0; width: 2px; background: #232a3b;
  border-radius: 1px; box-shadow: 0 0 0 1.5px rgba(255, 255, 255, .85); }
.np-now-top { position: absolute; top: -1px; left: -2px; width: 6px; height: 6px;
  border-radius: 50%; background: #232a3b; box-shadow: 0 0 0 1.5px #fff; }
.np-axis { display: flex; justify-content: space-between; font-size: 10px;
  color: #aab3c4; margin-top: 5px; font-variant-numeric: tabular-nums; }
.np-legend { display: flex; margin-top: 11px; font-size: 11px; color: #7b869b; }
.np-legend-item { display: flex; align-items: center; margin-right: 16px; }
.np-legend-dot { width: 9px; height: 9px; border-radius: 3px; margin-right: 5px; }
.np-note { margin-top: 10px; font-size: 11px; line-height: 1.75; color: #7b869b; }

/* 最近几天：折线图。
   hosted-tsx 不支持 SVG，所以曲线不用 <path> —— 用 clip-path 裁一块面积，
   顶点坐标全是百分比（响应式，不需要知道容器像素宽），顶边就是折线。
   drop-shadow 跟着裁切形状走，给顶边补一道深色描边，看着才像"线"而不是"色块"。 */
.np-chart { position: relative; height: 132px; margin: 8px 2px 0; }
.np-area { position: absolute; left: 0; right: 0; top: 0; bottom: 0;
  background: linear-gradient(180deg, rgba(99, 102, 241, .72) 0%,
    rgba(91, 124, 250, .40) 30%, rgba(125, 153, 255, .16) 62%,
    rgba(125, 153, 255, 0) 96%);
  filter: drop-shadow(0 -2px 0 rgba(47, 86, 224, .70))
          drop-shadow(0 8px 14px rgba(91, 124, 250, .22)); }
.np-pt { position: absolute; width: 7px; height: 7px; margin: -3.5px 0 0 -3.5px;
  border-radius: 50%; background: #ffffff; border: 2px solid #5b7cfa;
  box-shadow: 0 1px 3px rgba(47, 86, 224, .35), 0 0 0 3px rgba(91, 124, 250, .16); }
.np-pt-top { width: 9px; height: 9px; margin: -4.5px 0 0 -4.5px;
  border-color: #ffffff; background: #2f56e0;
  box-shadow: 0 0 0 3px rgba(47, 86, 224, .28), 0 2px 6px rgba(47, 86, 224, .4); }
.np-xaxis { display: flex; justify-content: space-between; margin-top: 8px;
  font-size: 9.5px; color: #a3acbd; font-variant-numeric: tabular-nums; }
.np-chart-sum { margin-top: 9px; font-size: 11px; color: #7b869b; }

.np-block { margin-top: 18px; }

/* 年历热力图：一个月一块月历，列 = 周一..周日，行 = 周。
   行用独立 div 铺，不靠 flex-wrap，避免第 7 格因浮点误差换行。 */
.np-cal { display: flex; flex-wrap: wrap; margin-top: 2px; }
.np-cal-sub { font-size: 10.5px; color: #8b95a8; margin-bottom: 9px; }
/* 翻页条：按月翻，左右边界分别是初识月和本月 */
.np-cal-nav { display: flex; align-items: center; margin-bottom: 9px; }
.np-cal-btn { width: 22px; height: 22px; padding: 0; border-radius: 6px;
  border: 1px solid #e2e7f0; background: #ffffff; color: #5b6577;
  font-size: 14px; line-height: 20px; text-align: center; cursor: pointer; }
.np-cal-btn:hover { background: #f2f5fb; border-color: #cfd8e6; }
.np-cal-btn-off, .np-cal-btn-off:hover { color: #c6cedd; border-color: #eaeef5;
  background: #ffffff; cursor: default; }
.np-cal-navlbl { font-size: 12px; font-weight: 700; color: #3c465c;
  margin: 0 10px; min-width: 84px; text-align: center; }
.np-cal-month { width: 161px; margin: 0 10px 12px 0; }
.np-cal-title { font-size: 11px; font-weight: 600; color: #5b6577; margin-bottom: 5px; }
.np-cal-row { display: flex; }
.np-cal-dow { font-size: 8.5px; color: #a3acbd; margin-bottom: 2px; }
.np-cal-dow span { width: 23px; text-align: center; line-height: 13px; }
.np-cal-cell { width: 22px; height: 22px; margin: 0 1px 1px 0; border-radius: 5px;
  box-sizing: border-box; font-size: 10px; line-height: 22px; text-align: center;
  background: #f4f6fa; }
.np-cal-void { background: transparent; }
.np-cal-cell.np-cal-today { outline: 2px solid #2f56e0; outline-offset: 1px;
  font-weight: 700; }
.np-cal-cell:hover { box-shadow: 0 0 0 2px rgba(47, 86, 224, .22); }

.np-heat-legend { display: flex; align-items: center; margin-top: 3px;
  font-size: 10.5px; color: #9aa4b6; }
.np-heat-legend .np-heat-cell { width: 12px; height: 12px; margin: 0 3px 0 0;
  border-radius: 3px; }
.np-heat-lbl { margin: 0 5px 0 6px; }

.np-foot { margin-top: 16px; font-size: 10.5px; color: #9aa4b6; line-height: 1.75; }
`
