import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Markdown 渲染封装
 *
 * 排版样式全部在 index.css 的 `.loom-md` 里，不在这里写 inline style ——
 * 这样聊天视图和 FINAL_ANSWER 卡片共用同一套排版。
 */
export function Markdown({ text }: { text: string }) {
  return (
    <div className="loom-md selectable">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  );
}
