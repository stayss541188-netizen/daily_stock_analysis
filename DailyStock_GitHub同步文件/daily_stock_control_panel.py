from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import END, BooleanVar, Listbox, StringVar, Tk, messagebox
from tkinter import scrolledtext, ttk


PROJECT_DIR = Path(__file__).resolve().parent
WATCHLIST_FILE = PROJECT_DIR / "watchlist_stocks.csv"
REPORTS_DIR = PROJECT_DIR / "reports"
LOGS_DIR = PROJECT_DIR / "logs"
LOGO_ICO = PROJECT_DIR / "assets" / "daily_stock_logo.ico"
LOGO_PNG = PROJECT_DIR / "assets" / "daily_stock_logo.png"

PYTHON_EXE = Path(
    r"C:\Users\stays\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
)
if not PYTHON_EXE.exists():
    PYTHON_EXE = Path(sys.executable)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SECRET_FIELD_NAMES = {
    "DINGTALK_SECRET",
    "DINGTALK_WEBHOOK_URL",
    "YUNAI_AUTHORIZATION",
    "YUNAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "FINNHUB_API_KEY",
    "ALPHAVANTAGE_API_KEY",
    "TICKFLOW_API_KEY",
    "TUSHARE_TOKEN",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "AIHUBMIX_KEY",
    "FEISHU_WEBHOOK_URL",
    "FEISHU_WEBHOOK_SECRET",
    "WECHAT_WEBHOOK_URL",
    "PUSHPLUS_TOKEN",
}

SECRET_NAME_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|WEBHOOK[_-]?URL|AUTHORIZATION)[A-Z0-9_]*\s*[:=]\s*)([^\r\n]+)"
)
JWT_RE = re.compile(r"Bearer\s+[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", re.I)
BARE_JWT_RE = re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b")
URL_SECRET_REPLACEMENTS = [
    re.compile(r"https://oapi\.dingtalk\.com/robot/send\?access_token=[^\s)]+", re.I),
    re.compile(r"https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[^\s)]+", re.I),
    re.compile(r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[^\s)]+", re.I),
    re.compile(r"SEC[A-Za-z0-9+/=_-]{8,}", re.I),
]


def read_env_file() -> dict[str, str]:
    env_path = PROJECT_DIR / ".env"
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def write_env_values(values: dict[str, str]) -> None:
    env_path = PROJECT_DIR / ".env"
    original = env_path.read_text(encoding="utf-8", errors="replace").splitlines() if env_path.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in original:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in values:
            output.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in values.items():
        if key not in seen:
            output.append(f"{key}={value}")
    env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def parse_stock_codes(value: str) -> list[str]:
    seen: set[str] = set()
    codes: list[str] = []
    for item in re.split(r"[\s,;，、；]+", value or ""):
        code = item.strip()
        if not code:
            continue
        key = code.upper()
        if key not in seen:
            seen.add(key)
            codes.append(code)
    return codes


def status_text(ok: bool) -> str:
    return "已就绪" if ok else "未配置"


