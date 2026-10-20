import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

/**
 * Vite 配置
 *
 * 与 Tauri 相关的三个关键项（Phase 6 会用到，现在先配好）：
 *   · strictPort —— Tauri 的 devUrl 写死了端口，端口漂移会导致白屏
 *   · clearScreen: false —— 保留 Rust 编译日志，否则一屏刷掉就看不到报错
 *   · envPrefix 加 TAURI_ —— 让 TAURI_ 开头的变量也能注入前端
 */
export default defineConfig({
  plugins: [react(), tailwindcss()],

  clearScreen: false,

  server: {
    port: 5173,
    strictPort: true,
    watch: {
      // Rust 侧改动不该触发前端 HMR
      ignored: ["**/src-tauri/**"],
    },
  },

  envPrefix: ["VITE_", "TAURI_"],

  build: {
    // Tauri 2 的 WebView2 基于较新的 Chromium，可以放心用现代语法
    target: "chrome105",
    sourcemap: !!process.env.TAURI_DEBUG,
  },
});
