import { useEffect, useMemo, useState } from "react";
import { url, ENDPOINTS } from "../services/config";

/**
 * 「会话统计」页面（对比表 #52）。
 *
 * ★ 为什么是独立页面而不是主应用里的第三个视图：
 *   统计是"偶尔看一眼"的东西，塞进主视图要改 ViewSwitcher / ViewMode /
 *   App 的渲染分支，还要和 SSE 生命周期纠缠。独立页面零侵入，
 *   而且数据是拉一次就完事的，不需要常驻连接。
 *
 * ★ 为什么自己画 SVG 而不用图表库：
 *   项目没有图表依赖，为一张堆叠柱图引一个 100KB 的库不值当。
 *   这里的图就三件事：算高度、画矩形、写刻度。
 */

interface DayRow {
  date: string;
  tasks: number;
  done: number;
  failed: number;
  aborted: number;
  prompt_tokens: number;
  completion_tokens: number;
}

interface Totals {
  tasks: number;
  done: number;
  failed: number;
  aborted: number;
  success_rate: number;
  prompt_tokens: number;
  completion_tokens: number;
  reasoning_tokens: number;
  llm_calls: number;
  tool_calls: number;
  events: number;
  avg_turns: number;
  avg_duration_s: number;
  // ---- 墙钟分解（对比表 #52）----
  llm_ms?: number;
  tool_ms?: number;
  busy_ms?: number;
  wall_ms?: number;
  avg_llm_ms?: number;
  avg_tool_ms?: number;
  avg_first_token_ms?: number;
  llm_ratio?: number;
  tool_ratio?: number;
}

interface StatsData {
  range: { days: number; since: number };
  scope: string;
  totals: Totals;
  by_day: DayRow[];
}

const RANGES = [7, 30, 90] as const;

const COLOR = {
  done: "#4ade80",
  failed: "#f87272",
  aborted: "#8b9bb4",
};

