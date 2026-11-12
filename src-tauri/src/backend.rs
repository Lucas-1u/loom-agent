//! Python 后端子进程管理（方案 C）
//!
//! Rust 直接持有 Python 子进程句柄，应用退出时自动 kill。
//! 替代方案 B（tauri 钩子外部启动），优势：
//!   - 后端生命周期与应用绑定，退出即清理，不会残留孤儿进程
//!   - 不依赖外部 bat 脚本，跨平台路径统一处理
//!
//! ★ 2026-11-11 补两件事（为了让 release 版真的能双击运行）：
//!   1. **定位项目根**：原来只按"exe 向上三级"硬推，只有
//!      `src-tauri/target/{debug,release}/` 这种仓库内位置成立。现在改成
//!      「环境变量 LOOM_ROOT → 从 exe 向上逐级找 → 从 cwd 向上逐级找」，
//!      判定条件是目录里真的有 `backend/server/__main__.py`，不是猜层数。
//!   2. **复用已在跑的后端**：如果你先跑了 `loom-gui.bat`（或自己开了
//!      `python -m backend.server`），8765 已经有人听了。这时**不能**再起第二个
//!      （会因端口占用直接死），更不能在退出时 kill —— 那会顺手杀掉你自己的后端。
//!      所以句柄是 Option：只有"我们起的"才在 Drop 时 kill。

use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::time::{Duration, Instant};

/// 后端监听地址（与 backend/config.py 的 ServerConfig 默认值一致）
const BACKEND_ADDR: &str = "127.0.0.1:8765";

/// 后端进程句柄。Drop 时自动 kill 子进程。
pub struct BackendProcess {
    /// `None` = 复用了已经运行的实例 —— 不是我们启动的，退出时**不许** kill
    child: Option<Child>,
}

impl BackendProcess {
    /// 启动后端并等待就绪（最多 15 秒）；已在运行则直接复用
    pub fn start(loom_root: &PathBuf) -> std::io::Result<BackendProcess> {
        if is_ready() {
            println!("[loom] 8765 已在监听 —— 复用已有后端（退出时不会关掉它）");
            return Ok(BackendProcess { child: None });
        }

        let python = resolve_python(loom_root)?;
        println!("[loom] 启动后端：{} -m backend.server（cwd={}）",
                 python.display(), loom_root.display());

        let child = Command::new(&python)
            .args(["-m", "backend.server"])
            .current_dir(loom_root)
            .spawn()?;

        let backend = BackendProcess { child: Some(child) };
        backend.wait_ready(Duration::from_secs(15))?;
        Ok(backend)
    }

    /// 轮询 8765 端口直到就绪或超时
    fn wait_ready(&self, timeout: Duration) -> std::io::Result<()> {
        let start = Instant::now();
        while start.elapsed() < timeout {
            if is_ready() {
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        Err(std::io::Error::new(
            std::io::ErrorKind::TimedOut,
            "后端启动超时（15 秒内 8765 未就绪）。可先手动跑 \
             `.venv\\Scripts\\python.exe -m backend.server` 看报错。",
        ))
    }
}

impl Drop for BackendProcess {
    fn drop(&mut self) {
        // 只关自己启动的那个；复用别人的实例时 child 是 None
        if let Some(mut child) = self.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// 8765 是否已经有人监听
fn is_ready() -> bool {
    let addr: SocketAddr = BACKEND_ADDR.parse().unwrap();
    TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_ok()
}

/// 找 Python 解释器
///
/// 优先级：`LOOM_PYTHON`（显式指定）→ 仓库内 `.venv`。
/// ★ release 版仍然是"跑仓库里的代码"，所以依赖 `.venv`；
///   真要装给别人，得把 backend 打成 sidecar（PyInstaller）再改这里。
fn resolve_python(loom_root: &PathBuf) -> std::io::Result<PathBuf> {
    if let Ok(explicit) = std::env::var("LOOM_PYTHON") {
        let p = PathBuf::from(explicit);
        if p.exists() {
            return Ok(p);
        }
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            format!("LOOM_PYTHON 指向的解释器不存在：{}", p.display()),
        ));
    }

    let candidates = [
        loom_root.join(".venv").join("Scripts").join("python.exe"), // Windows
        loom_root.join(".venv").join("bin").join("python"),         // Unix
    ];
    for p in candidates.iter() {
        if p.exists() {
            return Ok(p.clone());
        }
    }

    Err(std::io::Error::new(
        std::io::ErrorKind::NotFound,
        format!(
            "找不到 Python 解释器：{}\n\
             请在 loom 目录下创建虚拟环境并装依赖：\n\
             \x20 python -m venv .venv\n\
             \x20 .venv\\Scripts\\pip install -r requirements.txt",
            loom_root.join(".venv").display()
        ),
    ))
}

/// 定位 loom 项目根目录
///
/// 顺序：`LOOM_ROOT` → 从 exe 所在目录向上逐级 → 从 cwd 向上逐级。
/// 判定条件不是"第几层"，而是"这个目录里真的有 backend/server/__main__.py"，
/// 所以 target 目录换个位置、或者 exe 被拷到别处，都不会误判。
pub fn find_loom_root() -> std::io::Result<PathBuf> {
    if let Ok(explicit) = std::env::var("LOOM_ROOT") {
        let p = PathBuf::from(&explicit);
        if is_loom_root(&p) {
            return Ok(p.canonicalize().unwrap_or(p));
        }
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            format!("LOOM_ROOT={} 不是有效的 loom 根目录（缺 backend/server/__main__.py）", explicit),
        ));
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            if let Some(root) = walk_up(dir, 8) {
                return Ok(root);
            }
        }
    }

    if let Ok(cwd) = std::env::current_dir() {
        if let Some(root) = walk_up(&cwd, 8) {
            return Ok(root);
        }
    }

    Err(std::io::Error::new(
        std::io::ErrorKind::NotFound,
        "无法定位 loom 项目根目录。\n\
         期望的目录里应该有 backend/server/__main__.py。\n\
         可以把 exe 放回仓库内的 src-tauri/target/release/，\
         或设置环境变量 LOOM_ROOT=D:\\Loom_agent\\loom。",
    ))
}

/// 这个目录是不是 loom 的根（用真实文件做判据，不靠层数）
fn is_loom_root(dir: &Path) -> bool {
    dir.join("backend").join("server").join("__main__.py").exists()
}

/// 从 start 开始向上最多 max 层，找第一个看起来像 loom 根的目录
fn walk_up(start: &Path, max: usize) -> Option<PathBuf> {
    let mut cur = Some(start);
    for _ in 0..=max {
        let dir = cur?;
        if is_loom_root(dir) {
            return Some(dir.canonicalize().unwrap_or_else(|_| dir.to_path_buf()));
        }
        cur = dir.parent();
    }
    None
}
