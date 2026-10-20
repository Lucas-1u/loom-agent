import type { Message } from "../features/session/types";
import type { ApprovalItem } from "../types/chat";
import type { RunHint } from "../features/events/runHint";
import { useAutoScroll } from "../hooks/useAutoScroll";
import { ChatMessage } from "./ChatMessage";

/**
 * 聊天视图
 *
 * 它只消费两样东西：`messages`（当前 Session 的 Message）和 `runHint`（从当前 Task 的最后一条事件推断）。
 * ★ 没有任何"前端计时器假装在跑"的逻辑 —— 状态一律来自真实事件。
 */
export function ChatView({
  messages,
  runHint,
  pendingApprovals,
  onGoToExecution,
}: {
  messages: Message[];
  runHint: RunHint | null;
  pendingApprovals: ApprovalItem[];
  onGoToExecution: () => void;
}) {
  const { ref, onScroll, showJump, scrollToBottom } = useAutoScroll<HTMLDivElement>(
    messages.length + (runHint ? 1 : 0),
  );

  if (!messages.length && !runHint) {
    return (
      <div className="flex h-full items-center justify-center px-6">
        <div className="max-w-[380px] text-center">
          <div className="mx-auto mb-3 flex h-11 w-11 items-center justify-center rounded-xl border border-ink-700 bg-ink-850">
            <svg width="18" height="18" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path
                d="M2.5 3.2h11a1 1 0 0 1 1 1v6.4a1 1 0 0 1-1 1H6.2L3 14V11.6h-.5a1 1 0 0 1-1-1V4.2a1 1 0 0 1 1-1Z"
                stroke="#4b5a6e"
                strokeWidth="1.2"
                strokeLinejoin="round"
              />
            </svg>
          </div>
          <p className="text-[13px] text-slate-400">开始和 Loom 对话</p>
          <p className="mt-1 text-[11.5px] leading-relaxed text-slate-600">
            在下方输入任务。想要看它每一步在干什么，切到「执行流」。
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="relative h-full">
      <div ref={ref} onScroll={onScroll} className="h-full overflow-y-auto overscroll-contain">
        <div className="mx-auto max-w-[820px] space-y-5 px-5 py-5">
          {messages.map((m) => (
            <ChatMessage key={m.message_id} msg={m} />
          ))}

          {/* 运行状态（真实事件推断，不是假动画） */}
          {runHint && (
            <div className="loom-enter flex items-center gap-2 pt-0.5">
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  runHint.spinning ? "loom-blink" : ""
                } ${
                  runHint.tone === "amber"
                    ? "bg-tone-amber"
                    : runHint.tone === "red"
                      ? "bg-tone-red"
                      : runHint.tone === "green"
                        ? "bg-tone-green"
                        : runHint.tone === "teal"
                          ? "bg-tone-teal"
                          : runHint.tone === "violet"
                            ? "bg-tone-violet"
                            : runHint.tone === "cyan"
                              ? "bg-tone-cyan"
                              : "bg-tone-slate"
                }`}
              />
              <span className="text-[12px] text-slate-500">{runHint.text}</span>
            </div>
          )}

          {/* ★ 待审批提示：**在聊天视图里也必须能看到**
              任务书 §4.2 明确要求 —— 不能逼用户切到执行流才发现 Agent 在等授权 */}
          {pendingApprovals.length > 0 && (
            <div className="loom-enter rounded-lg border border-tone-amber/35 bg-tone-amber/8 px-3.5 py-3">
              <div className="flex items-start gap-2">
                <span className="mt-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-tone-amber/20 text-[10px] font-bold text-tone-amber">
                  !
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-[12.5px] font-medium text-tone-amber">
                    Agent 正在等待你的审批（{pendingApprovals.length} 项）
                  </p>
                  {pendingApprovals.map((a) => (
                    <p key={a.approval_id} className="mt-1 text-[11.5px] text-slate-300">
                      <span className="text-slate-100">{a.action}</span>
                      {a.target && (
                        <span className="ml-1.5 font-mono text-[11px] text-slate-400">
                          {a.target}
                        </span>
                      )}
                    </p>
                  ))}

                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      onClick={onGoToExecution}
                      className="rounded-md border border-tone-amber/40 bg-tone-amber/15 px-2.5 py-1 text-[11.5px] text-tone-amber transition-colors hover:bg-tone-amber/25"
                    >
                      去执行流处理 →
                    </button>
                    <span className="text-[10px] text-slate-600">
                      批准/拒绝按钮在执行流的审批卡片里（Phase 5 接入）
                    </span>
                  </div>
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {showJump && (
        <button
          type="button"
          onClick={() => scrollToBottom()}
          className="loom-enter absolute bottom-4 left-1/2 flex -translate-x-1/2 items-center gap-1.5 rounded-full border border-ink-600 bg-ink-800/95 px-3 py-1.5 text-[11.5px] text-slate-300 shadow-lg backdrop-blur transition-colors hover:border-tone-cyan/40 hover:text-slate-100"
        >
          <svg width="11" height="11" viewBox="0 0 12 12" fill="none" aria-hidden="true">
            <path
              d="M6 2v7M3 6.5 6 9.5l3-3"
              stroke="currentColor"
              strokeWidth="1.4"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          回到最新
        </button>
      )}
    </div>
  );
}