/** 大数字缩写 */
function fmt(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

/** 秒 → "2分18秒" / "46秒" */
function fmtDur(s: number): string {
  if (!s) return "—";
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return m > 0 ? `${m}分${r}秒` : `${r}秒`;
}

/**
 * 把 by_day 补成**连续的**日期序列。
 *
 * ★ 为什么必须补：没有任务的那天后端不会返回行，直接画会把 11-15 和 11-18
 *   并排在一起，看起来像"每天都满的"，趋势就骗人了。
 */
function fillDays(rows: DayRow[], spanDays: number): DayRow[] {
  const map = new Map(rows.map((r) => [r.date, r]));
  const out: DayRow[] = [];
  const today = new Date();
  for (let i = spanDays - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(today.getDate() - i);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    out.push(
      map.get(key) ?? {
        date: key, tasks: 0, done: 0, failed: 0, aborted: 0,
        prompt_tokens: 0, completion_tokens: 0,
      },
    );
  }
  return out;
}

export function StatsPage() {
  const [days, setDays] = useState<number>(30);
  const [data, setData] = useState<StatsData | null>(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setErr("");
    fetch(url(`${ENDPOINTS.stats}?days=${days}`))
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((j) => {
        if (!alive) return;
        setData(j as StatsData);
      })
      .catch((e) => {
        if (alive) setErr(String(e?.message || e));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [days]);

  const series = useMemo(
    () => (data ? fillDays(data.by_day, data.range.days) : []),
    [data],
  );

  const t = data?.totals;

  return (
    <div className="st-page">
      <header className="st-header">
        <div>
          <h1 className="st-title">会话统计</h1>
          <p className="st-sub">
            {data ? `最近 ${data.range.days} 天 · ${data.scope === "all" ? "全部工作区" : data.scope}` : "加载中…"}
          </p>
        </div>
        <div className="st-actions">
          <div className="st-range">
            {RANGES.map((r) => (
              <button
                key={r}
                type="button"
                onClick={() => setDays(r)}
                className={`st-range__btn ${days === r ? "is-active" : ""}`}
              >
                {r} 天
              </button>
            ))}
          </div>
          <a className="st-back" href="/">← 返回</a>
        </div>
      </header>

      {err && (
        <div className="st-err">
          取不到统计数据：{err}
          <br />
          <span className="st-err__hint">确认后端在跑（/api/health），且库里有任务。</span>
        </div>
      )}

      {t && (
        <section className="st-cards">
          <Card label="任务数" value={String(t.tasks)} hint={`完成 ${t.done} / 失败 ${t.failed} / 中止 ${t.aborted}`} />
          <Card
            label="成功率"
            value={t.tasks ? `${Math.round(t.success_rate * 100)}%` : "—"}
            hint="完成数 ÷ 任务总数"
            tone={t.success_rate >= 0.5 ? "green" : "amber"}
          />
          <Card
            label="输入 tokens"
            value={fmt(t.prompt_tokens)}
            hint={`${t.llm_calls} 次模型调用`}
            tone="blue"
          />
          <Card
            label="输出 tokens"
            value={fmt(t.completion_tokens)}
            hint={t.reasoning_tokens > 0 ? `含思考 ${fmt(t.reasoning_tokens)}` : "不含思考 token"}
            tone="blue"
          />
          <Card label="平均轮数" value={t.avg_turns ? t.avg_turns.toFixed(1) : "—"} hint={`工具调用共 ${t.tool_calls} 次`} />
          <Card label="平均时长" value={fmtDur(t.avg_duration_s)} hint="从首个事件到末个事件" />
          <Card
            label="首 token"
            value={t.avg_first_token_ms ? `${(t.avg_first_token_ms / 1000).toFixed(1)}s` : "—"}
            hint="等模型吐第一个字的平均时间"
            tone="blue"
          />
        </section>
      )}

      {/* 时间构成：回答"慢在等模型还是跑工具" */}
      {t && (t.busy_ms ?? 0) > 0 && <TimeBreakdown t={t} />}

      {series.length > 0 && (
        <section className="st-chart">
          <div className="st-chart__head">
            <h2 className="st-chart__title">每日任务量</h2>
            <div className="st-legend">
              <Legend color={COLOR.done} label="完成" />
              <Legend color={COLOR.failed} label="失败" />
              <Legend color={COLOR.aborted} label="中止" />
            </div>
          </div>
          <DailyBars series={series} />
        </section>
      )}

      {loading && <p className="st-loading">读取中…</p>}
    </div>
  );
}

function Card({
  label, value, hint, tone,
}: {
  label: string; value: string; hint: string; tone?: "green" | "amber" | "blue";
}) {
  return (
    <div className={`st-card ${tone ? `st-card--${tone}` : ""}`}>
      <span className="st-card__label">{label}</span>
      <span className="st-card__value">{value}</span>
      <span className="st-card__hint">{hint}</span>
    </div>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span className="st-legend__item">
      <i style={{ background: color }} />
      {label}
    </span>
  );
}

/** 毫秒 → "12.3s" / "2分18秒" */
function fmtMs(ms: number): string {
  if (!ms) return "0 秒";
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} 秒`;
  const m = Math.floor(s / 60);
  return `${m} 分 ${Math.round(s % 60)} 秒`;
}

/**
 * 时间构成：模型 / 工具 / 其他。
 *
 * ★ 为什么分母是 wall_ms（任务墙钟总和）而不是 llm+tool：
 *   "其他"这部分（用户等待、调度、落盘）如果不显示，会让人以为
 *   "模型 72% + 工具 28% = 100%"，从而把优化注意力全放在两者上。
 *   实际往往还有一段可观的开销被忽略了。
 * ★ 但**判断该优化谁**要看 llm : tool 的比值（llm_ratio），
 *   墙钟分母里掺了等待，比值会被稀释。
 */
function TimeBreakdown({ t }: { t: Totals }) {
  const wall = Math.max(t.wall_ms ?? 0, (t.llm_ms ?? 0) + (t.tool_ms ?? 0), 1);
  const llm = Math.min(1, (t.llm_ms ?? 0) / wall);
  const tool = Math.min(Math.max(0, 1 - llm), (t.tool_ms ?? 0) / wall);
  const other = Math.max(0, 1 - llm - tool);

  const seg = (w: number, color: string, label: string) =>
    w > 0.001 ? (
      <div
        className="st-tb__seg"
        style={{ width: `${(w * 100).toFixed(2)}%`, background: color }}
        title={`${label} ${(w * 100).toFixed(0)}%`}
      />
    ) : null;

  const hint =
    (t.llm_ratio ?? 0) > 0.6
      ? "该减上下文或换模型"
      : (t.tool_ratio ?? 0) > 0.6
        ? "该优化命令、超时和多轮往返"
        : "大致对半，看具体是哪种任务在拖";

  return (
    <section className="st-chart">
      <div className="st-chart__head">
        <h2 className="st-chart__title">时间构成</h2>
        {/* ★ 口径必须写清：条形按"占墙钟"画，结论按"干活时间里谁占大头"给 ——
            两个分母不同，混着说会让人以为 88% 是工具占的。 */}
        <span className="st-tb__hint">
          干活时间（模型+工具）里：模型 {((t.llm_ratio ?? 0) * 100).toFixed(0)}% · 工具{" "}
          {((t.tool_ratio ?? 0) * 100).toFixed(0)}%　→　{hint}
        </span>
      </div>

      <div className="st-tb__bar">
        {seg(llm, "#5b9dff", "等模型")}
        {seg(tool, "#4ade80", "跑工具")}
        {seg(other, "#39445a", "其他")}
      </div>

      <div className="st-tb__legend">
        <span>
          <i style={{ background: "#5b9dff" }} /> 等模型 {fmtMs(t.llm_ms ?? 0)}
          <em>{((t.llm_ms ?? 0) / wall * 100).toFixed(0)}%</em>
        </span>
        <span>
          <i style={{ background: "#4ade80" }} /> 跑工具 {fmtMs(t.tool_ms ?? 0)}
          <em>{((t.tool_ms ?? 0) / wall * 100).toFixed(0)}%</em>
        </span>
        <span>
          <i style={{ background: "#39445a" }} /> 其他 {fmtMs(Math.max(0, wall - (t.llm_ms ?? 0) - (t.tool_ms ?? 0)))}
          <em>{(other * 100).toFixed(0)}%</em>
        </span>
      </div>
      <p className="st-tb__note">
        <b>其他</b> = 墙钟里不在「模型调用」和「工具执行」内的部分：调度、RAG 检索、
        上下文压缩、事件落盘、以及等待用户。它偏大说明瓶颈可能在框架本身，而不在模型或工具。
      </p>
      <p className="st-tb__note">
        单次模型调用平均 {fmtMs(t.avg_llm_ms ?? 0)}，单次工具平均 {fmtMs(t.avg_tool_ms ?? 0)}；
        首 token 平均{" "}
        {t.avg_first_token_ms ? fmtMs(t.avg_first_token_ms) : "无数据（v6 之前跑的任务没有这个字段）"}。
      </p>
      <p className="st-tb__note st-tb__note--warn">
        这是**所有任务混合**后的口径。实测不同任务类型的瓶颈完全相反（纯对话任务模型占 97%，
        桌面任务工具占大头），混在一起看只能知道总体倾向；要判断某类任务该优化什么，
        得看单任务：<code>/api/tasks/&lt;id&gt;/projection</code>。
      </p>
    </section>
  );
}

/**
 * 堆叠柱状图。
 *
 * ★ 用 viewBox + width:100%，让 SVG 自己缩放 —— 不用监听窗口 resize 重算。
 * ★ 每根柱内部从下往上堆：完成 → 失败 → 中止。顺序固定，
 *   否则不同日期的同色段高度不可比。
 */
function DailyBars({ series }: { series: DayRow[] }) {
  const W = 900;
  const H = 240;
  const PAD_L = 44;
  const PAD_B = 26;
  const PAD_T = 12;
  const plotW = W - PAD_L - 12;
  const plotH = H - PAD_T - PAD_B;

  const max = Math.max(1, ...series.map((d) => d.tasks));
  const step = plotW / series.length;
  const barW = Math.max(2, step * 0.62);

  // y 轴 3 条刻度：0 / 一半 / 最大
  const ticks = [0, Math.ceil(max / 2), max];

  // x 轴只标首、中、尾，避免 90 天时糊成一团
  const labelAt = new Set([0, Math.floor(series.length / 2), series.length - 1]);

  return (
    <svg className="st-svg" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="每日任务量堆叠柱状图">
      {ticks.map((v) => {
        const y = PAD_T + plotH - (v / max) * plotH;
        return (
          <g key={v}>
            <line x1={PAD_L} y1={y} x2={W - 12} y2={y} stroke="#212934" strokeWidth="0.5" />
            <text x={PAD_L - 8} y={y} fontSize="11" fill="#64748b" textAnchor="end" dominantBaseline="central">
              {v}
            </text>
          </g>
        );
      })}

      {series.map((d, i) => {
        const x = PAD_L + i * step + (step - barW) / 2;
        let acc = 0;
        const segs: { v: number; color: string }[] = [
          { v: d.done, color: COLOR.done },
          { v: d.failed, color: COLOR.failed },
          { v: d.aborted, color: COLOR.aborted },
        ];
        return (
          <g key={d.date}>
            <title>{`${d.date}：${d.tasks} 个任务（完成 ${d.done} / 失败 ${d.failed} / 中止 ${d.aborted}）`}</title>
            {segs.map((s, k) => {
              if (s.v <= 0) return null;
              const h = (s.v / max) * plotH;
              const y = PAD_T + plotH - acc - h;
              acc += h;
              return <rect key={k} x={x} y={y} width={barW} height={h} fill={s.color} />;
            })}
            {labelAt.has(i) && (
              <text
                x={x + barW / 2}
                y={H - PAD_B + 14}
                fontSize="11"
                fill="#64748b"
                textAnchor="middle"
              >
                {d.date.slice(5)}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}
