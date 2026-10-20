import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 底部任务输入框（两种视图共用，位置固定）
 *
 * ★ 它不直接调后端 —— 只调 `onSubmit(goal)`。
 *   Phase 2 里父组件把它接到"往 store 里塞一条演示消息"，
 *   Phase 3 换成 `POST /api/tasks`，这里不用改。
 */
export function TaskComposer({
  onSubmit,
  onStop,
  running,
  disabled,
  hint,
}: {
  onSubmit: (goal: string) => void;
  onStop: () => void;
  running: boolean;
  disabled?: boolean;
  hint?: string;
}) {
  const [text, setText] = useState("");
  const ref = useRef<HTMLTextAreaElement>(null);

  // 自适应高度：3 行起步，最多 8 行
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    const line = 22;
    const max = line * 8;
    el.style.height = `${Math.min(el.scrollHeight, max)}px`;
  }, [text]);

  const canSend = text.trim().length > 0 && !running && !disabled;

  const submit = useCallback(() => {
    const goal = text.trim();
    if (!goal || running || disabled) return;
    onSubmit(goal);
    setText("");
  }, [text, running, disabled, onSubmit]);

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    // Ctrl/Cmd + Enter 发送；单独 Enter 换行（多行任务更常见）
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      submit();
    }
  }

  return (
    <div className="shrink-0 border-t border-ink-700 bg-ink-900 px-4 py-3">
      <div className="mx-auto max-w-[980px]">
        <div
          className={[
            "rounded-lg border bg-ink-850 transition-colors",
            running ? "border-tone-amber/30" : "border-ink-600 focus-within:border-tone-cyan/40",
          ].join(" ")}
        >
          <textarea
            ref={ref}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={disabled}
            rows={3}
            placeholder={
              running
                ? "任务执行中…… 可以先想好下一条要问什么"
                : "输入任务，例如「分析当前项目的结构」"
            }
            className="selectable block w-full resize-none bg-transparent px-3.5 py-2.5 text-[13px] leading-[1.6] text-slate-100 outline-none placeholder:text-slate-600 disabled:cursor-not-allowed"
            style={{ minHeight: "66px" }}
          />

          <div className="flex items-center justify-between gap-2 border-t border-ink-700/70 px-3 py-2">
            <div className="flex items-center gap-3 text-[10.5px] text-slate-600">
              <span>
                <kbd className="rounded border border-ink-600 bg-ink-800 px-1 py-0.5 font-mono text-[10px]">
                  Ctrl
                </kbd>
                {" + "}
                <kbd className="rounded border border-ink-600 bg-ink-800 px-1 py-0.5 font-mono text-[10px]">
                  Enter
                </kbd>{" "}
                发送
              </span>
              {hint && <span className="text-slate-500">{hint}</span>}
            </div>

            <div className="flex items-center gap-2">
              {running && (
                <button
                  type="button"
                  onClick={onStop}
                  className="flex items-center gap-1.5 rounded-md border border-tone-red/35 bg-tone-red/10 px-2.5 py-1 text-[12px] text-tone-red transition-colors hover:bg-tone-red/20"
                >
                  <span className="h-2 w-2 rounded-[1px] bg-tone-red" />
                  停止
                </button>
              )}

              <button
                type="button"
                onClick={submit}
                disabled={!canSend}
                className={[
                  "rounded-md px-3.5 py-1 text-[12px] font-medium transition-colors",
                  canSend
                    ? "bg-tone-blue/85 text-ink-950 hover:bg-tone-blue"
                    : "cursor-not-allowed bg-ink-700 text-slate-500",
                ].join(" ")}
              >
                发送
              </button>
            </div>
          </div>
        </div>

        <p className="mt-1.5 text-center text-[10px] text-slate-600">
          所有写操作都会先请求你的批准 · 权限判定由后端完成，前端无法绕过
        </p>
      </div>
    </div>
  );
}
