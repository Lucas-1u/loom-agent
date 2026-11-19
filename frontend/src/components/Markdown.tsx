import type { ReactElement, ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { HtmlPreview, isPreviewable } from "./HtmlPreview";

/**
 * Markdown 渲染封装
 *
 * 排版样式全部在 index.css 的 `.loom-md` 里，不在这里写 inline style ——
 * 这样聊天视图和 FINAL_ANSWER 卡片共用同一套排版。
 *
 * ★ 覆盖 `pre` 而不是 `code`：react-markdown v10 起 `code` 组件不再区分
 *   inline / block，只有被 `pre` 包住才是代码块。从 `pre` 里取内层 `code`
 *   的 className 判断语言，是最稳的一层。
 * ★ 只有 html / svg 交给沙箱预览，其它语言保持纯文本 —— 不执行任何东西。
 */

type CodeEl = ReactElement<{ className?: string; children?: ReactNode }>;

function pickCode(node: ReactNode): CodeEl | null {
  if (!node || typeof node !== "object") return null;
  const el = node as CodeEl;
  if (el.type !== "code") return null;
  return el;
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="loom-md selectable">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          pre({ children, ...rest }) {
            const codeEl = pickCode(children);
            const cls = codeEl?.props?.className ?? "";
            const m = /language-([\w+-]+)/.exec(cls);
            const lang = m?.[1] ?? "";

            if (lang && isPreviewable(lang)) {
              const raw = String(codeEl?.props?.children ?? "").replace(/\n+$/, "");
              return <HtmlPreview lang={lang} code={raw} />;
            }
            return <pre {...rest}>{children}</pre>;
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
