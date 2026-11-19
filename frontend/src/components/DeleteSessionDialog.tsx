import { useEffect, useMemo, useState } from "react";
import type { Session } from "../features/session/types";
import { displayTitle } from "../features/session/selectors";
import { ENDPOINTS, url } from "../services/config";

/**
 * 删除会话的确认弹窗（两级删除）。
 *
 * ★ 为什么必须先拉 preview 才允许点删除：
 *   用户点的那个按钮只写了"删除"，但他不知道会连带删掉几个任务、哪些文件。
 *   把清单摆出来（尤其是"不会删"的那部分和原因），比事后解释便宜得多。
 *
 * ★ 为什么要求输入会话标题：
 *   删除不可逆。一个输入框能挡掉"手滑点到"和"脚本写错 id"两类事故，
 *   代价只是多敲几个字。
 *
 * ★ 为什么"仅删会话"是默认选项：
 *   删库是应用自己的数据，删文件是用户机器上的东西。默认选破坏性小的那个。
 */

interface PreviewFile {
  path: string;
  bytes: number;
  task_id: string;
}
interface SkippedFile {
  path: string;
  reason: string;
}
interface Preview {
  session: { session_id: string; title: string; workspace: string };
  tasks: number;
  events: number;
  artifacts: {
    total: number;
    total_bytes: number;
    files: PreviewFile[];
    skipped: SkippedFile[];
  };
  confirm_text: string;
}

function fmtBytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${n} B`;
}

export function DeleteSessionDialog({
  session,
  onCancel,
  onDeleted,
}: {
  session: Session;
  onCancel: () => void;
  onDeleted: (sessionId: string) => void;
}) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [loadErr, setLoadErr] = useState("");
  const [withArtifacts, setWithArtifacts] = useState(false);
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [submitErr, setSubmitErr] = useState("");

  // 拉预览
  useEffect(() => {
    let alive = true;
    setLoadErr("");
    setPreview(null);
    fetch(url(`${ENDPOINTS.sessions}/${session.session_id}/delete-preview`))
      .then(async (r) => {
        const j = await r.json().catch(() => ({}));
        if (!r.ok) {
          const note = typeof j?.detail === "object" ? j.detail?.note : j?.detail;
          throw new Error(String(note || `HTTP ${r.status}`));
        }
        return j as Preview;
      })
      .then((j) => {
        if (alive) setPreview(j);
      })
      .catch((e) => {
        if (alive) setLoadErr(String(e?.message || e));
      });
    return () => {
      alive = false;
    };
  }, [session.session_id]);

  const canSubmit = useMemo(
    () => Boolean(preview) && confirm.trim() === (preview?.confirm_text ?? "") && !busy,
    [preview, confirm, busy],
  );

  async function submit() {
    if (!preview || !canSubmit) return;
    setBusy(true);
    setSubmitErr("");
    try {
      const r = await fetch(url(`${ENDPOINTS.sessions}/${session.session_id}/delete`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ with_artifacts: withArtifacts, confirm: confirm.trim() }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) {
        const note = typeof j?.detail === "object" ? j.detail?.note : j?.detail;
        throw new Error(String(note || `HTTP ${r.status}`));
      }
      onDeleted(session.session_id);
    } catch (e) {
      // 失败时**不关闭弹窗**：用户需要看到原因，而且可以改选后重试
      setSubmitErr(String((e as Error)?.message || e));
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onCancel}
      role="presentation"
    >
      <div
        className="w-full max-w-[520px] rounded-xl border border-ink-700 bg-ink-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="删除会话"
      >
        <div className="border-b border-ink-700 px-5 py-3">
          <h2 className="text-[14px] font-medium text-slate-100">删除会话</h2>
          <p className="mt-1 truncate text-[12px] text-slate-500">
            {displayTitle(session)}
            <span className="ml-2 text-slate-600">{session.workspace_path || "（无工作区）"}</span>
          </p>
        </div>

        <div className="px-5 py-4">
          {loadErr && (
            <p className="rounded-md border border-tone-red/35 bg-tone-red/10 px-3 py-2 text-[12px] text-tone-red">
              读取删除预案失败：{loadErr}
            </p>
          )}

          {!preview && !loadErr && (
            <p className="text-[12px] text-slate-500">正在统计将删除的内容…</p>
          )}

          {preview && (
            <>
              <p className="mb-3 text-[12px] text-slate-400">
                该会话有 <b className="text-slate-200">{preview.tasks}</b> 个任务、
                <b className="text-slate-200"> {preview.events}</b> 个事件。
              </p>

              <div className="space-y-2">
                <label
                  className={`flex cursor-pointer items-start gap-2.5 rounded-lg border px-3 py-2.5 transition-colors ${
                    withArtifacts ? "border-tone-red/40 bg-tone-red/8" : "border-ink-700 bg-ink-850"
                  }`}
                >
                  <input
                    type="radio"
                    name="del-mode"
                    checked={!withArtifacts}
                    onChange={() => setWithArtifacts(false)}
                    className="mt-0.5"
                  />
                  <span className="min-w-0">
                    <span className="block text-[12.5px] text-slate-200">仅删除会话</span>
                    <span className="block text-[11px] text-slate-500">
                      清掉会话、任务、事件、统计与历史记录。<b className="text-slate-400">工作区里的文件全部保留。</b>
                    </span>
                  </span>
                </label>

                <label
                  className={`flex cursor-pointer items-start gap-2.5 rounded-lg border px-3 py-2.5 transition-colors ${
                    withArtifacts ? "border-tone-red/40 bg-tone-red/8" : "border-ink-700 bg-ink-850"
                  }`}
                >
                  <input
                    type="radio"
                    name="del-mode"
                    checked={withArtifacts}
                    onChange={() => setWithArtifacts(true)}
                    className="mt-0.5"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block text-[12.5px] text-slate-200">
                      连会话产物一起删
                      <span className="ml-1.5 text-[11px] text-slate-500">
                        （{preview.artifacts.total} 个文件，共 {fmtBytes(preview.artifacts.total_bytes)}）
                      </span>
                    </span>
                    <span className="block text-[11px] text-slate-500">
                      只删<b className="text-slate-400">本会话创建、且内容未被改动过</b>的文件；
                      删除前会自动备份到 <code className="text-slate-400">.loom_backup/</code>。
                    </span>
                  </span>
                </label>
              </div>

              {withArtifacts && (
                <div className="mt-3 max-h-[168px] overflow-y-auto rounded-lg border border-ink-700 bg-ink-950/60 px-3 py-2">
                  {preview.artifacts.files.length === 0 && (
                    <p className="text-[11px] text-slate-600">这个会话没有可识别的产物文件。</p>
                  )}
                  {preview.artifacts.files.map((f) => (
                    <div key={f.path} className="flex items-baseline gap-2 py-0.5">
                      <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-tone-red">
                        − {f.path}
                      </span>
                      <span className="shrink-0 text-[10px] text-slate-600">{fmtBytes(f.bytes)}</span>
                    </div>
                  ))}
                  {preview.artifacts.skipped.length > 0 && (
                    <div className="mt-2 border-t border-ink-700 pt-2">
                      <p className="mb-1 text-[10.5px] text-slate-500">以下文件看起来相关，但不会被删：</p>
                      {preview.artifacts.skipped.map((s) => (
                        <div key={s.path} className="flex items-baseline gap-2 py-0.5">
                          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-slate-500">
                            {s.path}
                          </span>
                          <span className="shrink-0 text-[10px] text-slate-600">{s.reason}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              <div className="mt-4">
                <label className="block text-[11.5px] text-slate-500">
                  输入会话标题确认：
                  <code className="ml-1 rounded bg-ink-800 px-1.5 py-0.5 text-[11px] text-slate-300">
                    {preview.confirm_text}
                  </code>
                </label>
                <input
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  placeholder={preview.confirm_text}
                  className="mt-1.5 w-full rounded-md border border-ink-700 bg-ink-850 px-3 py-1.5 text-[12px] text-slate-200 outline-none placeholder:text-slate-600 focus:border-tone-red/50"
                  autoFocus
                />
              </div>

              {submitErr && (
                <p className="mt-3 rounded-md border border-tone-red/35 bg-tone-red/10 px-3 py-2 text-[12px] text-tone-red">
                  {submitErr}
                </p>
              )}
            </>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-ink-700 px-5 py-3">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md border border-ink-600 px-3 py-1.5 text-[12px] text-slate-300 transition-colors hover:bg-ink-800"
          >
            取消
          </button>
          <button
            type="button"
            disabled={!canSubmit}
            onClick={submit}
            className="rounded-md border border-tone-red/50 bg-tone-red/15 px-3 py-1.5 text-[12px] text-tone-red transition-colors hover:bg-tone-red/25 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? "删除中…" : withArtifacts ? "删除会话与产物" : "删除会话"}
          </button>
        </div>
      </div>
    </div>
  );
}
