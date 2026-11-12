import { useMemo, useState } from "react";
import { answerUserInput } from "../services/api";
import type { UserInputAnswer, UserInputQuestion } from "../types/events";

/**
 * ask_user 输入卡片（执行流内）
 *
 * ★ 契约对齐 DSH（2026-11-11）：一次可以问多个问题，每题可带选项与多选。
 *
 * ★ 「其他（自己填写）」永远存在 —— 这是本地的关键增强：
 *   AI 给的选项再全也可能穷举不完（路径、命名、取值……），
 *   只给固定选项等于逼用户"选个最接近的"，那比不问更糟。
 *   所以每题末尾固定追加一个自由输入口，答案走 `custom` 字段。
 *
 * ★ 未作答的问题不算错：提交时 unanswered → `{id, selected: []}`，
 *   语义是"用户明确跳过"（对齐 DSH 的 skipped 语义），和"没提交"不同。
 *
 * ★ 自包含：直接从事件 data 拿 task_id / ask_id，不走全局 store ——
 *   避免为一个小功能改动整个状态机。
 */
export function UserInputCard({ data }: { data: Record<string, unknown> }) {
  const askId = String(data.ask_id ?? "");
  const taskId = String(data.task_id ?? "");
  /** pending = 前台等待已超时但问题没作废（同一张卡片，文案与徽标要改口） */
  const isPending = Boolean(data.pending);

  /** 老事件只有单个 question 字段，这里统一成 questions 数组 */
  const questions = useMemo<UserInputQuestion[]>(() => {
    const raw = data.questions;
    if (Array.isArray(raw) && raw.length > 0) {
      return (raw as UserInputQuestion[]).filter((q) => q && q.id && q.question);
    }
    const text = String(data.question ?? "");
    return text ? [{ id: "q1", question: text }] : [];
  }, [data.questions, data.question]);

  const [picked, setPicked] = useState<Record<string, string[]>>({});
  const [customOn, setCustomOn] = useState<Record<string, boolean>>({});
  const [customText, setCustomText] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState<"" | "answered" | "late">("");
  const [error, setError] = useState("");

  const hasOptions = (q: UserInputQuestion) => Array.isArray(q.options) && q.options.length > 0;

  function pickOption(q: UserInputQuestion, label: string) {
    setPicked((prev) => {
      const cur = prev[q.id] ?? [];
      if (q.multi_select) {
        const next = cur.includes(label) ? cur.filter((x) => x !== label) : [...cur, label];
        return { ...prev, [q.id]: next };
      }
      return { ...prev, [q.id]: cur.includes(label) ? [] : [label] };
    });
    // 单选：选了普通选项就取消「其他」
    if (!q.multi_select) setCustomOn((prev) => ({ ...prev, [q.id]: false }));
  }

  function toggleCustom(q: UserInputQuestion) {
    setCustomOn((prev) => {
      const next = !prev[q.id];
      if (next && !q.multi_select) {
        // 单选：选了「其他」就清空已选项
        setPicked((p) => ({ ...p, [q.id]: [] }));
      }
      return { ...prev, [q.id]: next };
    });
  }

  function buildAnswers(): UserInputAnswer[] {
    return questions.map((q) => {
      const selected = picked[q.id] ?? [];
      const custom = customOn[q.id] || !hasOptions(q) ? (customText[q.id] ?? "").trim() : "";
      const item: UserInputAnswer = { id: q.id, selected };
      if (custom) item.custom = custom;
      return item;
    });
  }

  /** 「其他」勾了却没填字 → 不静默当跳过（用户以为答了，实际什么都没发） */
  const emptyCustom = questions.filter(
    (q) => customOn[q.id] && hasOptions(q) && !(customText[q.id] ?? "").trim(),
  );

  async function handleSubmit() {
    if (!askId || !taskId || submitting || submitted) return;
    if (emptyCustom.length > 0) {
      setError("勾了「其他」但还没填写内容；不想答就再点一次取消勾选，会记为跳过。");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const r = await answerUserInput(taskId, askId, buildAnswers());
      if (r.accepted) {
        setSubmitted(r.status === "late" ? "late" : "answered");
      } else {
        setError(r.note || "提交失败（问题可能已回答或已过期）");
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSubmitting(false);
    }
  }

  const multiCount = questions.filter((q) => q.multi_select).length;

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <span className="text-[12.5px] font-medium text-slate-100">
          {isPending
            ? "等待已超时，但问题没作废"
            : questions.length > 1
              ? `Agent 在等你回答（${questions.length} 个问题）`
              : "Agent 在等你回答"}
        </span>
        <span className="rounded border border-tone-amber/40 bg-tone-amber/10 px-1.5 py-px text-[10px] font-medium text-tone-amber">
          {isPending ? "可补答" : "挂起中"}
        </span>
        {multiCount > 0 && (
          <span className="text-[10px] text-slate-600">可多选</span>
        )}
      </div>

      <div className="space-y-2.5 rounded border border-tone-amber/25 bg-tone-amber/5 px-2.5 py-2">
        {questions.map((q, qi) => {
          const opts = q.options ?? [];
          const isCustomOn = customOn[q.id] ?? !hasOptions(q);
          return (
            <div key={q.id} className="space-y-1.5">
              <div className="flex items-baseline gap-1.5">
                {questions.length > 1 && (
                  <span className="shrink-0 font-mono text-[10px] text-slate-600">{qi + 1}.</span>
                )}
                {q.header && (
                  <span className="shrink-0 rounded bg-ink-800 px-1 py-px text-[10px] text-slate-400">
                    {q.header}
                  </span>
                )}
                <p className="selectable text-[12px] leading-relaxed text-slate-200">{q.question}</p>
              </div>

              {opts.length > 0 && (
                <div className="space-y-1 pl-1">
                  {opts.map((opt) => {
                    const on = (picked[q.id] ?? []).includes(opt.label);
                    return (
                      <button
                        key={opt.label}
                        type="button"
                        disabled={submitting || !!submitted}
                        onClick={() => pickOption(q, opt.label)}
                        className={[
                          "flex w-full items-start gap-2 rounded border px-2 py-1 text-left transition-colors",
                          on
                            ? "border-tone-cyan/45 bg-tone-cyan/10"
                            : "border-ink-700 bg-ink-900/40 hover:border-ink-600",
                        ].join(" ")}
                      >
                        <span
                          className={[
                            "mt-[3px] flex h-3.5 w-3.5 shrink-0 items-center justify-center border font-mono text-[9px]",
                            q.multi_select ? "rounded-sm" : "rounded-full",
                            on ? "border-tone-cyan text-tone-cyan" : "border-ink-600 text-transparent",
                          ].join(" ")}
                        >
                          {on ? "✓" : "·"}
                        </span>
                        <span className="min-w-0">
                          <span className={`text-[12px] ${on ? "text-slate-100" : "text-slate-300"}`}>
                            {opt.label}
                          </span>
                          {opt.description && (
                            <span className="ml-1.5 text-[10.5px] text-slate-600">{opt.description}</span>
                          )}
                        </span>
                      </button>
                    );
                  })}

                  {/* ★ 永远多一个自由输入口：选项穷举不完时，用户不该被迫选个最接近的 */}
                  <button
                    type="button"
                    disabled={submitting || !!submitted}
                    onClick={() => toggleCustom(q)}
                    className={[
                      "flex w-full items-start gap-2 rounded border px-2 py-1 text-left transition-colors",
                      isCustomOn
                        ? "border-tone-violet/45 bg-tone-violet/10"
                        : "border-ink-700 bg-ink-900/40 hover:border-ink-600",
                    ].join(" ")}
                  >
                    <span
                      className={[
                        "mt-[3px] flex h-3.5 w-3.5 shrink-0 items-center justify-center border font-mono text-[9px]",
                        q.multi_select ? "rounded-sm" : "rounded-full",
                        isCustomOn ? "border-tone-violet text-tone-violet" : "border-ink-600 text-transparent",
                      ].join(" ")}
                    >
                      {isCustomOn ? "✓" : "·"}
                    </span>
                    <span className="text-[12px] text-slate-300">
                      其他（自己填写）
                      <span className="ml-1.5 text-[10.5px] text-slate-600">选项都不合适时用这个</span>
                    </span>
                  </button>
                </div>
              )}

              {(isCustomOn || opts.length === 0) && (
                <input
                  type="text"
                  value={customText[q.id] ?? ""}
                  disabled={submitting || !!submitted}
                  onChange={(e) => setCustomText((prev) => ({ ...prev, [q.id]: e.target.value }))}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") void handleSubmit();
                  }}
                  placeholder={opts.length === 0 ? "输入你的回答…" : "自己填写…"}
                  className="w-full rounded-md border border-ink-600 bg-ink-950/70 px-2.5 py-1 text-[12px] text-slate-200 outline-none focus:border-tone-violet/50"
                />
              )}
            </div>
          );
        })}
      </div>

      {submitted ? (
        <p className="text-[11.5px] text-tone-green">
          {submitted === "late"
            ? "已提交。Agent 当时没等到这条回答，会在下一步看到它。"
            : "已提交，Agent 已恢复执行。"}
        </p>
      ) : (
        <>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => void handleSubmit()}
              disabled={submitting || !askId || !taskId}
              className="rounded-md border border-tone-cyan/35 bg-tone-cyan/10 px-3 py-1 text-[12px] font-medium text-tone-cyan transition-colors hover:bg-tone-cyan/20 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {submitting ? "提交中…" : isPending ? "补答" : "回答"}
            </button>
            <span className="text-[10px] text-slate-600">
              {isPending
                ? "Agent 已往下走了，这条会作为一条用户消息送进它下一步"
                : "没作答的问题会记为「跳过」，不是拒绝"}
            </span>
          </div>
          {error ? <p className="text-[11px] text-tone-red">{error}</p> : null}
        </>
      )}

      <p className="text-[10px] text-slate-600">ask_id {askId || "—"}</p>
    </div>
  );
}
