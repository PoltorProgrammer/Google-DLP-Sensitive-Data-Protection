import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import os
import re
import sys
import time
import json
import queue
import shutil
import hashlib
import platform
import threading
import subprocess

import fitz  # PyMuPDF

# Note: Integration with Google Cloud DLP (Data Loss Prevention)
from dlp_processor import ClinicalDocumentProcessor

APP_VERSION = "2.4.0"
HISTORY_FILE = "performance_history.json"
CONFIG_FILE = "config.json"
AUDIT_FILE = "audit_log.jsonl"

# Status suffixes appended to listbox entries, e.g. "report.pdf (Success - verified)"
STATUS_TAG_RE = re.compile(r"\s\((Completed|Success|Failed|Simulated)[^)]*\)$")

TRANSLATION_REGION = "us-central1"  # Google document translation is US/global only


class LocalFileProcessorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Clinical Document Processor - Google DLP")
        self.root.geometry("640x780")

        self.source_folder = ""
        self.is_processing = False
        self.should_stop = False
        self.history_calibrated = False
        self.keywords_mapping = {None: []}  # None key stores Global keywords
        self.current_selected_file = None
        self.files_to_process = []
        self.processed_files = []
        self.failed_files = []
        self.review_files = []
        self.verification_status = {}  # filename -> "verified" | "review" | None

        # Thread-safe UI plumbing: worker threads enqueue widget updates,
        # the Tk main loop drains them (Tk is not thread-safe).
        self.ui_queue = queue.Queue()
        self.config_lock = threading.Lock()
        self.audit_lock = threading.Lock()

        # Window Close Protocol
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # Load Config first
        self.config_load_error = None
        self.config = self.load_config()
        self.ensure_config_defaults()

        # Estimation State & Buffering
        app_settings = self.config.get('app_settings', {})
        metrics = app_settings.get('performance_metrics', {})

        self.stats = {
            "avg_time_per_page": metrics.get("avg_time_per_page", 2.5),
            "avg_time_per_page_save": metrics.get("avg_time_per_page_save", 0.05),
            "avg_time_per_mb_load": metrics.get("avg_time_per_mb_load", 0.1),
            "last_ping": metrics.get("avg_ping_ms", 50),
            "total_pages_global": 0,
            "pages_done_global": 0,
            "total_size_mb_global": 0,
            "size_done_mb_global": 0
        }

        # Load History to refine statistics
        self.load_history()

        self.current_ping = 50
        self.gpu_name = "Detecting..."

        self.measurement_buffers = {
            "page_times": [],
            "save_times_per_mb": []
        }
        self.steps_since_calibration = 0

        self.detect_environment()

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def load_config(self):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            self.config_load_error = "config.json not found - using defaults."
            return {}
        except Exception as e:
            self.config_load_error = f"config.json could not be read ({e}) - using defaults. Fix the file to restore your settings!"
            return {}

    def ensure_config_defaults(self):
        """Fill any missing config keys so the rest of the app can rely on them."""
        cfg = self.config
        gc = cfg.setdefault("google_cloud", {})
        gc.setdefault("project_id", "")
        gc.setdefault("location", "europe-west6")
        gc.setdefault("service_account_key_file", "credentials.json")

        oo = cfg.setdefault("output_options", {})
        oo.setdefault("redaction", True)
        oo.setdefault("redaction_iterations", 1)
        oo.setdefault("selectable_text_copy", True)
        oo.setdefault("non_selectable_text_copy", False)
        oo.setdefault("translation_redaction_iterations", 0)
        oo.setdefault("generate_full_translated_document", False)

        tr = cfg.setdefault("translation", {})
        tr.setdefault("enabled", False)
        tr.setdefault("target_language_code", "en")

        ap = cfg.setdefault("app_settings", {})
        ap.setdefault("output_folder", "")       # "" = <source>/processed
        ap.setdefault("overwrite_policy", "skip")  # skip | version | overwrite
        ap.setdefault("verification_scan", True)
        ap.setdefault("audit_log", True)

    def save_config(self):
        with self.config_lock:
            try:
                tmp = CONFIG_FILE + ".tmp"
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(self.config, f, indent=4)
                os.replace(tmp, CONFIG_FILE)
            except Exception as e:
                print(f"Error saving config: {e}")

    # ------------------------------------------------------------------
    # Performance history / estimation (regression model)
    # ------------------------------------------------------------------

    def load_history(self):
        """Load past performance data and calculate Linear Regression coefficients (y = mx + b)"""
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, 'r') as f:
                    history = json.load(f)
                    if len(history) >= 2:
                        samples = history[-50:]  # Use last 50 for relevancy

                        # 1. Regression for Page Processing (m1, b1)
                        m1, b1 = self.calculate_regression([(s['pages'], s['pages'] * s['page_avg']) for s in samples])
                        self.stats["slope_page"] = m1
                        self.stats["intercept_page"] = b1

                        # 2. Regression for Compiling/Saving (m2, b2)
                        m2, b2 = self.calculate_regression([(s['pages'], s['pages'] * s.get('save_pg_avg', 0.05)) for s in samples])
                        self.stats["slope_save"] = m2
                        self.stats["intercept_save"] = b2

                        # 3. Regression for Translation (m3, b3) - Based on Payload Size MB
                        trans_samples = [(s['trans_mb_total'], s['trans_time_total']) for s in samples if s.get('trans_mb_total', 0) > 0]
                        m3, b3 = self.calculate_regression(trans_samples) if trans_samples else (1.5, 0.5)
                        self.stats["slope_trans"] = m3
                        self.stats["intercept_trans"] = b3

                        # 4. Load MB average (mostly linear)
                        self.stats["avg_time_per_mb_load"] = sum(s.get('load_mb_avg', 0.1) for s in samples) / len(samples)
                        self.stats["last_ping"] = sum(s['ping'] for s in samples) / len(samples)
                        self.history_calibrated = True
                        print(f"Regression Calibration: {round(m1, 2)}s/pg (redact) + {round(m3, 2)}s/mb (trans)")
        except Exception as e:
            print(f"Could not load performance history: {e}")

    def calculate_regression(self, data):
        """Perform Ordinary Least Squares: returns (slope, intercept)"""
        n = len(data)
        if n < 2: return 2.5, 0.5  # Defaults

        sum_x = sum(d[0] for d in data)
        sum_y = sum(d[1] for d in data)
        sum_xx = sum(d[0]**2 for d in data)
        sum_xy = sum(d[0]*d[1] for d in data)

        denominator = (n * sum_xx - sum_x**2)
        if denominator == 0: return (sum_y/sum_x if sum_x != 0 else 2.5), 0.5

        slope = (n * sum_xy - sum_x * sum_y) / denominator
        intercept = (sum_y - slope * sum_x) / n

        # Clamp to realistic values
        return max(0.1, slope), max(0.1, intercept)

    def append_history_sample(self, pages, size_mb, page_avg, save_pg_avg, load_mb_avg):
        """Store a new document's data into the history file"""
        sample = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pages": pages,
            "size_mb": round(size_mb, 2),
            "page_avg": round(page_avg, 3),
            "save_pg_avg": round(save_pg_avg, 3),
            "load_mb_avg": round(load_mb_avg, 3),
            "trans_mb_total": round(self.stats.get("current_doc_trans_mb", 0), 2),
            "trans_time_total": round(self.stats.get("current_doc_trans_time", 0), 2),
            "trans_flatten_time": round(self.stats.get("current_doc_trans_flatten_time", 0), 2),
            "trans_api_time": round(self.stats.get("current_doc_trans_api_time", 0), 2),
            "ping": self.current_ping,
            "gpu": self.gpu_name
        }
        history = []
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE, 'r') as f:
                    history = json.load(f)
        except Exception as e:
            print(f"Could not read performance history: {e}")

        history.append(sample)
        try:
            with open(HISTORY_FILE, 'w') as f:
                json.dump(history, f, indent=4)
        except Exception as e:
            print(f"Could not write performance history: {e}")

    # ------------------------------------------------------------------
    # Widgets
    # ------------------------------------------------------------------

    def create_widgets(self):
        # 0. Security Posture Strip
        posture_frame = tk.Frame(self.root, bd=1, relief=tk.GROOVE)
        posture_frame.pack(fill=tk.X, padx=10, pady=(8, 0))

        self.posture_var = tk.StringVar(value="")
        self.posture_label = tk.Label(posture_frame, textvariable=self.posture_var,
                                      font=("Segoe UI", 8, "bold"), anchor="w")
        self.posture_label.pack(side=tk.LEFT, padx=6, pady=2)

        tk.Button(posture_frame, text="⚙ Settings", command=self.open_settings,
                  font=("Segoe UI", 8), bg="#eeeeee").pack(side=tk.RIGHT, padx=4, pady=2)

        # 1. Folder Selection
        select_frame = tk.Frame(self.root, pady=8)
        select_frame.pack(fill=tk.X, padx=10)

        self.btn_select = tk.Button(select_frame, text="Select Data Folder", command=self.select_folder)
        self.btn_select.pack(side=tk.LEFT)

        self.lbl_folder = tk.Label(select_frame, text="No folder selected", fg="gray")
        self.lbl_folder.pack(side=tk.LEFT, padx=10)

        # 1.2 Output Destination Row
        out_frame = tk.Frame(self.root)
        out_frame.pack(fill=tk.X, padx=10)

        tk.Label(out_frame, text="Save results to:", font=("Segoe UI", 9)).pack(side=tk.LEFT)
        self.output_var = tk.StringVar(value="(select a data folder first)")
        tk.Label(out_frame, textvariable=self.output_var, fg="#0277bd",
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=6)

        tk.Button(out_frame, text="Open", command=self.open_output_folder,
                  font=("Segoe UI", 8)).pack(side=tk.RIGHT, padx=2)
        tk.Button(out_frame, text="Change...", command=self.change_output_folder,
                  font=("Segoe UI", 8)).pack(side=tk.RIGHT, padx=2)

        # 1.5 Custom Keywords Section (Chips UI)
        kw_section = tk.Frame(self.root, pady=5)
        kw_section.pack(fill=tk.X, padx=10)

        self.lbl_kw_target = tk.Label(kw_section, text="Global Redaction Keywords:", font=("Segoe UI", 9, "bold"), fg="#0277bd")
        self.lbl_kw_target.pack(anchor="w")

        input_frame = tk.Frame(kw_section)
        input_frame.pack(fill=tk.X, pady=2)

        self.entry_keyword = tk.Entry(input_frame, font=("Segoe UI", 10))
        self.entry_keyword.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.entry_keyword.bind("<Return>", self.add_keyword_event)
        self.entry_keyword.bind("<KeyRelease-,>", self.add_keyword_event)

        self.btn_add_kw = tk.Button(input_frame, text="Add", command=self.add_keyword, bg="#e1e1e1")
        self.btn_add_kw.pack(side=tk.LEFT, padx=5)

        # Chip Container
        self.chip_container = tk.Frame(kw_section)
        self.chip_container.pack(fill=tk.X, pady=5)

        tk.Label(kw_section, text="Press Enter or Comma to add. Click [X] to remove.", fg="gray", font=("Segoe UI", 8)).pack(anchor="w")

        # 2. Controls
        ctrl_frame = tk.Frame(self.root, pady=10)
        ctrl_frame.pack(fill=tk.X, padx=10)

        self.btn_start = tk.Button(ctrl_frame, text="Start Batch Processing", command=self.start_processing_thread, state=tk.DISABLED, bg="#dddddd", font=("Segoe UI", 10, "bold"))
        self.btn_start.pack(side=tk.LEFT)

        self.btn_stop = tk.Button(ctrl_frame, text="Stop", command=self.confirm_stop, state=tk.DISABLED, bg="#f5f5f5", font=("Segoe UI", 10, "bold"))
        self.btn_stop.pack(side=tk.LEFT, padx=10)

        # 3. Status Lists
        list_frame = tk.Frame(self.root, pady=10)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10)

        # Pending Files
        tk.Label(list_frame, text="Documents to Process:").grid(row=0, column=0, sticky="w")
        self.list_pending = tk.Listbox(list_frame, height=8, width=40, exportselection=False)
        self.list_pending.grid(row=1, column=0, padx=5, sticky="news")
        self.list_pending.bind("<<ListboxSelect>>", self.on_file_selected)

        # Processed Files
        tk.Label(list_frame, text="Completed Documents:").grid(row=0, column=1, sticky="w")
        self.list_processed = tk.Listbox(list_frame, height=8, width=40, exportselection=False)
        self.list_processed.grid(row=1, column=1, padx=5, sticky="news")
        self.list_processed.bind("<<ListboxSelect>>", self.on_file_selected)

        list_frame.columnconfigure(0, weight=1)
        list_frame.columnconfigure(1, weight=1)

        # 4. Log Box
        log_frame = tk.Frame(self.root, pady=5)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10)

        log_header = tk.Frame(log_frame)
        log_header.pack(fill=tk.X)

        tk.Label(log_header, text="Execution Log:").pack(side=tk.LEFT)

        self.time_var = tk.StringVar()
        self.time_var.set("")
        self.time_bar = tk.Label(log_header, textvariable=self.time_var, fg="#0277bd", font=("Consolas", 9, "bold"))
        self.time_bar.pack(side=tk.RIGHT)

        self.text_log = tk.Text(log_frame, height=20, state=tk.DISABLED)
        self.text_log.pack(fill=tk.BOTH, expand=True)

        # 5. Status Bar
        status_frame = tk.Frame(self.root, bd=1, relief=tk.SUNKEN)
        status_frame.pack(side=tk.BOTTOM, fill=tk.X)

        self.status_var = tk.StringVar()
        self.status_var.set("Ready")
        self.status_bar = tk.Label(status_frame, textvariable=self.status_var, anchor=tk.W)
        self.status_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # 6. Env Info
        self.env_var = tk.StringVar(value="GPU: Scanning... | Ping: --ms")
        self.env_bar = tk.Label(self.root, textvariable=self.env_var, fg="#757575", font=("Segoe UI", 8))
        self.env_bar.pack(side=tk.BOTTOM, anchor="e", padx=10)

        self.refresh_posture()

        # Start draining worker-thread UI updates on the main loop
        self.root.after(100, self._pump_ui_queue)

        if self.config_load_error:
            self.log_message(f"⚠ {self.config_load_error}")

        # Credentials hygiene check (runs on main thread, may show a dialog)
        self.root.after(800, self.check_credentials_security)

    # ------------------------------------------------------------------
    # Thread-safe UI plumbing
    # ------------------------------------------------------------------

    def ui_call(self, fn):
        """Run fn on the Tk main thread (immediately if already there)."""
        if threading.current_thread() is threading.main_thread():
            try:
                fn()
            except Exception as e:
                print(f"UI update error: {e}")
        else:
            self.ui_queue.put(fn)

    def _pump_ui_queue(self):
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                try:
                    fn()
                except Exception as e:
                    print(f"UI update error: {e}")
        except queue.Empty:
            pass
        self.root.after(100, self._pump_ui_queue)

    # ------------------------------------------------------------------
    # Window / control events
    # ------------------------------------------------------------------

    def on_closing(self):
        """Handle window X button click"""
        if self.is_processing:
            if messagebox.askyesno("Exit?", "Processing is still active. Are you sure you want to stop everything and exit?"):
                self.should_stop = True
                self.root.destroy()
        else:
            self.root.destroy()

    def confirm_stop(self):
        """Handle Stop button click"""
        if self.is_processing:
            if messagebox.askyesno("Stop Processing", "Are you sure you want to stop the batch processing?"):
                self.should_stop = True
                self.log_message("Stopping... finishing current document.")

    def detect_environment(self):
        """Detect GPU and Ping to adjust estimation formula (platform-aware, no shell)."""
        def task():
            system = platform.system()

            # 1. Detect GPU
            try:
                if system == "Windows":
                    res = subprocess.check_output(
                        ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_VideoController).Name"],
                        stderr=subprocess.DEVNULL, timeout=25).decode(errors="ignore")
                    lines = [l.strip() for l in res.splitlines() if l.strip()]
                    self.gpu_name = lines[0] if lines else "Unknown GPU"
                elif system == "Darwin":
                    self.gpu_name = "Apple GPU"
                else:
                    self.gpu_name = "Unknown GPU"
            except Exception:
                self.gpu_name = "Unknown GPU"

            # 2. Detect Ping to Google DLP (1 packet for speed)
            try:
                if system == "Windows":
                    cmd = ["ping", "dlp.googleapis.com", "-n", "1"]
                else:
                    cmd = ["ping", "-c", "1", "dlp.googleapis.com"]
                res = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=15).decode(errors="ignore")
                if "time=" in res:
                    ping = res.split("time=")[1].split("ms")[0].strip()
                    self.current_ping = int(float(ping))
                elif "Average =" in res:
                    ping = res.split("Average =")[1].split("ms")[0].strip()
                    self.current_ping = int(float(ping))
            except Exception:
                self.current_ping = 100  # Default if blocked

            self.ui_call(lambda: self.env_var.set(f"GPU: {self.gpu_name} | Ping: {self.current_ping}ms"))

        threading.Thread(target=task, daemon=True).start()

    # ------------------------------------------------------------------
    # Logging & estimation
    # ------------------------------------------------------------------

    def log_message(self, message, metadata=None):
        """Callable from any thread. Metadata is a structured dict (never parsed from text)."""
        if metadata:
            self.handle_metadata(metadata)

        self.ui_call(lambda: self._log_to_widgets(message))

        # Trigger Recalibration every 10 log messages
        self.steps_since_calibration += 1
        if self.steps_since_calibration >= 10:
            self.recalibrate_estimation()
            self.steps_since_calibration = 0

        if self.is_processing:
            self.ui_call(self.update_estimation_ui)

    def _log_to_widgets(self, message):
        self.status_var.set(message)
        self.text_log.config(state=tk.NORMAL)

        # Smart Scroll: Check if we are at the very bottom before adding content
        y_scroll = self.text_log.yview()
        is_at_bottom = y_scroll[1] >= 0.995

        self.text_log.insert(tk.END, f"{time.strftime('%H:%M:%S')} - {message}\n")

        if is_at_bottom:
            self.text_log.see(tk.END)

        self.text_log.config(state=tk.DISABLED)

    def recalibrate_estimation(self):
        """Refreshes the regression model with new samples from this session"""
        if self.measurement_buffers["page_times"] or self.measurement_buffers["save_times_per_mb"]:
            # Trigger a full history reload and regression calc
            self.load_history()

    def save_performance_metrics(self):
        """Saves current learned stats to config file for next startup"""
        if 'app_settings' not in self.config: self.config['app_settings'] = {}
        self.config['app_settings']['performance_metrics'] = {
            "avg_time_per_page": round(self.stats["avg_time_per_page"], 3),
            "avg_time_per_page_save": round(self.stats["avg_time_per_page_save"], 3),
            "avg_time_per_mb_load": round(self.stats["avg_time_per_mb_load"], 3),
            "avg_ping_ms": self.current_ping,
            "last_gpu": self.gpu_name
        }
        self.save_config()

    def handle_metadata(self, metadata):
        """Pure stats bookkeeping - no widget access (may run on worker threads)."""
        now = time.time()
        if "page_done" in metadata:
            self.stats["pages_done_global"] += 1
            if hasattr(self, '_page_start_time'):
                duration = now - self._page_start_time
                self.measurement_buffers["page_times"].append(duration)
            self._page_start_time = now

        elif "save_start" in metadata:
            self._save_start_time = now
            self._save_size_mb = metadata["save_start"]

        elif "save_done" in metadata:
            if hasattr(self, '_save_start_time') and getattr(self, '_current_doc_pages', 0) > 0:
                duration = now - self._save_start_time
                time_per_page_save = duration / self._current_doc_pages
                self.measurement_buffers["save_times_per_mb"].append(time_per_page_save)
                self.stats["size_done_mb_global"] += self._save_size_mb

                if self.measurement_buffers["page_times"]:
                    # Look at the last block of pages for this doc
                    pages_to_avg = self.measurement_buffers["page_times"][-self._current_doc_pages:]
                    doc_page_avg = sum(pages_to_avg) / len(pages_to_avg) if pages_to_avg else 2.5

                    # Use a fixed guess for load time per MB if it's the first run
                    load_avg = self.stats.get("avg_time_per_mb_load", 0.1)

                    self.append_history_sample(
                        self._current_doc_pages,
                        self._save_size_mb,
                        doc_page_avg,
                        time_per_page_save,
                        load_avg
                    )
                    # Reset translation counters for next doc
                    self.stats["current_doc_trans_mb"] = 0
                    self.stats["current_doc_trans_time"] = 0
                    self.stats["current_doc_trans_flatten_time"] = 0
                    self.stats["current_doc_trans_api_time"] = 0

        elif "trans_api_start" in metadata:
            self._trans_api_chunk_start = now
            chunk_size_mb = metadata["trans_api_start"] / (1024 * 1024)
            self.stats["current_doc_trans_mb"] = self.stats.get("current_doc_trans_mb", 0) + chunk_size_mb

        elif "trans_api_done" in metadata:
            if hasattr(self, '_trans_api_chunk_start'):
                duration = now - self._trans_api_chunk_start
                self.stats["current_doc_trans_api_time"] = self.stats.get("current_doc_trans_api_time", 0) + duration
                self.stats["current_doc_trans_time"] = self.stats.get("current_doc_trans_time", 0) + duration

        elif "trans_flatten_start" in metadata:
            self._trans_flatten_start = now

        elif "trans_flatten_done" in metadata:
            if hasattr(self, '_trans_flatten_start'):
                duration = now - self._trans_flatten_start
                self.stats["current_doc_trans_flatten_time"] = self.stats.get("current_doc_trans_flatten_time", 0) + duration
                self.stats["current_doc_trans_time"] = self.stats.get("current_doc_trans_time", 0) + duration

        elif "pages" in metadata:
            # Document started
            self._page_start_time = now
            self._current_doc_pages = metadata["pages"]
            # Track load time from document initialization start to first metadata page report
            if hasattr(self, '_doc_load_start_time'):
                load_duration = now - self._doc_load_start_time
                if getattr(self, '_save_size_mb', 0) > 0:
                    self.stats["avg_time_per_mb_load"] = (self.stats["avg_time_per_mb_load"] * 0.9) + ((load_duration / self._save_size_mb) * 0.1)

    def update_estimation_ui(self):
        # We can update the UI even before hitting Start if we have global stats
        elapsed = (time.time() - self.start_time_global) if hasattr(self, 'start_time_global') else 0

        pages_left = self.stats["total_pages_global"] - self.stats["pages_done_global"]
        mb_left = self.stats["total_size_mb_global"] - self.stats["size_done_mb_global"]

        # Safeguard for start
        if pages_left < 0: pages_left = 0

        # REGRESSION FORMULA (y = mx + b)
        ping_ratio = self.current_ping / max(1, self.stats.get("last_ping", 50))
        ping_ratio = max(0.5, min(2.0, ping_ratio))

        # Use slopes and intercepts from stats
        m1 = self.stats.get("slope_page", 2.5)
        m2 = self.stats.get("slope_save", 0.05)
        b1 = self.stats.get("intercept_page", 0.5)
        b2 = self.stats.get("intercept_save", 0.5)

        # Apply balanced ping correction
        balanced_m1 = (m1 * 0.6 * ping_ratio) + (m1 * 0.4)

        # Load cost (MB based)
        load_time = mb_left * self.stats.get("avg_time_per_mb_load", 0.1)

        # Total Remaining = Remaining Docs * Fixed_Costs + Remaining Pages * Variable_Costs
        files_left = len(self.files_to_process)

        # Predicted Translation Cost
        # Since we use Flattened Image (Zoom 2.0), each page is ~1.5MB to 2.5MB
        avg_mb_per_page = 2.0
        projected_trans_mb = pages_left * avg_mb_per_page if self.config.get('translation', {}).get('enabled', False) else 0
        m3 = self.stats.get("slope_trans", 1.5)
        b3 = self.stats.get("intercept_trans", 0.5)
        translation_time = (projected_trans_mb * m3) + (files_left * b3)

        remaining = load_time + (files_left * (b1 + b2)) + (pages_left * (balanced_m1 + m2)) + translation_time

        if not self.history_calibrated:
            status_text = "Est. Remaining: Calibrating..."
        else:
            status_text = f"Est. Remaining: {self.format_time(remaining)}"

        if self.is_processing and pages_left > 0:
             status_text = f"Elapsed: {self.format_time(elapsed)} | {status_text}"

        self.time_var.set(status_text)

    def format_time(self, seconds):
        if seconds < 0: return "Calculating..."
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h:02}h {m:02}m {s:02}s"

    # ------------------------------------------------------------------
    # File list / keyword chips
    # ------------------------------------------------------------------

    @staticmethod
    def strip_status(item):
        return STATUS_TAG_RE.sub("", item)

    def update_file_ui_status(self, filename, status_text):
        """Moves a file from pending to processed listbox (thread-safe)."""
        def apply():
            items = self.list_pending.get(0, tk.END)
            for idx, item in enumerate(items):
                if item == filename:
                    self.list_pending.delete(idx)
                    break

            self.list_processed.insert(tk.END, f"{filename} ({status_text})")
            self.list_processed.see(tk.END)
        self.ui_call(apply)

    def on_file_selected(self, event):
        w = event.widget
        selection = w.curselection()
        if selection:
            filename = self.strip_status(w.get(selection[0]))

            self.current_selected_file = filename
            self.lbl_kw_target.config(text=f"Keywords for: {filename}", fg="#e65100")
        else:
            self.current_selected_file = None
            self.lbl_kw_target.config(text="Global Redaction Keywords:", fg="#0277bd")

        self.render_chips()

    def add_keyword_event(self, event):
        self.add_keyword()
        return "break"

    def add_keyword(self):
        val = self.entry_keyword.get().replace(",", "").strip()
        target = self.current_selected_file
        if val:
            if target not in self.keywords_mapping:
                self.keywords_mapping[target] = []
            if val not in self.keywords_mapping[target]:
                self.keywords_mapping[target].append(val)
                self.render_chips()
        self.entry_keyword.delete(0, tk.END)

    def remove_keyword(self, val):
        target = self.current_selected_file
        # Check current specific target or global
        if target in self.keywords_mapping and val in self.keywords_mapping[target]:
            self.keywords_mapping[target].remove(val)
        elif val in self.keywords_mapping.get(None, []):
             self.keywords_mapping[None].remove(val)
        self.render_chips()

    def render_chips(self):
        for widget in self.chip_container.winfo_children():
            widget.destroy()

        row, col = 0, 0
        max_cols = 4

        # Show both global AND target-specific chips
        to_render = []
        if None in self.keywords_mapping:
            for kw in self.keywords_mapping[None]:
                to_render.append((kw, True))  # is_global=True

        if self.current_selected_file and self.current_selected_file in self.keywords_mapping:
            for kw in self.keywords_mapping[self.current_selected_file]:
                # Don't duplicate if already in global
                if kw not in [x[0] for x in to_render]:
                    to_render.append((kw, False))

        for kw, is_global in to_render:
            bg_color = "#e1f5fe" if is_global else "#fff3e0"
            border_color = "#03a9f4" if is_global else "#ff9800"

            chip = tk.Frame(self.chip_container, bg=bg_color, padx=5, pady=2, highlightbackground=border_color, highlightthickness=1)
            chip.grid(row=row, column=col, padx=3, pady=3, sticky="w")

            label_text = f"{kw} (G)" if is_global else kw
            tk.Label(chip, text=label_text, bg=bg_color, font=("Segoe UI", 9)).pack(side=tk.LEFT)

            btn_del = tk.Button(chip, text="×", bg=bg_color, fg="red", bd=0,
                               command=lambda v=kw: self.remove_keyword(v), font=("Segoe UI", 10, "bold"),
                               activebackground=bg_color, cursor="hand2")
            btn_del.pack(side=tk.LEFT, padx=(5, 0))

            col += 1
            if col >= max_cols:
                col = 0
                row += 1

    # ------------------------------------------------------------------
    # Output destination & save pipeline
    # ------------------------------------------------------------------

    def get_output_folder(self):
        custom = self.config.get("app_settings", {}).get("output_folder", "")
        if custom:
            return custom
        if self.source_folder:
            return os.path.join(self.source_folder, "processed")
        return ""

    def refresh_output_label(self):
        folder = self.get_output_folder()
        self.output_var.set(folder if folder else "(select a data folder first)")

    def change_output_folder(self):
        folder = filedialog.askdirectory(title="Choose where anonymized files are saved")
        if folder:
            if self.source_folder and os.path.abspath(folder) == os.path.abspath(self.source_folder):
                messagebox.showwarning(
                    "Output Folder",
                    "Saving results directly into the source folder mixes originals and anonymized copies.\n"
                    "Please pick a subfolder or a separate folder."
                )
                return
            self.config["app_settings"]["output_folder"] = folder
            self.save_config()
            self.refresh_output_label()
            self.log_message(f"Output folder set to: {folder}")
            self.warn_if_cloud_synced()
            if self.source_folder:
                self.load_files()

    def open_output_folder(self):
        folder = self.get_output_folder()
        if not folder or not os.path.isdir(folder):
            messagebox.showinfo("Output Folder", "No output folder exists yet - process a document first.")
            return
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            messagebox.showerror("Error", f"Could not open folder: {e}")

    def expected_outputs(self, filename):
        """The output files this filename should produce under current settings."""
        oo = self.config.get("output_options", {})
        out_folder = self.get_output_folder()
        names = []
        if filename.lower().endswith(".pdf"):
            if oo.get("selectable_text_copy", True):
                names.append(f"anonymized_{filename}")
            if oo.get("non_selectable_text_copy", False):
                names.append(f"anonymized_flattened_{filename}")
            if not names:
                names.append(f"anonymized_{filename}")
        else:
            names.append(f"anonymized_{filename}")
        return [os.path.join(out_folder, n) for n in names]

    def is_valid_output(self, path):
        """A file only counts as 'done' if it exists, is non-empty and (for PDFs) opens cleanly."""
        try:
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                return False
            if path.lower().endswith(".pdf"):
                with fitz.open(path) as doc:
                    return len(doc) > 0
            return True
        except Exception:
            return False

    @staticmethod
    def unique_path(path):
        base, ext = os.path.splitext(path)
        n = 1
        candidate = f"{base} ({n}){ext}"
        while os.path.exists(candidate):
            n += 1
            candidate = f"{base} ({n}){ext}"
        return candidate

    def save_bytes_atomic(self, path, data, expected_pages=None):
        """Validate -> write temp -> fsync -> atomic rename. Never leaves a half-written file
        under the final name, so 'Completed' can be trusted. If expected_pages is given,
        the output must have exactly that many pages."""
        if not data:
            raise ValueError("refusing to save empty output")
        if path.lower().endswith(".pdf"):
            with fitz.open("pdf", data) as check_doc:  # raises if corrupt
                if len(check_doc) == 0:
                    raise ValueError("output PDF has 0 pages")
                if expected_pages is not None and len(check_doc) != expected_pages:
                    raise ValueError(f"output has {len(check_doc)} pages, expected {expected_pages}")

        tmp_path = path + ".part"
        with open(tmp_path, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)

    def save_output(self, path, data, expected_pages=None):
        """Save honoring the overwrite policy. Returns the final path, or None on failure."""
        policy = self.config.get("app_settings", {}).get("overwrite_policy", "skip")
        final_path = path
        if os.path.exists(path):
            if policy == "skip" and self.is_valid_output(path):
                self.log_message(f"Skipped (already exists): {os.path.basename(path)}")
                return path
            if policy == "version":
                final_path = self.unique_path(path)
        try:
            self.save_bytes_atomic(final_path, data, expected_pages=expected_pages)
            return final_path
        except Exception as e:
            self.log_message(f"SAVE FAILED for {os.path.basename(final_path)}: {e}")
            return None

    # ------------------------------------------------------------------
    # Audit trail
    # ------------------------------------------------------------------

    @staticmethod
    def sha256_file(path):
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(1 << 20), b''):
                h.update(chunk)
        return h.hexdigest()

    def write_audit_entry(self, entry):
        """Append one JSON line per processed document. Contains hashes and counts,
        never document content or keyword values."""
        folder = self.get_output_folder()
        if not folder:
            return
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, AUDIT_FILE)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self.audit_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    # ------------------------------------------------------------------
    # Folder selection & scan
    # ------------------------------------------------------------------

    @staticmethod
    def detect_cloud_sync(path):
        """Reuse the engine's single implementation - no drift between UIs."""
        from batch_core import BatchEngine
        return BatchEngine.detect_cloud_sync(path)

    def warn_if_cloud_synced(self):
        for where, p in (("source folder", self.source_folder), ("output folder", self.get_output_folder())):
            service = self.detect_cloud_sync(p)
            if service:
                self.log_message(f"⚠ The {where} appears to be inside a {service}-synced location - "
                                 "files there are copied to the cloud outside this app's control.")

    def select_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.source_folder = folder
            self.lbl_folder.config(text=folder)
            self.refresh_output_label()
            self.warn_if_cloud_synced()
            self.load_files()
            self.btn_start.config(state=tk.NORMAL, bg="#90ee90")

    def load_files(self):
        self.files_to_process = []
        self.list_pending.delete(0, tk.END)
        self.list_processed.delete(0, tk.END)
        self.processed_files = []

        # Reset Stats for new workload analysis
        self.stats["total_pages_global"] = 0
        self.stats["total_size_mb_global"] = 0
        self.stats["pages_done_global"] = 0
        self.stats["size_done_mb_global"] = 0

        policy = self.config.get("app_settings", {}).get("overwrite_policy", "skip")

        try:
            raw_files = [f for f in os.listdir(self.source_folder) if not f.startswith('.') and f.lower().endswith(('.pdf', '.png', '.jpg', '.jpeg', '.tiff'))]

            if not raw_files:
                self.log_message("No supported documents found in selected folder.")
                return

            self.log_message(f"Analyzing {len(raw_files)} documents for workload estimation...")

            def scan_task():
                for f in raw_files:
                    full_path = os.path.join(self.source_folder, f)
                    if os.path.isdir(full_path): continue

                    # A file only counts as done if ALL its expected outputs exist AND validate.
                    done = False
                    if policy == "skip":
                        expected = self.expected_outputs(f)
                        if expected and all(self.is_valid_output(p) for p in expected):
                            done = True
                        else:
                            for p in expected:
                                if os.path.exists(p) and not self.is_valid_output(p):
                                    self.log_message(f"⚠ Found invalid/incomplete output for {f} - it will be re-processed.")
                                    break

                    if done:
                        self.processed_files.append(f)
                        self.ui_call(lambda name=f: self.list_processed.insert(tk.END, f"{name} (Completed)"))
                    else:
                        # Update Weight & Pages only for documents we will actually process
                        size_mb = os.path.getsize(full_path) / (1024 * 1024)
                        self.stats["total_size_mb_global"] += size_mb

                        try:
                            if f.lower().endswith('.pdf'):
                                with fitz.open(full_path) as doc:
                                    self.stats["total_pages_global"] += len(doc)
                            else:
                                self.stats["total_pages_global"] += 1
                        except Exception:
                            self.stats["total_pages_global"] += 1

                        self.files_to_process.append(f)
                        self.ui_call(lambda name=f: self.list_pending.insert(tk.END, name))

                    # Update UI Estimation incrementally
                    self.ui_call(self.update_estimation_ui)

                self.log_message(f"Ready! Added {len(self.files_to_process)} files. Total Workload: {self.stats['total_pages_global']} pgs | {round(self.stats['total_size_mb_global'], 1)} MB")

            threading.Thread(target=scan_task, daemon=True).start()

        except Exception as e:
            self.log_message(f"Error listing files: {e}")
            messagebox.showerror("Error", f"Failed to list files: {e}")

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    def start_processing_thread(self):
        # Run in thread to not freeze UI during long API calls
        thread = threading.Thread(target=self.start_processing, daemon=True)
        thread.start()

    def status_tag_for(self, filename, success):
        if not success:
            return "Failed"
        v = self.verification_status.get(filename)
        if v == "review":
            return "Success ⚠ REVIEW"
        if v == "verified":
            return "Success ✔ verified"
        return "Success"

    def start_processing(self):
        if not self.files_to_process:
            self.ui_call(lambda: messagebox.showinfo("Info", "No files to process."))
            return

        self.is_processing = True
        self.should_stop = False
        self.failed_files = []
        self.review_files = []
        self.verification_status = {}
        self.ui_call(lambda: self.btn_start.config(state=tk.DISABLED))
        self.ui_call(lambda: self.btn_stop.config(state=tk.NORMAL, bg="#ffcdd2"))

        # Reset Stats for the current run
        self.stats["pages_done_global"] = 0
        self.stats["size_done_mb_global"] = 0

        # We already pre-scanned, so we can start immediately
        self.log_message(f"Starting batch: {self.stats['total_pages_global']} pages total.")

        # Get Custom Terms from chips
        global_kws = self.keywords_mapping.get(None, [])

        files_snapshot = list(self.files_to_process)
        self.start_time_global = time.time()

        app_settings = self.config.get('app_settings', {}) or {}
        output_options = self.config.get("output_options", {}) or {}
        trans_config = self.config.get('translation', {}) or {}
        cloud_config = self.config.get('google_cloud', {}) or {}

        total_files = len(files_snapshot)
        success_count = 0

        try:
            # REAL MODE - Direct DLP (Transient)
            self.log_message("Initializing DLP Processor...")
            self.log_message(f"🔒 Data region: {cloud_config.get('location', 'global')} - all DLP inspection is pinned to this region.")
            if trans_config.get('enabled', False):
                self.log_message(f"⚠ Translation is ON: redacted copies will be sent to Google Translation in {TRANSLATION_REGION} (US).")

            processor = ClinicalDocumentProcessor(
                project_id=cloud_config.get('project_id'),
                location=cloud_config.get('location', 'global'),
                credentials_file=cloud_config.get('service_account_key_file'),
                log_callback=self.log_message,
                translation_location=TRANSLATION_REGION
            )

            # Setup output folder
            output_folder = self.get_output_folder()
            os.makedirs(output_folder, exist_ok=True)

            for idx, filename in enumerate(files_snapshot):
                if self.should_stop:
                    self.log_message("Processing halted by user.")
                    break

                self.log_message(f"Processing {idx+1}/{total_files}: {filename}")
                file_path = os.path.join(self.source_folder, filename)
                file_size = os.path.getsize(file_path) / (1024 * 1024)

                # Mark start of doc for load time tracking
                self._doc_load_start_time = time.time()
                self._save_size_mb = file_size

                success = False
                saved_paths = []
                doc_stats = {}
                verification = None
                error_text = None
                input_sha = None
                merged_terms = []

                try:
                    input_sha = self.sha256_file(file_path)

                    # Merge keywords for this specific file
                    specific_kws = self.keywords_mapping.get(filename, [])
                    merged_terms = list(set(global_kws + specific_kws))

                    # Direct RAM-only processing.
                    # Returns dictionary: {"selectable": bytes, "non_selectable": bytes, "stats": dict}
                    results = processor.process_document(file_path, custom_terms=merged_terms, output_config=output_options)

                    if results:
                        doc_stats = results.get("stats", {})
                        # PDF outputs must have exactly as many pages as the input
                        expected_pages = doc_stats.get("pages") if filename.lower().endswith(".pdf") else None

                        # 1. Save Selectable (Standard Anonymized)
                        if results.get("selectable"):
                            target = os.path.join(output_folder, f"anonymized_{filename}")
                            saved = self.save_output(target, results["selectable"], expected_pages=expected_pages)
                            if saved:
                                saved_paths.append(("selectable", saved))
                                success = True

                        # 2. Save Non-Selectable (Flattened/Undigitalized)
                        if results.get("non_selectable"):
                            target = os.path.join(output_folder, f"anonymized_flattened_{filename}")
                            saved = self.save_output(target, results["non_selectable"], expected_pages=expected_pages)
                            if saved:
                                saved_paths.append(("non_selectable", saved))
                                success = True

                        # 3. VERIFICATION SCAN (safety net on the finished output)
                        if success and app_settings.get("verification_scan", True) and filename.lower().endswith('.pdf') and results.get("selectable"):
                            try:
                                self.log_message(f"Running verification scan on output of {filename}...")
                                verification = processor.verify_output(results["selectable"], merged_terms)
                                if verification["dlp_findings"] == 0 and verification["keyword_hits"] == 0:
                                    self.verification_status[filename] = "verified"
                                    self.log_message(f"✔ Verification passed: no residual sensitive text detected in {filename}.")
                                else:
                                    self.verification_status[filename] = "review"
                                    self.review_files.append(filename)
                                    self.log_message(f"⚠ Verification: {verification['dlp_findings']} possible InfoType hit(s) and {verification['keyword_hits']} keyword hit(s) remain in {filename}. Manual review recommended.")
                            except Exception as ve:
                                self.log_message(f"Verification scan failed for {filename}: {ve}")

                        # 4. Translation Step (applies to Selectable version preferentially)
                        doc_to_translate = results.get("selectable") or results.get("non_selectable")

                        if trans_config.get('enabled', False) and filename.lower().endswith('.pdf') and doc_to_translate:
                            try:
                                target_lang = trans_config.get('target_language_code', 'en')
                                # translate_document returns a list of (label, bytes)
                                trans_results = processor.translate_document(doc_to_translate, target_language=target_lang)

                                # REDACTION ON TRANSLATION
                                trans_redact_iters = output_options.get("translation_redaction_iterations", 0)
                                if trans_redact_iters > 0:
                                    self.log_message(f"Re-redacting translated document ({trans_redact_iters} passes)...")
                                    trans_output_config = {
                                        "redaction": True,
                                        "redaction_iterations": trans_redact_iters,
                                        "selectable_text_copy": True,
                                        "non_selectable_text_copy": False
                                    }

                                    new_trans_results = []
                                    for label, trans_bytes in trans_results:
                                        try:
                                            # Re-redact the translated bytes with the SAME full config
                                            redacted_trans_dict = processor.process_bytes(trans_bytes, custom_terms=merged_terms, output_config=trans_output_config)
                                            final_bytes = redacted_trans_dict.get("selectable") or redacted_trans_dict.get("non_selectable") or trans_bytes
                                            new_trans_results.append((label, final_bytes))
                                        except Exception as re_err:
                                            self.log_message(f"Failed to re-redact translation chunk {label}: {re_err}")
                                            new_trans_results.append((label, trans_bytes))  # Fallback
                                    trans_results = new_trans_results

                                if len(trans_results) == 1 and trans_results[0][0] == "":
                                    # Case A: Small document (<= 1 chunk) - Save normally in output folder
                                    _, trans_bytes = trans_results[0]
                                    trans_target = os.path.join(output_folder, f"translated_{target_lang}_{filename}")
                                    saved = self.save_output(trans_target, trans_bytes)
                                    if saved:
                                        saved_paths.append(("translated", saved))
                                        self.log_message(f"Translated copy saved: {os.path.basename(saved)}")
                                else:
                                    # Case B: Large document - Save in a dedicated subfolder
                                    folder_base = os.path.splitext(filename)[0]
                                    subfolder_name = f"{target_lang}_anonymized_{folder_base}"
                                    subfolder_path = os.path.join(output_folder, subfolder_name)
                                    os.makedirs(subfolder_path, exist_ok=True)

                                    chunk_bytes_list = []
                                    for label, trans_bytes in trans_results:
                                        # Naming convention: 00-20_translated_en_filename.pdf
                                        chunk_filename = f"{label}_translated_{target_lang}_{filename}"
                                        saved = self.save_output(os.path.join(subfolder_path, chunk_filename), trans_bytes)
                                        if saved:
                                            saved_paths.append(("translated_chunk", saved))
                                        chunk_bytes_list.append(trans_bytes)

                                    self.log_message(f"Large document split into {len(trans_results)} translated chunks in: {subfolder_name}")

                                    # MERGE OPTION
                                    if output_options.get("generate_full_translated_document", False):
                                        try:
                                            merged_bytes = processor.merge_pdf_bytes(chunk_bytes_list)
                                            full_name = f"FULL_translated_{target_lang}_{filename}"
                                            saved = self.save_output(os.path.join(subfolder_path, full_name), merged_bytes)
                                            if saved:
                                                saved_paths.append(("translated_full", saved))
                                                self.log_message(f"Merged full translated document saved: {full_name}")
                                        except Exception as me:
                                            self.log_message(f"Error merging translated chunks: {me}")

                            except Exception as te:
                                self.log_message(f"Translation error: {str(te)}")

                        if not saved_paths:
                            self.log_message(f"Completed {filename} but no output was saved - check Output settings.")
                    else:
                         self.log_message(f"Completed {filename} but no content returned?")

                except Exception as e:
                    error_text = str(e)
                    self.log_message(f"Failed {filename}: {error_text[:120]}")

                # AUDIT TRAIL (hashes and counts only - no content, no keywords)
                if app_settings.get("audit_log", True):
                    try:
                        self.write_audit_entry({
                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "app_version": APP_VERSION,
                            "input": {"name": filename, "sha256": input_sha, "size_mb": round(file_size, 2)},
                            "outputs": [
                                {"kind": kind, "name": os.path.basename(p), "sha256": self.sha256_file(p)}
                                for kind, p in saved_paths
                            ],
                            "settings": {
                                "dlp_region": cloud_config.get("location", "global"),
                                "redaction": output_options.get("redaction", True),
                                "redaction_iterations": output_options.get("redaction_iterations", 1),
                                "custom_terms_count": len(merged_terms),
                                "translation_enabled": bool(trans_config.get("enabled", False)),
                            },
                            "processing_stats": doc_stats,
                            "verification": verification,
                            "status": "success" if success else "failed",
                            "error": error_text,
                        })
                    except Exception as ae:
                        self.log_message(f"Could not write audit entry: {ae}")

                if success:
                    success_count += 1
                else:
                    self.failed_files.append(filename)

                # Update UI status immediately after each file
                self.update_file_ui_status(filename, self.status_tag_for(filename, success))
                self.ui_call(self.update_estimation_ui)

                # Persist metrics after each document so progress isn't lost on cancel
                self.save_performance_metrics()

            # Final persistence
            self.save_performance_metrics()

            elapsed = time.time() - self.start_time_global
            self.log_message(f"Batch Processing Complete! ({success_count} success, {len(self.failed_files)} failed, {len(self.review_files)} to review)")
            self.ui_call(lambda: self.time_var.set(f"Finished in {self.format_time(elapsed)}"))

            failed_snapshot = list(self.failed_files)
            review_snapshot = list(self.review_files)
            self.ui_call(lambda: self.show_batch_summary(total_files, success_count, failed_snapshot, review_snapshot, elapsed))

        except Exception as e:
            full_error = str(e)
            print(f"FULL ERROR TRACEBACK: {full_error}")
            self.log_message(f"Error: {full_error}")

        finally:
            self.is_processing = False
            self.should_stop = False
            self.files_to_process = []
            self.ui_call(lambda: self.btn_start.config(state=tk.NORMAL))
            self.ui_call(lambda: self.btn_stop.config(state=tk.DISABLED, bg="#f5f5f5"))

    # ------------------------------------------------------------------
    # Batch summary dialog
    # ------------------------------------------------------------------

    def show_batch_summary(self, total, success_count, failed_files, review_files, elapsed):
        win = tk.Toplevel(self.root)
        win.title("Batch Summary")
        win.transient(self.root)
        win.geometry("520x440")

        tk.Label(win, text="Batch Complete", font=("Segoe UI", 14, "bold")).pack(pady=(15, 5))
        tk.Label(win, text=f"{success_count} of {total} documents processed successfully",
                 font=("Segoe UI", 11)).pack()
        tk.Label(win, text=f"Time: {self.format_time(elapsed)}", fg="#555555").pack(pady=2)
        tk.Label(win, text=f"Saved to: {self.get_output_folder()}", fg="#0277bd",
                 wraplength=480).pack(pady=2)

        if review_files:
            tk.Label(win, text=f"⚠ {len(review_files)} document(s) flagged for manual review (possible residual data):",
                     fg="#e65100", font=("Segoe UI", 9, "bold")).pack(pady=(8, 0))
        if failed_files:
            tk.Label(win, text=f"✖ {len(failed_files)} document(s) failed:",
                     fg="#b71c1c", font=("Segoe UI", 9, "bold")).pack(pady=(4, 0))

        if failed_files or review_files:
            lb = tk.Listbox(win, height=8)
            for f in review_files:
                lb.insert(tk.END, f"REVIEW  -  {f}")
            for f in failed_files:
                lb.insert(tk.END, f"FAILED  -  {f}")
            lb.pack(fill=tk.BOTH, expand=True, padx=15, pady=6)
        else:
            tk.Label(win, text="✔ All outputs saved and verified.", fg="#1b5e20",
                     font=("Segoe UI", 10, "bold")).pack(pady=12)

        btns = tk.Frame(win)
        btns.pack(pady=10)
        tk.Button(btns, text="Open Output Folder", command=self.open_output_folder,
                  bg="#e1f5fe").pack(side=tk.LEFT, padx=5)
        if failed_files:
            tk.Button(btns, text="Retry Failed", command=lambda: self.retry_failed(win, failed_files),
                      bg="#fff3e0").pack(side=tk.LEFT, padx=5)
        tk.Button(btns, text="Close", command=win.destroy).pack(side=tk.LEFT, padx=5)

    def retry_failed(self, win, failed_files):
        for f in failed_files:
            items = self.list_processed.get(0, tk.END)
            for idx in range(len(items) - 1, -1, -1):
                if self.strip_status(items[idx]) == f:
                    self.list_processed.delete(idx)
            if f not in self.files_to_process:
                self.files_to_process.append(f)
                self.list_pending.insert(tk.END, f)
        win.destroy()
        self.btn_start.config(state=tk.NORMAL, bg="#90ee90")
        self.log_message(f"{len(failed_files)} failed file(s) queued for retry. Press Start when ready.")

    # ------------------------------------------------------------------
    # Security posture & settings dialog
    # ------------------------------------------------------------------

    def refresh_posture(self):
        gc = self.config.get("google_cloud", {})
        oo = self.config.get("output_options", {})
        tr = self.config.get("translation", {})
        ap = self.config.get("app_settings", {})

        redaction_on = oo.get("redaction", True)
        parts = [f"Region: {gc.get('location', 'global')}"]
        parts.append(f"Redaction: {'ON x' + str(oo.get('redaction_iterations', 1)) if redaction_on else 'OFF!'}")
        parts.append(f"Translation: {'ON -> ' + TRANSLATION_REGION + ' (US)' if tr.get('enabled', False) else 'OFF'}")
        parts.append(f"Verify: {'ON' if ap.get('verification_scan', True) else 'OFF'}")
        parts.append(f"Audit: {'ON' if ap.get('audit_log', True) else 'OFF'}")

        danger = not redaction_on
        warn = tr.get('enabled', False) or not ap.get('verification_scan', True)
        color = "#b71c1c" if danger else ("#e65100" if warn else "#1b5e20")
        prefix = "⚠ " if (danger or warn) else "🔒 "

        self.posture_var.set(prefix + "  |  ".join(parts))
        self.posture_label.config(fg=color)

    def open_settings(self):
        if self.is_processing:
            messagebox.showinfo("Settings", "Settings are locked while processing is running.")
            return

        oo = self.config.get("output_options", {})
        tr = self.config.get("translation", {})
        ap = self.config.get("app_settings", {})

        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.transient(self.root)
        win.grab_set()
        win.geometry("540x640")

        # --- Variables ---
        out_mode = tk.StringVar(value="custom" if ap.get("output_folder") else "default")
        out_custom = tk.StringVar(value=ap.get("output_folder", ""))
        policy = tk.StringVar(value=ap.get("overwrite_policy", "skip"))
        v_sel = tk.BooleanVar(value=oo.get("selectable_text_copy", True))
        v_flat = tk.BooleanVar(value=oo.get("non_selectable_text_copy", False))
        v_red = tk.BooleanVar(value=oo.get("redaction", True))
        v_iter = tk.IntVar(value=oo.get("redaction_iterations", 1))
        v_tr = tk.BooleanVar(value=tr.get("enabled", False))
        v_lang = tk.StringVar(value=tr.get("target_language_code", "en"))
        v_triter = tk.IntVar(value=oo.get("translation_redaction_iterations", 0))
        v_full = tk.BooleanVar(value=oo.get("generate_full_translated_document", False))
        v_verify = tk.BooleanVar(value=ap.get("verification_scan", True))
        v_audit = tk.BooleanVar(value=ap.get("audit_log", True))

        body = tk.Frame(win, padx=12, pady=8)
        body.pack(fill=tk.BOTH, expand=True)

        # --- Where to save ---
        f_out = tk.LabelFrame(body, text=" Where to save results ", padx=8, pady=4)
        f_out.pack(fill=tk.X, pady=4)

        tk.Radiobutton(f_out, text="Next to the source folder (<source>/processed)",
                       variable=out_mode, value="default").pack(anchor="w")
        row_custom = tk.Frame(f_out)
        row_custom.pack(fill=tk.X)
        tk.Radiobutton(row_custom, text="Custom folder:", variable=out_mode, value="custom").pack(side=tk.LEFT)
        e_custom = tk.Entry(row_custom, textvariable=out_custom)
        e_custom.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        def browse_custom():
            folder = filedialog.askdirectory(parent=win, title="Choose output folder")
            if folder:
                out_custom.set(folder)
                out_mode.set("custom")
        tk.Button(row_custom, text="Browse...", command=browse_custom).pack(side=tk.LEFT)

        # --- Overwrite policy ---
        f_pol = tk.LabelFrame(body, text=" If a result already exists ", padx=8, pady=4)
        f_pol.pack(fill=tk.X, pady=4)
        tk.Radiobutton(f_pol, text="Skip the file (don't re-process)", variable=policy, value="skip").pack(anchor="w")
        tk.Radiobutton(f_pol, text="Keep both (save with a version number)", variable=policy, value="version").pack(anchor="w")
        tk.Radiobutton(f_pol, text="Overwrite the old result", variable=policy, value="overwrite").pack(anchor="w")

        # --- Outputs ---
        f_gen = tk.LabelFrame(body, text=" Outputs to generate ", padx=8, pady=4)
        f_gen.pack(fill=tk.X, pady=4)
        tk.Checkbutton(f_gen, text="Selectable copy (searchable text via OCR overlay)", variable=v_sel).pack(anchor="w")
        tk.Checkbutton(f_gen, text="Flattened copy (image-only, no text layer)", variable=v_flat).pack(anchor="w")

        # --- Redaction ---
        f_red = tk.LabelFrame(body, text=" Redaction ", padx=8, pady=4)
        f_red.pack(fill=tk.X, pady=4)
        tk.Checkbutton(f_red, text="Redact sensitive data (DLP)", variable=v_red).pack(anchor="w")
        row_iter = tk.Frame(f_red)
        row_iter.pack(anchor="w")
        tk.Label(row_iter, text="Redaction passes:").pack(side=tk.LEFT)
        tk.Spinbox(row_iter, from_=1, to=10, textvariable=v_iter, width=4).pack(side=tk.LEFT, padx=4)
        tk.Label(row_iter, text="(more passes = stricter, slower)", fg="gray",
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)

        # --- Translation ---
        f_tr = tk.LabelFrame(body, text=" Translation ", padx=8, pady=4)
        f_tr.pack(fill=tk.X, pady=4)
        tk.Checkbutton(f_tr, text="Translate redacted documents", variable=v_tr).pack(anchor="w")
        tk.Label(f_tr, text=f"⚠ Translation is processed by Google in {TRANSLATION_REGION} (US), outside the EU.\nOnly already-redacted copies are sent.",
                 fg="#e65100", font=("Segoe UI", 8), justify="left").pack(anchor="w")
        row_lang = tk.Frame(f_tr)
        row_lang.pack(anchor="w", pady=2)
        tk.Label(row_lang, text="Target language:").pack(side=tk.LEFT)
        ttk.Combobox(row_lang, textvariable=v_lang, width=6,
                     values=["en", "de", "fr", "it", "es", "ca", "pt", "nl", "pl"]).pack(side=tk.LEFT, padx=4)
        tk.Label(row_lang, text="Re-redaction passes on translation:").pack(side=tk.LEFT, padx=(10, 0))
        tk.Spinbox(row_lang, from_=0, to=10, textvariable=v_triter, width=4).pack(side=tk.LEFT, padx=4)
        tk.Checkbutton(f_tr, text="Also merge chunked translations into one full document", variable=v_full).pack(anchor="w")

        # --- Safety ---
        f_safe = tk.LabelFrame(body, text=" Safety ", padx=8, pady=4)
        f_safe.pack(fill=tk.X, pady=4)
        tk.Checkbutton(f_safe, text="Verification scan (re-check finished outputs for residual sensitive text)", variable=v_verify).pack(anchor="w")
        tk.Checkbutton(f_safe, text="Audit trail (audit_log.jsonl with file hashes - never content or keywords)", variable=v_audit).pack(anchor="w")

        # --- Buttons ---
        def on_save():
            if not v_sel.get() and not v_flat.get():
                messagebox.showerror("Settings", "Select at least one output type (selectable or flattened).", parent=win)
                return
            if out_mode.get() == "custom" and not out_custom.get().strip():
                messagebox.showerror("Settings", "Choose a custom output folder or switch back to the default.", parent=win)
                return
            if not v_red.get():
                if not messagebox.askyesno("Are you sure?",
                        "Redaction is DISABLED. Output files will contain the ORIGINAL sensitive data.\n\nContinue anyway?",
                        icon="warning", parent=win):
                    return

            iters = max(1, min(10, int(v_iter.get() or 1)))
            triters = max(0, min(10, int(v_triter.get() or 0)))

            oo2 = self.config.setdefault("output_options", {})
            oo2["selectable_text_copy"] = bool(v_sel.get())
            oo2["non_selectable_text_copy"] = bool(v_flat.get())
            oo2["redaction"] = bool(v_red.get())
            oo2["redaction_iterations"] = iters
            oo2["translation_redaction_iterations"] = triters
            oo2["generate_full_translated_document"] = bool(v_full.get())

            tr2 = self.config.setdefault("translation", {})
            tr2["enabled"] = bool(v_tr.get())
            tr2["target_language_code"] = (v_lang.get() or "en").strip().lower()

            ap2 = self.config.setdefault("app_settings", {})
            ap2["output_folder"] = out_custom.get().strip() if out_mode.get() == "custom" else ""
            ap2["overwrite_policy"] = policy.get()
            ap2["verification_scan"] = bool(v_verify.get())
            ap2["audit_log"] = bool(v_audit.get())

            self.save_config()
            self.refresh_posture()
            self.refresh_output_label()
            self.log_message("Settings saved.")
            win.destroy()

            # Re-scan: output/overwrite settings change what counts as 'already done'
            if self.source_folder:
                self.load_files()

        btns = tk.Frame(body)
        btns.pack(pady=8)
        tk.Button(btns, text="Save", command=on_save, bg="#c8e6c9",
                  font=("Segoe UI", 10, "bold"), width=10).pack(side=tk.LEFT, padx=5)
        tk.Button(btns, text="Cancel", command=win.destroy, width=10).pack(side=tk.LEFT, padx=5)

    # ------------------------------------------------------------------
    # Credentials hygiene
    # ------------------------------------------------------------------

    @staticmethod
    def get_secure_credentials_dir():
        if sys.platform == "win32":
            base = os.environ.get("APPDATA", os.path.expanduser("~"))
            return os.path.join(base, "MediXtract")
        return os.path.join(os.path.expanduser("~"), ".config", "medixtract")

    @staticmethod
    def restrict_file_acl(path):
        """Windows: strip inheritance and broad groups so only the current user can
        read the key. Must use DOMAIN\\user - a bare username can resolve to the
        machine account when the PC shares the user's name."""
        try:
            user = os.environ.get("USERNAME") or os.getlogin()
            domain = os.environ.get("USERDOMAIN")
            principal = f"{domain}\\{user}" if domain else user
            subprocess.run(["icacls", path, "/inheritance:r", "/grant:r", f"{principal}:F"],
                           capture_output=True, timeout=15)
            subprocess.run(["icacls", path, "/remove", "BUILTIN\\Users", "NT AUTHORITY\\Authenticated Users"],
                           capture_output=True, timeout=15)
            # Never leave the key unreadable: verify, restore access if broken
            with open(path, "rb") as f:
                f.read(1)
        except PermissionError:
            subprocess.run(["icacls", path, "/grant", f"{os.environ.get('USERNAME', '')}:F"],
                           capture_output=True, timeout=15)
            print("ACL restriction rolled back - key readability restored.")
        except Exception as e:
            print(f"Could not restrict key file ACL: {e}")

    def check_credentials_security(self):
        """Warn if the service account key lives inside the (shareable) app folder
        and offer to move it to a protected per-user location."""
        try:
            gc = self.config.get("google_cloud", {})
            key_setting = gc.get("service_account_key_file", "")
            if not key_setting:
                return
            key_path = os.path.abspath(key_setting)
            if not os.path.exists(key_path):
                self.log_message("Note: no service account key found yet - configure google_cloud in Settings/config.json.")
                return

            project_dir = os.path.abspath(os.path.dirname(os.path.abspath(__file__)))
            try:
                inside_project = os.path.commonpath([key_path, project_dir]) == project_dir
            except ValueError:
                inside_project = False
            if not inside_project:
                return

            secure_dir = self.get_secure_credentials_dir()
            move = messagebox.askyesno(
                "Security Recommendation",
                "Your Google service account key (credentials.json) is stored inside the app folder.\n\n"
                "If this folder is ever zipped, shared or synced, the key travels with it and anyone "
                "holding it can use your Google Cloud project.\n\n"
                f"Move the key to a protected per-user location?\n\n{secure_dir}\n\n"
                "The app will remember the new location automatically."
            )
            if not move:
                self.log_message("⚠ Service account key left in app folder. Avoid zipping or sharing this folder.")
                return

            os.makedirs(secure_dir, exist_ok=True)
            new_path = os.path.join(secure_dir, os.path.basename(key_path))
            if os.path.exists(new_path):
                new_path = self.unique_path(new_path)
            shutil.move(key_path, new_path)
            if sys.platform == "win32":
                self.restrict_file_acl(new_path)

            gc["service_account_key_file"] = new_path
            self.config["google_cloud"] = gc
            self.save_config()
            self.log_message(f"🔒 Service account key moved to: {new_path}")
        except Exception as e:
            self.log_message(f"Credentials security check failed: {e}")


if __name__ == "__main__":
    root = tk.Tk()
    app = LocalFileProcessorApp(root)
    app.create_widgets()
    root.mainloop()