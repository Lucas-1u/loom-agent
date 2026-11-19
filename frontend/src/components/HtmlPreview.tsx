import { useMemo, useState } from "react";

/**
 * HTML / SVG 代码块的「沙箱预览」。
 *
 * ★ 为什么不直接 dangerouslySetInnerHTML：
 *   聊天内容来自模型输出，而模型会读工作区文件、会调工具 —— 它的输出里
 *   可能夹带别处的文本。一旦无脑 innerHTML，一个恶意片段就能读 localStorage、
 *   拿页面里的 token。所以一律塞进 iframe。
 *
 * ★ sandbox 只给 allow-scripts，**不给 allow-same-origin**：
 *   脚本能跑（图表要它），但碰不到父页面，也读不了 cookie。
 *   这两个一起给等于白给 —— 同源 + 可脚本 = 完全逃逸。
 *
 * ★ 为什么默认不渲染、必须点一下：
 *   自动执行等于"打开消息就跑别人的代码"。多一次点击，换掉一整类风险。
 */

const PREVIEW_LANGS = new Set(["html", "htm", "svg"]);

/** 在沙箱里补一层克制的默认样式：Agent 产出的 HTML 大多假设白底 */
const BASE_STYLE = `
  <style>
    html, body { margin: 0; padding: 12px; background: #ffffff; color: #1a1a1a;
      font: 14px/1.6 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
    svg { max-width: 100%; height: auto; }
  </style>
`;

export function HtmlPreview({ lang, code }: { lang: string; code: string }) {
  const [open, setOpen] = useState(false);

  const srcdoc = useMemo(() => {
    if (lang === "svg") {
      return `<!doctype html><html><head><meta charset="utf-8">${BASE_STYLE}</head><body>${code}</body></html>`;
    }
    // 已经是完整 HTML 就原样塞（浏览器容错），否则套一层壳
    const looksFull = /<html[\s>]/i.test(code);
    return looksFull
      ? code
      : `<!doctype html><html><head><meta charset="utf-8">${BASE_STYLE}</head><body>${code}</body></html>`;
  }, [lang, code]);

  const lines = code.split("\n").length;

  return (
    <div className="my-3 overflow-hidden rounded-lg border border-ink-700">
      {/* 工具条 */}
      <div className="flex items-center gap-2 border-b border-ink-700 bg-ink-850 px-2.5 py-1.5">
        <span className="rounded bg-ink-800 px-1.5 py-0.5 font-mono text-[10px] text-slate-400">
          {lang}
        </span>
        <span className="text-[10.5px] text-slate-600">{lines} 行</span>

        <div className="flex-1" />

        {open && (
          <span
            className="hidden text-[10px] text-slate-600 sm:inline"
            title="iframe sandbox=allow-scripts，不给 allow-same-origin：脚本能跑，但读不到本页数据"
          >
            沙箱隔离运行
          </span>
        )}
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="rounded border border-ink-600 px-2 py-0.5 text-[11px] text-slate-300 transition-colors hover:border-tone-blue/40 hover:bg-ink-800 hover:text-slate-100"
        >
          {open ? "■ 收起" : "▶ 预览"}
        </button>
      </div>

      {/* 内容：默认显示代码，点过预览才换成 iframe */}
      {open ? (
        <iframe
          title={`${lang} 预览`}
          srcDoc={srcdoc}
          sandbox="allow-scripts"
          className="block h-[420px] w-full bg-white"
        />
      ) : (
        <pre className="m-0 max-h-[420px] overflow-auto bg-[#0b0f15] p-3 font-mono text-[12px] leading-relaxed text-slate-300">
          <code>{code}</code>
        </pre>
      )}
    </div>
  );
}

/** 语言是否走预览分支（Markdown 组件用它分派） */
export function isPreviewable(lang: string): boolean {
  return PREVIEW_LANGS.has(lang.toLowerCase());
}
