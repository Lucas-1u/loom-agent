import { useCallback, useEffect, useRef, useState } from "react";
import type { SessionMode } from "../types/events";

/**
 * 权限模式选项（步骤 7）
 *
 * ★ 文案必须如实：`full-access` 是真的可以写宿主机任意位置，
 *   不能写成"高级模式"这类模糊说法 —— 用户要能一眼看出风险。
 */
const MODE_OPTIONS: Array<{ value: SessionMode; label: string; tip: string }> = [
  {
    value: "read-only",
    label: "只读",
    tip: "只注册只读工具：模型看得到文件，但改不了、也执行不了命令",
  },
  {
    value: "workspace-write",
    label: "可写",
    tip: "可修改工作区内文件并执行命令（默认）",
  },
  {
    value: "full-access",
    label: "完全访问",
    tip: "不受工作区写限制，可写宿主机任意位置 —— 仅在明确需要时使用",
  },
];

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
  mode,
  onModeChange,
}: {
  onSubmit: (goal: string) => void;
  onStop: () => void;
  running: boolean;
  disabled?: boolean;
  hint?: string;
  mode: SessionMode;
  onModeChange: (m: SessionMode) => void;
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
            <div className="flex flex-wrap items-center gap-3 text-[10.5px] text-slate-600">
              {/* 步骤 7：权限模式切换入口（只影响下一条任务） */}
              <div className="flex items-center gap-1" role="group" aria-label="权限模式">
                {MODE_OPTIONS.map((opt) => {
                  const active = opt.value === mode;
                  const activeCls =
                    opt.value === "full-access"
                      ? "border-tone-red/45 bg-tone-red/15 text-tone-red"
                      : opt.value === "read-only"
                        ? "border-tone-cyan/45 bg-tone-cyan/15 text-tone-cyan"
                        : "border-tone-green/45 bg-tone-green/15 text-tone-green";
                  return (
                    <button
                      key={opt.value}
                      type="button"
                      onClick={() => onModeChange(opt.value)}
                      disabled={disabled}
                      aria-pressed={active}
                      title={opt.tip}
                      className={[
                        "rounded border px-1.5 py-0.5 text-[10.5px] transition-colors",
                        active
                          ? activeCls
                          : "border-ink-600 bg-ink-800 text-slate-500 hover:border-ink-500 hover:text-slate-300",
                        disabled ? "cursor-not-allowed opacity-60" : "",
                      ].join(" ")}
                    >
                      {opt.label}
                    </button>
                  );
                })}
              </div>

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
          审批已移除：模型会直接执行已注册的工具 · 边界由会话模式与沙箱决定，前端无法绕过
        </p>
      </div>
    </div>
  );
}
