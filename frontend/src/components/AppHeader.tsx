import type { ConnectionState, ViewMode } from "../types/chat";
import { ViewSwitcher } from "./ViewSwitcher";

/**
 * 已知但**未接入**的模型（仅作占位展示）。
 *
 * ★ M5 之后修过一次"假装能切模型"的问题：
 *   原来这里是一份写死的候选列表，跟后端 `.env` 里真正在跑的模型毫无关系 ——
 *   `/api/health` 返回 `spark-x2.6`，但 `<select>` 的 options 里没有它，
 *   浏览器只能把显示回落到列表第一项，于是界面永远显示一个"老模型"。
 *   更糟的是 `onModelChange` 只改前端 state，后端 scheduler 用的是启动时
 *   按 `cfg.model` 建好的 adapter —— 选了也不生效。
 *
 * 现在的约定：
 *   · 第一项永远是 `/api/health` 返回的**真实模型**，标注「后端配置」；
 *   · 其余候选保持 disabled，明确是"未接入"，不给"能切换"的错觉；
 *   · 等后端支持按任务指定模型后，再把 disabled 去掉（见 docs 的后续事项）。
 */
const KNOWN_MODELS = ["qwen3.7-plus", "qwen3.7-max", "deepseek-chat"];

export function AppHeader({
  viewMode,
  onViewChange,
  model,
  onModelChange,
  connection,
  demo,
}: {
  viewMode: ViewMode;
  onViewChange: (m: ViewMode) => void;
  model: string;
  onModelChange: (m: string) => void;
  connection: ConnectionState;
  demo: boolean;
}) {
  // 真实模型可能为空（演示 / 未连接），此时不伪造一个模型名
  const active = (model || "").trim();
  const hasReal = active !== "" && active !== "—" && active !== "演示";
  const placeholders = KNOWN_MODELS.filter((m) => m !== active);
  return (
    <header className="flex h-14 shrink-0 items-center gap-4 border-b border-ink-700 bg-ink-900 px-4">
      {/* ---- 左：品牌 ---- */}
      <div className="flex shrink-0 items-center gap-2.5">
        <Logo />
        <div className="leading-tight">
          <div className="text-[15px] font-semibold tracking-wide text-slate-100">Loom</div>
          <div className="text-[10px] uppercase tracking-[0.14em] text-slate-500">
            Agent Runtime
          </div>
        </div>
      </div>

      {/* ---- 中：视图切换（居中）---- */}
      <div className="flex flex-1 justify-center">
        <ViewSwitcher mode={viewMode} onChange={onViewChange} />
      </div>

      {/* ---- 右：模型 / 状态 ---- */}
      <div className="flex shrink-0 items-center gap-3">
        <label className="flex items-center gap-1.5 text-[11px] text-slate-500">
          <span className="hidden xl:inline">模型</span>
          <select
            value={active}
            onChange={(e) => onModelChange(e.target.value)}
            title="当前模型由后端配置（.env 的 provider/model）决定；灰色项尚未接入，选中也不会改变实际调用。"
            className="cursor-pointer rounded-md border border-ink-700 bg-ink-850 px-2 py-1 text-[12px] text-slate-200 outline-none transition-colors hover:border-ink-600 focus:border-tone-cyan/50"
          >
            {hasReal ? (
              <option value={active}>{active}（后端配置）</option>
            ) : (
              // 演示 / 离线时不伪造模型名，如实显示当前状态
              <option value={active}>{active || "未连接"}</option>
            )}
            {placeholders.map((m) => (
              <option key={m} value={m} disabled>
                {m}（未接入）
              </option>
            ))}
          </select>
        </label>

        <StatusBadge demo={demo} connection={connection} />
      </div>
    </header>
  );
}

/**
 * 状态徽章
 *
 * ★ 两个状态不能混在一起显示：
 *     连接状态（有没有连上后端）
 *     任务状态（Agent 在不在跑）
 *   之前我把它们塞进同一个徽章，结果「任务在跑」时连接状态被盖掉，
 *   而演示模式下又永远显示"未连接"—— 看起来像出了故障。
 *
 * ★ 演示模式的语义不是"未连接"，而是"**不需要连接**"。
 *   所以这里显示「演示数据」而不是灰色的「未连接」。
 */
function StatusBadge({ demo, connection }: { demo: boolean; connection: ConnectionState }) {
  if (demo) {
    return (
      <span
        className="flex items-center gap-1.5 rounded border border-tone-amber/35 bg-tone-amber/10 px-1.5 py-0.5 text-[10px] font-semibold tracking-wider text-tone-amber"
        title="当前界面使用演示数据，没有连接后端。Phase 3 接入 HTTP + SSE 后这里会变成连接状态。"
      >
        <span className="h-1.5 w-1.5 rounded-full bg-tone-amber" />
        演示数据
      </span>
    );
  }

  const map: Record<ConnectionState, { label: string; dot: string; text: string; tip: string }> = {
    idle: {
      label: "未连接",
      dot: "bg-slate-600",
      text: "text-slate-500",
      tip: "还没有连接后端服务",
    },
    connecting: {
      label: "连接中",
      dot: "bg-tone-amber loom-blink",
      text: "text-tone-amber",
      tip: "正在连接后端",
    },
    connected: {
      label: "已连接",
      dot: "bg-tone-green",
      text: "text-tone-green",
      tip: "已连接后端，事件流正常",
    },
    error: {
      label: "连接失败",
      dot: "bg-tone-red",
      text: "text-tone-red",
      tip: "后端无响应，检查服务是否启动",
    },
  };
  const c = map[connection];

  return (
    <span className="flex items-center gap-1.5 text-[11px]" title={c.tip}>
      <span className={`h-1.5 w-1.5 rounded-full ${c.dot}`} />
      <span className={c.text}>{c.label}</span>
    </span>
  );
}

/** 织机意象：经纬交叉 */
function Logo() {
  return (
    <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-ink-700 bg-gradient-to-br from-ink-800 to-ink-850">
      <svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true">
        <path d="M3 6h12M3 12h12" stroke="#2dd4e8" strokeWidth="1.4" strokeLinecap="round" />
        <path d="M6 3v12M12 3v12" stroke="#5b9dff" strokeWidth="1.4" strokeLinecap="round" opacity="0.75" />
      </svg>
    </div>
  );
}
