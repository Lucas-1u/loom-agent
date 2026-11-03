//! Python 后端子进程管理（方案 C）
//!
//! Rust 直接持有 Python 子进程句柄，应用退出时自动 kill。
//! 替代方案 B（tauri 钩子外部启动），优势：
//!   - 后端生命周期与应用绑定，退出即清理，不会残留孤儿进程
//!   - 不依赖外部 bat 脚本，跨平台路径统一处理

use std::net::{SocketAddr, TcpStream};
use std::path::PathBuf;
use std::process::{Child, Command};
use std::time::{Duration, Instant};

/// 后端进程句柄。Drop 时自动 kill 子进程。
pub struct BackendProcess {
    child: Child,
}

impl BackendProcess {
    /// 启动后端并等待就绪（最多 15 秒）
    pub fn start(loom_root: &PathBuf) -> std::io::Result<BackendProcess> {
        let python = loom_root.join(".venv").join("Scripts").join("python.exe");

        if !python.exists() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                format!("找不到 Python 解释器：{}", python.display()),
            ));
        }

        let child = Command::new(&python)
            .args(["-m", "backend.server"])
            .current_dir(loom_root)
            .spawn()?;

        let backend = BackendProcess { child };
        backend.wait_ready(Duration::from_secs(15))?;
        Ok(backend)
    }

    /// 轮询 8765 端口直到就绪或超时
    fn wait_ready(&self, timeout: Duration) -> std::io::Result<()> {
        let addr: SocketAddr = "127.0.0.1:8765".parse().unwrap();
        let start = Instant::now();
        while start.elapsed() < timeout {
            if TcpStream::connect_timeout(&addr, Duration::from_millis(500)).is_ok() {
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        Err(std::io::Error::new(
            std::io::ErrorKind::TimedOut,
            "后端启动超时（15 秒内 8765 未就绪）",
        ))
    }
}

impl Drop for BackendProcess {
    fn drop(&mut self) {
        // 应用退出时自动 kill 后端子进程
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// 从可执行文件路径推导 loom 项目根目录
///
/// dev 模式下 exe 位于 <loom_root>/src-tauri/target/debug/loom-agent.exe
/// 向上三级即为 loom 根目录
pub fn find_loom_root() -> std::io::Result<PathBuf> {
    let exe = std::env::current_exe()?;
    let exe_dir = exe.parent()
        .ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::NotFound, "无法获取可执行文件路径")
        })?;

    // 向上三级：debug → target → src-tauri → loom
    let loom_root = exe_dir.join("..").join("..").join("..");

    // 验证是有效的 loom 项目根（有 backend 目录）
    if loom_root.join("backend").exists() {
        Ok(loom_root.canonicalize().unwrap_or(loom_root))
    } else {
        Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            format!(
                "从 {} 推导 loom 根失败：找不到 backend 目录",
                loom_root.display()
            ),
        ))
    }
}
