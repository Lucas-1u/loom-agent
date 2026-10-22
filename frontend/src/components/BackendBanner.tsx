import type { StreamState } from "../services/eventStream";
import type { AppMode } from "../features/session/store";

/**
 * 顶部状态条
 *
 * 四种情况共用一条，但语义严格区分：
 *
 *   offline  → 「后端未启动」+ 重试 + 查看演示数据   （琥珀/红）
 *   demo     → 「演示数据」+ 退出演示               （琥珀，常驻）
 *   gap      → 「事件流有缺口」                     （琥珀，可关）
 *   message  → 操作结果的临时提示                   （红/蓝，可关）
 *
 * ★ 为什么不用 toast？
 *   "事件流有缺口"这类信息不该 3 秒后消失 —— 用户可能需要过一会儿才看执行流。
 *   所以缺口计数常驻，直到用户明确关掉或重新连上。
 */
export function BackendBanner({
  mode,
  streamState,
  message,
  detail,
  gaps,
  demoAllowed,
  onRetry,
  onEnterDemo,
  onExitDemo,
  onDismiss,
}: {
  mode: AppMode;
  streamState: StreamState;
  message: string;
  detail: string;
  gaps: number;
  demoAllowed: boolean;
  onRetry: () => void;
  onEnterDemo: () => void;
  onExitDemo: () => void;
  onDismiss: () => void;
}) {
  // ---- offline：后端没起来 ----
  if (mode === "offline") {
    return (
      <Bar tone="red">
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span className="text-[12.5px] font-medium">后端未启动</span>
          <span className="text-[11px] text-slate-400">
            {detail ||
              "请先运行 python -m backend.server，再点「重试」。界面不会自动切到演示数据。"}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button type="button" onClick={onRetry} className={BTN_PRIMARY}>
            重试
          </button>
          {demoAllowed && (
            <button type="button" onClick={onEnterDemo} className={BTN_GHOST}>
              查看演示数据
            </button>
          )}
        </div>
      </Bar>
    );
  }

  // ---- demo：用户手动进来的 ----
  if (mode === "demo") {
    return (
      <Bar tone="amber">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <span className="shrink-0 rounded bg-tone-amber/20 px-1.5 py-px text-[10px] font-semibold tracking-wider text-tone-amber">
            DEMO
          </span>
          <span className="truncate text-[11.5px] text-slate-300">
            当前是演示数据，不会调用 Agent，也不会写入后端。
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button type="button" onClick={onExitDemo} className={BTN_GHOST}>
            退出演示
          </button>
        </div>
      </Bar>
    );
  }

  // ---- 缺口 / 重连 / 普通消息 ----
  const reconnecting = streamState === "reconnecting";
  const disconnected = streamState === "closed";

  return (
    <Bar tone={gaps > 0 || disconnected ? "amber" : reconnecting ? "slate" : "red"}>
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex flex-wrap items-center gap-2">
          {reconnecting && (
            <span className="loom-blink h-1.5 w-1.5 rounded-full bg-tone-amber" aria-hidden />
          )}
          {gaps > 0 && (
            <span className="shrink-0 rounded bg-tone-amber/20 px-1.5 py-px text-[10px] text-tone-amber">
              事件流缺口 ×{gaps}
            </span>
          )}
          {disconnected && (
            <span className="shrink-0 rounded bg-tone-red/20 px-1.5 py-px text-[10px] text-tone-red">
              连接已断开
            </span>
          )}
          {message && <span className="text-[12.5px] font-medium text-slate-100">{message}</span>}
        </div>
        {detail && (
          <span className="text-[11px] leading-relaxed text-slate-400">{detail}</span>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        {(disconnected || reconnecting) && (
          <button type="button" onClick={onRetry} className={BTN_GHOST}>
            重新连接
          </button>
        )}
        <button
          type="button"
          onClick={onDismiss}
          className="rounded px-1.5 py-0.5 text-[11px] text-slate-500 transition-colors hover:text-slate-300"
          title="关闭提示"
        >
          ✕
        </button>
      </div>
    </Bar>
  );
}

const BTN_PRIMARY =
  "rounded-md border border-tone-blue/40 bg-tone-blue/15 px-2.5 py-1 text-[11.5px] text-tone-blue transition-colors hover:bg-tone-blue/25";
const BTN_GHOST =
  "rounded-md border border-ink-600 bg-ink-800 px-2.5 py-1 text-[11.5px] text-slate-300 transition-colors hover:border-ink-600 hover:text-slate-100";

function Bar({
  tone,
  children,
}: {
  tone: "red" | "amber" | "slate";
  children: React.ReactNode;
}) {
  const cls =
    tone === "red"
      ? "border-tone-red/30 bg-tone-red/8"
      : tone === "amber"
        ? "border-tone-amber/30 bg-tone-amber/8"
        : "border-ink-600 bg-ink-850";

  return (
    <div className={`flex shrink-0 items-start gap-3 border-b px-4 py-2 ${cls}`}>
      {children}
    </div>
  );
}
