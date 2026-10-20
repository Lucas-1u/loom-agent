import type { Message } from "../features/session/types";
import { Markdown } from "./Markdown";

/**
 * 单条聊天消息
 *
 * 三种角色的视觉差异（用户靠"谁说"判断，不靠颜色猜）：
 *   user      → 右对齐气泡，蓝色调
 *   assistant → 左对齐，无气泡，接近文档排版（长文更好读）
 *   system    → 居中窄条，用于错误/系统提示
 */
export function ChatMessage({ msg }: { msg: Message }) {
  if (msg.role === "user") {
    return (
      <div className="loom-enter flex justify-end">
        <div className="max-w-[76%] rounded-2xl rounded-br-md border border-tone-blue/25 bg-tone-blue/10 px-3.5 py-2">
          <p className="selectable whitespace-pre-wrap break-words text-[13px] leading-[1.7] text-slate-100">
            {msg.content}
          </p>
        </div>
      </div>
    );
  }

  if (msg.role === "system") {
    return (
      <div className="loom-enter flex justify-center">
        <div className="rounded-md border border-tone-red/25 bg-tone-red/5 px-3 py-1.5">
          <p className="selectable text-[11.5px] text-tone-red/90">{msg.content}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="loom-enter flex gap-2.5">
      <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-ink-700 bg-ink-850">
        <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path d="M2 5h12M2 11h12" stroke="#2dd4e8" strokeWidth="1.3" strokeLinecap="round" />
          <path d="M5 2v12M11 2v12" stroke="#5b9dff" strokeWidth="1.3" strokeLinecap="round" opacity="0.7" />
        </svg>
      </div>
      <div className="min-w-0 flex-1 pt-0.5">
        <Markdown text={msg.content} />
      </div>
    </div>
  );
}