class DailyStockControlPanel:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("DailyStock")
        self.root.geometry("980x720")
        self.root.minsize(900, 640)

        self.log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.running_process: subprocess.Popen[str] | None = None
        self.command_buttons: list[ttk.Button] = []
        self.secret_values = self._load_secret_values()

        env_values = read_env_file()
        self.stock_list_var = StringVar(value=env_values.get("STOCK_LIST") or "600519")
        self.watch_stock_var = StringVar(value="")
        self.test_stock_var = StringVar(value=parse_stock_codes(self.stock_list_var.get())[0] if self.stock_list_var.get() else "600519")
        self.force_run_var = BooleanVar(value=True)
        self.single_notify_var = BooleanVar(value=(env_values.get("SINGLE_STOCK_NOTIFY", "true").lower() == "true"))
        self.status_var = StringVar(value="")

        self.logo_image = None
        self._configure_style()
        self._set_window_icon()
        self._build_ui()
        self.refresh_status(log=False)
        self._poll_log_queue()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _configure_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("Subtle.TLabel", foreground="#667085")
        style.configure("Primary.TButton", padding=(10, 7))
        style.configure("Danger.TButton", padding=(10, 7))

    def _set_window_icon(self) -> None:
        try:
            if LOGO_ICO.exists():
                self.root.iconbitmap(str(LOGO_ICO))
        except Exception:
            pass
        try:
            if LOGO_PNG.exists():
                from tkinter import PhotoImage

                self.logo_image = PhotoImage(file=str(LOGO_PNG))
                self.root.iconphoto(True, self.logo_image)
        except Exception:
            self.logo_image = None

    def _load_secret_values(self) -> list[str]:
        values: list[str] = []
        for key, value in read_env_file().items():
            upper = key.upper()
            if (
                upper in SECRET_FIELD_NAMES
                or upper.endswith("_API_KEY")
                or upper.endswith("_TOKEN")
                or upper.endswith("_SECRET")
                or upper.endswith("_WEBHOOK_URL")
                or upper.endswith("_AUTHORIZATION")
            ):
                cleaned = value.strip()
                if len(cleaned) >= 6:
                    values.append(cleaned)
        return values

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=14)
        container.pack(fill="both", expand=True)

        header = ttk.Frame(container)
        header.pack(fill="x")
        logo_holder = ttk.Frame(header, width=56, height=56)
        logo_holder.pack(side="left", padx=(0, 12))
        logo_holder.pack_propagate(False)
        if self.logo_image is not None:
            ttk.Label(logo_holder, image=self.logo_image).pack(expand=True)
        else:
            ttk.Label(logo_holder, text="DS", font=("Segoe UI", 18, "bold")).pack(expand=True)
        ttk.Label(header, text="DailyStock 控制面板", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="本地运行，不依靠 GitHub。普通观望股只进报告，重点信号才推钉钉。", style="Subtle.TLabel").pack(anchor="w")

        status_frame = ttk.LabelFrame(container, text="状态", padding=10)
        status_frame.pack(fill="x", pady=(12, 10))
        ttk.Label(status_frame, textvariable=self.status_var).pack(side="left", fill="x", expand=True)
        ttk.Button(status_frame, text="刷新", command=self.refresh_status).pack(side="right")

        self._build_watchlist_ui(container)
        self._build_actions_ui(container)
        self._build_log_ui(container)
        self.log("控制面板已启动。", "success")

    def _build_watchlist_ui(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="自选股", padding=10)
        frame.pack(fill="x", pady=(0, 10))
        frame.columnconfigure(0, weight=1)

        list_frame = ttk.Frame(frame)
        list_frame.grid(row=0, column=0, rowspan=3, sticky="ew", padx=(0, 10))
        list_frame.columnconfigure(0, weight=1)
        self.watchlist_box = Listbox(list_frame, height=5, exportselection=False)
        self.watchlist_box.grid(row=0, column=0, sticky="ew")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.watchlist_box.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.watchlist_box.configure(yscrollcommand=scrollbar.set)
        self.watchlist_box.bind("<<ListboxSelect>>", self.on_watchlist_select)

        ttk.Label(frame, text="代码").grid(row=0, column=1, sticky="w")
        ttk.Entry(frame, textvariable=self.watch_stock_var, width=18).grid(row=0, column=2, sticky="w", padx=(6, 10))
        ttk.Button(frame, text="添加", command=self.add_watch_stock).grid(row=0, column=3, sticky="ew", padx=2)
        ttk.Button(frame, text="修改", command=self.update_watch_stock).grid(row=0, column=4, sticky="ew", padx=2)
        ttk.Button(frame, text="删除", command=self.remove_watch_stock).grid(row=0, column=5, sticky="ew", padx=2)
        ttk.Button(frame, text="上移", command=lambda: self.move_watch_stock(-1)).grid(row=1, column=3, sticky="ew", padx=2, pady=(6, 0))
        ttk.Button(frame, text="下移", command=lambda: self.move_watch_stock(1)).grid(row=1, column=4, sticky="ew", padx=2, pady=(6, 0))
        ttk.Button(frame, text="保存自选", command=self.save_watchlist_config).grid(row=1, column=5, sticky="ew", padx=2, pady=(6, 0))
        ttk.Label(frame, text="批量修改可直接编辑下方清单文件。", style="Subtle.TLabel").grid(row=2, column=1, columnspan=5, sticky="w", pady=(8, 0))
        self.load_watchlist_from_input(log_result=False)

    def _build_actions_ui(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="核心功能", padding=10)
        frame.pack(fill="x", pady=(0, 10))
        for col in range(5):
            frame.columnconfigure(col, weight=1)

        buttons = [
            ("今日重点筛选", self.run_focus_review),
            ("运行个股分析", self.run_stock_analysis),
            ("运行大盘复盘", self.run_market_review),
            ("完整分析", self.run_full_analysis),
            ("测试单股推送", self.test_single_stock_push),
            ("测试行情", self.test_yunai_quote),
            ("打开报告", lambda: self.open_path(REPORTS_DIR, create_dir=True)),
            ("打开日志", lambda: self.open_path(LOGS_DIR, create_dir=True)),
            ("自选清单", lambda: self.open_path(WATCHLIST_FILE)),
            ("项目文件夹", lambda: self.open_path(PROJECT_DIR)),
            ("停止任务", self.stop_running_task),
        ]
        for index, (label, command) in enumerate(buttons):
            button = ttk.Button(frame, text=label, command=command, style="Primary.TButton")
            button.grid(row=index // 5, column=index % 5, sticky="ew", padx=4, pady=4)
            if label not in {"打开报告", "打开日志", "自选清单", "项目文件夹", "停止任务"}:
                self.command_buttons.append(button)

        options = ttk.Frame(frame)
        options.grid(row=(len(buttons) + 4) // 5, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        ttk.Label(options, text="测试股票").pack(side="left")
        ttk.Entry(options, textvariable=self.test_stock_var, width=14).pack(side="left", padx=(6, 16))
        ttk.Checkbutton(options, text="强制运行", variable=self.force_run_var).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(options, text="重点单股推送", variable=self.single_notify_var).pack(side="left")

    def _build_log_ui(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="运行日志", padding=8)
        frame.pack(fill="both", expand=True)
        self.log_text = scrolledtext.ScrolledText(
            frame,
            wrap="word",
            height=16,
            font=("Consolas", 10),
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_config("info", foreground="#202124")
        self.log_text.tag_config("success", foreground="#16803c")
        self.log_text.tag_config("error", foreground="#b00020")
        self.log_text.tag_config("muted", foreground="#667085")

    def _watchlist_codes(self) -> list[str]:
        return [str(self.watchlist_box.get(index)) for index in range(self.watchlist_box.size())]

    def _set_watchlist_codes(self, codes: list[str]) -> None:
        self.watchlist_box.delete(0, END)
        for code in codes:
            self.watchlist_box.insert(END, code)
        self.stock_list_var.set(",".join(codes))

    def _selected_watchlist_index(self) -> int | None:
        selection = self.watchlist_box.curselection()
        return int(selection[0]) if selection else None

    def on_watchlist_select(self, _event: object | None = None) -> None:
        index = self._selected_watchlist_index()
        if index is not None:
            self.watch_stock_var.set(str(self.watchlist_box.get(index)))

    def load_watchlist_from_input(self, log_result: bool = True) -> None:
        codes = parse_stock_codes(self.stock_list_var.get())
        self._set_watchlist_codes(codes)
        if log_result:
            self.log(f"已载入自选股：{len(codes)} 只", "success")

    def add_watch_stock(self) -> None:
        codes = self._watchlist_codes()
        existing = {code.upper() for code in codes}
        added = 0
        for code in parse_stock_codes(self.watch_stock_var.get()):
            if code.upper() not in existing:
                codes.append(code)
                existing.add(code.upper())
                added += 1
        self._set_watchlist_codes(codes)
        self.log(f"已添加 {added} 只；当前 {len(codes)} 只", "success" if added else "muted")

    def update_watch_stock(self) -> None:
        index = self._selected_watchlist_index()
        replacement = parse_stock_codes(self.watch_stock_var.get())
        if index is None or not replacement:
            self.log("请先选中股票，并输入新的代码。", "error")
            return
        codes = self._watchlist_codes()
        new_code = replacement[0]
        if any(i != index and code.upper() == new_code.upper() for i, code in enumerate(codes)):
            self.log(f"{new_code} 已存在。", "error")
            return
        codes[index] = new_code
        self._set_watchlist_codes(codes)
        self.watchlist_box.selection_set(index)
        self.log(f"已修改为 {new_code}", "success")

    def remove_watch_stock(self) -> None:
        index = self._selected_watchlist_index()
        if index is None:
            self.log("请先选中要删除的股票。", "error")
            return
        codes = self._watchlist_codes()
        removed = codes.pop(index)
        self._set_watchlist_codes(codes)
        if codes:
            self.watchlist_box.selection_set(min(index, len(codes) - 1))
        self.log(f"已删除 {removed}；当前 {len(codes)} 只", "success")

    def move_watch_stock(self, direction: int) -> None:
        index = self._selected_watchlist_index()
        if index is None:
            self.log("请先选中股票。", "error")
            return
        codes = self._watchlist_codes()
        new_index = index + direction
        if new_index < 0 or new_index >= len(codes):
            return
        codes[index], codes[new_index] = codes[new_index], codes[index]
        self._set_watchlist_codes(codes)
        self.watchlist_box.selection_set(new_index)
        self.watchlist_box.see(new_index)

    def save_watchlist_config(self) -> None:
        codes = self._watchlist_codes()
        write_env_values(
            {
                "STOCK_LIST": ",".join(codes),
                "SINGLE_STOCK_NOTIFY": "true" if self.single_notify_var.get() else "false",
                "SINGLE_STOCK_NOTIFY_FILTER": "important",
            }
        )
        self.stock_list_var.set(",".join(codes))
        self.secret_values = self._load_secret_values()
        self.refresh_status(log=False)
        self.log(f"已保存自选股到 .env：{len(codes)} 只。", "success")

    def redact(self, text: str) -> str:
        redacted = text
        for secret in self.secret_values:
            redacted = redacted.replace(secret, "[已隐藏]")
        for pattern in URL_SECRET_REPLACEMENTS:
            redacted = pattern.sub("[已隐藏]", redacted)
        redacted = JWT_RE.sub("Bearer [已隐藏]", redacted)
        redacted = BARE_JWT_RE.sub("[已隐藏]", redacted)

        def replace_named_secret(match: re.Match[str]) -> str:
            value = match.group(2).strip()
            if value in {"已配置", "未配置", "configured", "not configured", "true", "false"}:
                return match.group(0)
            if len(value) >= 8 or "://" in value or value.lower().startswith("bearer "):
                return f"{match.group(1)}[已隐藏]"
            return match.group(0)

        return SECRET_NAME_RE.sub(replace_named_secret, redacted)

    def log(self, message: str, tag: str = "info") -> None:
        self.log_queue.put((self.redact(message), tag))

    def _poll_log_queue(self) -> None:
        try:
            while True:
                message, tag = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", message + "\n", tag)
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    def _set_running(self, is_running: bool) -> None:
        state = "disabled" if is_running else "normal"
        for button in self.command_buttons:
            button.configure(state=state)

    def refresh_status(self, log: bool = True) -> None:
        values = read_env_file()
        codes = parse_stock_codes(values.get("STOCK_LIST") or "")
        notification_ok = bool(values.get("DINGTALK_WEBHOOK_URL") or values.get("WECHAT_WEBHOOK_URL") or values.get("FEISHU_WEBHOOK_URL") or values.get("PUSHPLUS_TOKEN"))
        yunai_ok = bool(values.get("YUNAI_AUTHORIZATION") or values.get("YUNAI_API_KEY"))
        ai_ok = bool(values.get("DEEPSEEK_API_KEY") or values.get("GEMINI_API_KEY") or values.get("OPENAI_API_KEY") or values.get("AIHUBMIX_KEY") or values.get("ANSPIRE_API_KEYS"))
        notify_filter = values.get("SINGLE_STOCK_NOTIFY_FILTER") or "all"
        self.status_var.set(
            f"自选 {len(codes)} 只 | 通知 {status_text(notification_ok)} | YunAI {status_text(yunai_ok)} | AI {status_text(ai_ok)} | 推送筛选 {notify_filter}"
        )
        if log:
            self.log(self.status_var.get(), "success" if notification_ok and yunai_ok and ai_ok else "muted")

    def build_base_args(self) -> list[str]:
        args: list[str] = []
        if self.force_run_var.get():
            args.append("--force-run")
        if self.single_notify_var.get():
            args.append("--single-notify")
        return args

    def normalized_stock_list(self) -> str:
        codes = self._watchlist_codes() if hasattr(self, "watchlist_box") else []
        return ",".join(codes or parse_stock_codes(self.stock_list_var.get()))

    def normalized_test_stock(self) -> str:
        return self.test_stock_var.get().strip() or "600519"

    def run_python(self, script_args: list[str], title: str) -> None:
        if self.running_process and self.running_process.poll() is None:
            messagebox.showwarning("任务正在运行", "当前已有任务运行中，请等它结束后再启动新任务。")
            return
        command = [str(PYTHON_EXE)] + script_args
        self._set_running(True)
        self.log("=" * 72, "muted")
        self.log(f"开始：{title}", "info")
        self.log("命令：" + self.redact(" ".join(command)), "muted")
        threading.Thread(target=self._run_command_worker, args=(command,), daemon=True).start()

    def _run_command_worker(self, command: list[str]) -> None:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        start = time.time()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(PROJECT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            self.running_process = process
            assert process.stdout is not None
            for line in process.stdout:
                self.log(line.rstrip(), "info")
            code = process.wait()
            elapsed = time.time() - start
            if code == 0:
                self.log(f"完成：退出码 0，用时 {elapsed:.1f}s", "success")
            else:
                self.log(f"失败：退出码 {code}，用时 {elapsed:.1f}s", "error")
        except Exception as exc:
            self.log(f"执行失败：{exc}", "error")
        finally:
            self.running_process = None
            self.root.after(0, lambda: self._set_running(False))

    def stop_running_task(self) -> None:
        if self.running_process and self.running_process.poll() is None:
            self.running_process.terminate()
            self.log("已请求停止当前任务。", "error")
        else:
            self.log("当前没有正在运行的任务。", "muted")

    def test_single_stock_push(self) -> None:
        stock = self.normalized_test_stock()
        self.run_python(["main.py", "--test-notify", "--stocks", stock], f"测试单股推送：{stock}")

    def test_yunai_quote(self) -> None:
        stock = self.normalized_test_stock()
        code = (
            "from src.config import setup_env; setup_env(); "
            "from data_provider.yunai_fetcher import YunaiFetcher; "
            f"stock={stock!r}; f=YunaiFetcher(); "
            "print('YunAI:', '已配置' if f.has_configured_credentials() else '未配置'); "
            "q=f.get_realtime_quote(stock); "
            "import sys; sys.exit('YunAI 未返回有效行情') if q is None else None; "
            "print(f'{stock}: price={q.price}, change_pct={q.change_pct}, source={q.source.value}')"
        )
        self.run_python(["-c", code], f"测试行情：{stock}")

    def run_focus_review(self) -> None:
        self.save_watchlist_config()
        args = ["scripts/daily_focus_review.py", "--push", "--run-market", "--run-ai"]
        if self.force_run_var.get():
            args.append("--force-run")
        self.run_python(args, "今日重点筛选")

    def run_stock_analysis(self) -> None:
        args = ["main.py", "--no-market-review"] + self.build_base_args()
        stocks = self.normalized_stock_list()
        if stocks:
            args += ["--stocks", stocks]
        self.save_watchlist_config()
        self.run_python(args, "运行个股分析")

    def run_market_review(self) -> None:
        args = ["main.py", "--market-review"]
        if self.force_run_var.get():
            args.append("--force-run")
        self.run_python(args, "运行大盘复盘")

    def run_full_analysis(self) -> None:
        args = ["main.py"] + self.build_base_args()
        stocks = self.normalized_stock_list()
        if stocks:
            args += ["--stocks", stocks]
        self.save_watchlist_config()
        self.run_python(args, "完整分析")

    def open_path(self, path: Path, create_dir: bool = False) -> None:
        try:
            if create_dir:
                path.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                self.log(f"路径不存在：{path}", "error")
                return
            os.startfile(str(path))  # type: ignore[attr-defined]
            self.log(f"打开：{path}", "info")
        except Exception as exc:
            self.log(f"打开失败：{exc}", "error")

    def _on_close(self) -> None:
        if self.running_process and self.running_process.poll() is None:
            if not messagebox.askyesno("任务仍在运行", "当前任务仍在运行，确定关闭吗？"):
                return
        self.root.destroy()


def main() -> None:
    root = Tk()
    DailyStockControlPanel(root)
    root.mainloop()


if __name__ == "__main__":
    main()
