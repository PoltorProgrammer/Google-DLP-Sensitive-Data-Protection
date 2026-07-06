"""
UI-agnostic batch processing engine for the Clinical Document Processor.

Owns everything except widgets: config, performance history & time estimation,
the save pipeline (atomic writes, validation, overwrite policies), the audit
trail, credentials hygiene and the batch loop itself.

Frontends (web UI via app_webview.py) consume it through:
  - method calls (scan_folder, start_batch, stop, save_settings, ...)
  - a polled event queue (poll_events) with typed dict events:
      log, scan_item, scan_done, file_status, progress, env, summary, error
"""
import os
import re
import sys
import time
import json
import queue
import base64
import shutil
import hashlib
import platform
import threading
import subprocess

import fitz  # PyMuPDF

from dlp_processor import ClinicalDocumentProcessor

APP_VERSION = "2.5.5"
HISTORY_FILE = "performance_history.json"
CONFIG_FILE = "config.json"
AUDIT_FILE = "audit_log.jsonl"
TRANSLATION_REGION = "us-central1"  # Google document translation is US/global only

SUPPORTED_EXTS = ('.pdf', '.png', '.jpg', '.jpeg', '.tiff')


class BatchEngine:
    def __init__(self):
        self.events = queue.Queue()
        self.config_lock = threading.Lock()
        self.audit_lock = threading.Lock()

        self.source_folder = ""
        self.is_processing = False
        self.should_stop = False
        self.history_calibrated = False
        self.files_to_process = []
        self.processed_files = []
        self.failed_files = []
        self.review_files = []
        self.verification_status = {}

        self.config_load_error = None
        self.config = self.load_config()
        self.ensure_config_defaults()

        metrics = self.config.get('app_settings', {}).get('performance_metrics', {})
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
        self.load_history()

        self.current_ping = 50
        self.gpu_name = "Detecting..."
        self.measurement_buffers = {"page_times": [], "save_times_per_mb": []}
        self.steps_since_calibration = 0

        # Click-to-tag: OCR results for UNREDACTED source pages live in RAM only.
        # Persisting them to disk would leak the very text we're trying to erase.
        self._ocr_words_cache = {}
        self._ocr_processor = None

        self.detect_environment()
        if self.config_load_error:
            self.emit("log", message=f"⚠ {self.config_load_error}")

    # ------------------------------------------------------------------
    # Event queue (frontend polls this)
    # ------------------------------------------------------------------

    def emit(self, type_, **data):
        data["type"] = type_
        data["ts"] = time.strftime("%H:%M:%S")
        self.events.put(data)

    def poll_events(self, max_events=500):
        out = []
        try:
            while len(out) < max_events:
                out.append(self.events.get_nowait())
        except queue.Empty:
            pass
        return out

    def log(self, message):
        self.emit("log", message=message)

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

    def get_settings(self):
        """Settings snapshot for the UI."""
        return {
            "output_options": self.config.get("output_options", {}),
            "translation": self.config.get("translation", {}),
            "app_settings": {k: v for k, v in self.config.get("app_settings", {}).items()
                             if k != "performance_metrics"},
            "google_cloud": {"project_id": self.config.get("google_cloud", {}).get("project_id", ""),
                             "location": self.config.get("google_cloud", {}).get("location", "global")},
            "translation_region": TRANSLATION_REGION,
            "app_version": APP_VERSION,
        }

    def save_settings(self, settings):
        """Validate and persist settings coming from the UI. Returns new state."""
        oo_in = settings.get("output_options", {})
        tr_in = settings.get("translation", {})
        ap_in = settings.get("app_settings", {})

        if not oo_in.get("selectable_text_copy") and not oo_in.get("non_selectable_text_copy"):
            raise ValueError("Select at least one output type (selectable or flattened).")
        policy = ap_in.get("overwrite_policy", "skip")
        if policy not in ("skip", "version", "overwrite"):
            raise ValueError(f"Unknown overwrite policy: {policy}")

        oo = self.config.setdefault("output_options", {})
        oo["selectable_text_copy"] = bool(oo_in.get("selectable_text_copy", True))
        oo["non_selectable_text_copy"] = bool(oo_in.get("non_selectable_text_copy", False))
        oo["redaction"] = bool(oo_in.get("redaction", True))
        oo["redaction_iterations"] = max(1, min(10, int(oo_in.get("redaction_iterations", 1))))
        oo["translation_redaction_iterations"] = max(0, min(10, int(oo_in.get("translation_redaction_iterations", 0))))
        oo["generate_full_translated_document"] = bool(oo_in.get("generate_full_translated_document", False))

        tr = self.config.setdefault("translation", {})
        tr["enabled"] = bool(tr_in.get("enabled", False))
        tr["target_language_code"] = (tr_in.get("target_language_code") or "en").strip().lower()

        ap = self.config.setdefault("app_settings", {})
        ap["output_folder"] = (ap_in.get("output_folder") or "").strip()
        ap["overwrite_policy"] = policy
        ap["verification_scan"] = bool(ap_in.get("verification_scan", True))
        ap["audit_log"] = bool(ap_in.get("audit_log", True))

        self.save_config()
        self.log("Settings saved.")
        return {"posture": self.posture(), "output_folder": self.get_output_folder()}

    def posture(self):
        """Live security posture for the UI strip."""
        gc = self.config.get("google_cloud", {})
        oo = self.config.get("output_options", {})
        tr = self.config.get("translation", {})
        ap = self.config.get("app_settings", {})

        redaction_on = oo.get("redaction", True)
        danger = not redaction_on
        warn = tr.get("enabled", False) or not ap.get("verification_scan", True)
        return {
            "region": gc.get("location", "global"),
            "redaction": redaction_on,
            "redaction_iterations": oo.get("redaction_iterations", 1),
            "translation": tr.get("enabled", False),
            "translation_region": TRANSLATION_REGION,
            "verify": ap.get("verification_scan", True),
            "audit": ap.get("audit_log", True),
            "level": "danger" if danger else ("warn" if warn else "ok"),
        }

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
                        samples = history[-50:]

                        m1, b1 = self.calculate_regression([(s['pages'], s['pages'] * s['page_avg']) for s in samples])
                        self.stats["slope_page"] = m1
                        self.stats["intercept_page"] = b1

                        m2, b2 = self.calculate_regression([(s['pages'], s['pages'] * s.get('save_pg_avg', 0.05)) for s in samples])
                        self.stats["slope_save"] = m2
                        self.stats["intercept_save"] = b2

                        trans_samples = [(s['trans_mb_total'], s['trans_time_total']) for s in samples if s.get('trans_mb_total', 0) > 0]
                        m3, b3 = self.calculate_regression(trans_samples) if trans_samples else (1.5, 0.5)
                        self.stats["slope_trans"] = m3
                        self.stats["intercept_trans"] = b3

                        self.stats["avg_time_per_mb_load"] = sum(s.get('load_mb_avg', 0.1) for s in samples) / len(samples)
                        self.stats["last_ping"] = sum(s['ping'] for s in samples) / len(samples)
                        self.history_calibrated = True
        except Exception as e:
            print(f"Could not load performance history: {e}")

    def calculate_regression(self, data):
        """Perform Ordinary Least Squares: returns (slope, intercept)"""
        n = len(data)
        if n < 2: return 2.5, 0.5

        sum_x = sum(d[0] for d in data)
        sum_y = sum(d[1] for d in data)
        sum_xx = sum(d[0]**2 for d in data)
        sum_xy = sum(d[0]*d[1] for d in data)

        denominator = (n * sum_xx - sum_x**2)
        if denominator == 0: return (sum_y/sum_x if sum_x != 0 else 2.5), 0.5

        slope = (n * sum_xy - sum_x * sum_y) / denominator
        intercept = (sum_y - slope * sum_x) / n
        return max(0.1, slope), max(0.1, intercept)

    def append_history_sample(self, pages, size_mb, page_avg, save_pg_avg, load_mb_avg):
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

    def save_performance_metrics(self):
        if 'app_settings' not in self.config: self.config['app_settings'] = {}
        self.config['app_settings']['performance_metrics'] = {
            "avg_time_per_page": round(self.stats["avg_time_per_page"], 3),
            "avg_time_per_page_save": round(self.stats["avg_time_per_page_save"], 3),
            "avg_time_per_mb_load": round(self.stats["avg_time_per_mb_load"], 3),
            "avg_ping_ms": self.current_ping,
            "last_gpu": self.gpu_name
        }
        self.save_config()

    def on_processor_log(self, message, metadata=None):
        """log_callback handed to ClinicalDocumentProcessor (worker thread)."""
        if metadata:
            self.handle_metadata(metadata)
        self.emit("log", message=message)

        self.steps_since_calibration += 1
        if self.steps_since_calibration >= 10:
            if self.measurement_buffers["page_times"] or self.measurement_buffers["save_times_per_mb"]:
                self.load_history()
            self.steps_since_calibration = 0

        if self.is_processing:
            self.emit_progress()

    def handle_metadata(self, metadata):
        now = time.time()
        if "page_done" in metadata:
            self.stats["pages_done_global"] += 1
            if hasattr(self, '_page_start_time'):
                self.measurement_buffers["page_times"].append(now - self._page_start_time)
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
                    pages_to_avg = self.measurement_buffers["page_times"][-self._current_doc_pages:]
                    doc_page_avg = sum(pages_to_avg) / len(pages_to_avg) if pages_to_avg else 2.5
                    load_avg = self.stats.get("avg_time_per_mb_load", 0.1)
                    self.append_history_sample(self._current_doc_pages, self._save_size_mb,
                                               doc_page_avg, time_per_page_save, load_avg)
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
            self._page_start_time = now
            self._current_doc_pages = metadata["pages"]
            if hasattr(self, '_doc_load_start_time'):
                load_duration = now - self._doc_load_start_time
                if getattr(self, '_save_size_mb', 0) > 0:
                    self.stats["avg_time_per_mb_load"] = (self.stats["avg_time_per_mb_load"] * 0.9) + ((load_duration / self._save_size_mb) * 0.1)

    def emit_progress(self):
        elapsed = (time.time() - self.start_time_global) if hasattr(self, 'start_time_global') else 0

        pages_left = max(0, self.stats["total_pages_global"] - self.stats["pages_done_global"])
        mb_left = self.stats["total_size_mb_global"] - self.stats["size_done_mb_global"]

        ping_ratio = self.current_ping / max(1, self.stats.get("last_ping", 50))
        ping_ratio = max(0.5, min(2.0, ping_ratio))

        m1 = self.stats.get("slope_page", 2.5)
        m2 = self.stats.get("slope_save", 0.05)
        b1 = self.stats.get("intercept_page", 0.5)
        b2 = self.stats.get("intercept_save", 0.5)

        balanced_m1 = (m1 * 0.6 * ping_ratio) + (m1 * 0.4)
        load_time = mb_left * self.stats.get("avg_time_per_mb_load", 0.1)
        files_left = len(self.files_to_process)

        avg_mb_per_page = 2.0
        projected_trans_mb = pages_left * avg_mb_per_page if self.config.get('translation', {}).get('enabled', False) else 0
        m3 = self.stats.get("slope_trans", 1.5)
        b3 = self.stats.get("intercept_trans", 0.5)
        translation_time = (projected_trans_mb * m3) + (files_left * b3)

        remaining = load_time + (files_left * (b1 + b2)) + (pages_left * (balanced_m1 + m2)) + translation_time

        self.emit("progress",
                  elapsed=int(elapsed),
                  remaining=int(remaining) if self.history_calibrated else None,
                  calibrated=self.history_calibrated,
                  pages_done=self.stats["pages_done_global"],
                  pages_total=self.stats["total_pages_global"])

    def detect_environment(self):
        def task():
            system = platform.system()
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

            try:
                if system == "Windows":
                    cmd = ["ping", "dlp.googleapis.com", "-n", "1"]
                else:
                    cmd = ["ping", "-c", "1", "dlp.googleapis.com"]
                res = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=15).decode(errors="ignore")
                if "time=" in res:
                    self.current_ping = int(float(res.split("time=")[1].split("ms")[0].strip()))
                elif "Average =" in res:
                    self.current_ping = int(float(res.split("Average =")[1].split("ms")[0].strip()))
            except Exception:
                self.current_ping = 100

            self.emit("env", gpu=self.gpu_name, ping=self.current_ping)

        threading.Thread(target=task, daemon=True).start()

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

    def expected_outputs(self, filename):
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
        """Validate -> write temp -> fsync -> atomic rename.
        If expected_pages is given, the output must have exactly that many pages -
        a shorter PDF means content was lost somewhere and must never look 'done'."""
        if not data:
            raise ValueError("refusing to save empty output")
        if path.lower().endswith(".pdf"):
            with fitz.open("pdf", data) as check_doc:
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
        policy = self.config.get("app_settings", {}).get("overwrite_policy", "skip")
        final_path = path
        if os.path.exists(path):
            if policy == "skip" and self.is_valid_output(path):
                self.log(f"Skipped (already exists): {os.path.basename(path)}")
                return path
            if policy == "version":
                final_path = self.unique_path(path)
        try:
            self.save_bytes_atomic(final_path, data, expected_pages=expected_pages)
            return final_path
        except Exception as e:
            self.log(f"SAVE FAILED for {os.path.basename(final_path)}: {e}")
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
    # Credentials hygiene
    # ------------------------------------------------------------------

    @staticmethod
    def get_secure_credentials_dir():
        if sys.platform == "win32":
            base = os.environ.get("APPDATA", os.path.expanduser("~"))
            return os.path.join(base, "MediXtract")
        if sys.platform == "darwin":
            return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "MediXtract")
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

    def credentials_status(self):
        """Returns {'state': 'missing'|'in_project'|'secure', 'path': ..., 'secure_dir': ...}"""
        gc = self.config.get("google_cloud", {})
        key_setting = gc.get("service_account_key_file", "")
        secure_dir = self.get_secure_credentials_dir()
        if not key_setting:
            return {"state": "missing", "path": "", "secure_dir": secure_dir}
        key_path = os.path.abspath(key_setting)
        if not os.path.exists(key_path):
            return {"state": "missing", "path": key_path, "secure_dir": secure_dir}

        project_dir = os.path.abspath(os.path.dirname(os.path.abspath(__file__)))
        try:
            inside = os.path.commonpath([key_path, project_dir]) == project_dir
        except ValueError:
            inside = False
        return {"state": "in_project" if inside else "secure", "path": key_path,
                "secure_dir": secure_dir, "sync_service": self.detect_cloud_sync(key_path)}

    def move_credentials_to_secure_location(self):
        """Move the key out of the app folder; returns the new path."""
        status = self.credentials_status()
        if status["state"] != "in_project":
            raise ValueError("Key is not inside the app folder - nothing to move.")

        secure_dir = status["secure_dir"]
        os.makedirs(secure_dir, exist_ok=True)
        new_path = os.path.join(secure_dir, os.path.basename(status["path"]))
        if os.path.exists(new_path):
            new_path = self.unique_path(new_path)
        shutil.move(status["path"], new_path)
        if sys.platform == "win32":
            self.restrict_file_acl(new_path)
        else:
            # macOS/Linux: owner-only permissions (same intent as the Windows ACL)
            try:
                os.chmod(secure_dir, 0o700)
                os.chmod(new_path, 0o600)
            except OSError as e:
                print(f"Could not chmod key file: {e}")

        self.config.setdefault("google_cloud", {})["service_account_key_file"] = new_path
        self.save_config()
        self.log(f"🔒 Service account key moved to: {new_path}")
        return new_path

    # ------------------------------------------------------------------
    # Cloud-sync detection
    # ------------------------------------------------------------------

    @staticmethod
    def detect_cloud_sync(path):
        """Best-effort detection of cloud-synced folders (OneDrive, Dropbox, ...).
        Returns the service name or None. Files in synced folders leave the machine
        even though this app never uploads them - the biggest real-world leak vector
        for the unredacted originals."""
        if not path:
            return None
        p = os.path.abspath(path).lower()

        for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            base = os.environ.get(env)
            if base and p.startswith(os.path.abspath(base).lower()):
                return "OneDrive"

        markers = {
            "onedrive": "OneDrive",
            "dropbox": "Dropbox",
            "google drive": "Google Drive",
            "googledrive": "Google Drive",
            "icloud": "iCloud",
            "nextcloud": "Nextcloud",
            "seafile": "Seafile",
        }
        # Match per path segment to avoid false positives on partial words
        for segment in re.split(r"[\\/]+", p):
            for marker, service in markers.items():
                if marker in segment:
                    return service
        return None

    def _emit_sync_warnings(self):
        warnings = []
        src_service = self.detect_cloud_sync(self.source_folder)
        if src_service:
            warnings.append({"where": "source folder", "service": src_service})
        out_service = self.detect_cloud_sync(self.get_output_folder())
        if out_service:
            warnings.append({"where": "output folder", "service": out_service})

        for w in warnings:
            self.log(f"⚠ The {w['where']} appears to be inside a {w['service']}-synced location - "
                     "files there are copied to the cloud outside this app's control.")
        if warnings:
            self.emit("sync_warning", warnings=warnings)

    # ------------------------------------------------------------------
    # Folder scan
    # ------------------------------------------------------------------

    def set_source_folder(self, folder):
        self.source_folder = folder
        self.scan_folder_async()

    def scan_folder_async(self):
        threading.Thread(target=self._scan_folder, daemon=True).start()

    def _scan_folder(self):
        self.files_to_process = []
        self.processed_files = []

        self.stats["total_pages_global"] = 0
        self.stats["total_size_mb_global"] = 0
        self.stats["pages_done_global"] = 0
        self.stats["size_done_mb_global"] = 0

        policy = self.config.get("app_settings", {}).get("overwrite_policy", "skip")

        try:
            raw_files = [f for f in os.listdir(self.source_folder)
                         if not f.startswith('.') and f.lower().endswith(SUPPORTED_EXTS)]
        except Exception as e:
            self.emit("error", message=f"Failed to list files: {e}")
            return

        self.emit("scan_start", folder=self.source_folder, count=len(raw_files),
                  output_folder=self.get_output_folder())
        self._emit_sync_warnings()

        if not raw_files:
            self.log("No supported documents found in selected folder.")
            self.emit("scan_done", pending=0, completed=0, pages=0, size_mb=0)
            return

        for f in raw_files:
            full_path = os.path.join(self.source_folder, f)
            if os.path.isdir(full_path):
                continue

            done = False
            if policy == "skip":
                expected = self.expected_outputs(f)
                if expected and all(self.is_valid_output(p) for p in expected):
                    done = True
                else:
                    for p in expected:
                        if os.path.exists(p) and not self.is_valid_output(p):
                            self.log(f"⚠ Found invalid/incomplete output for {f} - it will be re-processed.")
                            break

            size_mb = os.path.getsize(full_path) / (1024 * 1024)
            pages = 1
            if done:
                self.processed_files.append(f)
                self.emit("scan_item", name=f, status="completed", size_mb=round(size_mb, 2), pages=None)
            else:
                self.stats["total_size_mb_global"] += size_mb
                try:
                    if f.lower().endswith('.pdf'):
                        with fitz.open(full_path) as doc:
                            pages = len(doc)
                except Exception:
                    pages = 1
                self.stats["total_pages_global"] += pages
                self.files_to_process.append(f)
                self.emit("scan_item", name=f, status="pending", size_mb=round(size_mb, 2), pages=pages)

        self.emit("scan_done",
                  pending=len(self.files_to_process),
                  completed=len(self.processed_files),
                  pages=self.stats["total_pages_global"],
                  size_mb=round(self.stats["total_size_mb_global"], 1))
        self.log(f"Ready! {len(self.files_to_process)} file(s) to process. Total workload: {self.stats['total_pages_global']} pgs | {round(self.stats['total_size_mb_global'], 1)} MB")

    # ------------------------------------------------------------------
    # Batch processing
    # ------------------------------------------------------------------

    def stop(self):
        if self.is_processing:
            self.should_stop = True
            self.log("Stopping... finishing current document.")

    def retry_failed(self):
        """Re-queue the failed files from the last batch."""
        requeued = []
        for f in list(self.failed_files):
            if f not in self.files_to_process:
                self.files_to_process.append(f)
                requeued.append(f)
                self.emit("file_status", name=f, status="pending")
        self.failed_files = []
        if requeued:
            self.log(f"{len(requeued)} failed file(s) queued for retry. Press Start when ready.")
        return requeued

    def start_batch(self, keywords=None):
        """keywords = {"global": [...], "per_file": {filename: [...]}}"""
        if self.is_processing:
            raise ValueError("A batch is already running.")
        if not self.files_to_process:
            raise ValueError("No files to process.")
        keywords = keywords or {}
        threading.Thread(target=self._run_batch, args=(keywords,), daemon=True).start()

    def status_tag_for(self, filename, success):
        if not success:
            return "failed"
        v = self.verification_status.get(filename)
        if v == "review":
            return "review"
        if v == "verified":
            return "verified"
        return "success"

    def _run_batch(self, keywords):
        self.is_processing = True
        self.should_stop = False
        self.failed_files = []
        self.review_files = []
        self.verification_status = {}

        self.stats["pages_done_global"] = 0
        self.stats["size_done_mb_global"] = 0

        self.emit("batch_started", files=len(self.files_to_process))
        self.log(f"Starting batch: {self.stats['total_pages_global']} pages total.")

        global_kws = keywords.get("global", []) or []
        per_file_kws = keywords.get("per_file", {}) or {}

        files_snapshot = list(self.files_to_process)
        self.start_time_global = time.time()

        app_settings = self.config.get('app_settings', {}) or {}
        output_options = self.config.get("output_options", {}) or {}
        trans_config = self.config.get('translation', {}) or {}
        cloud_config = self.config.get('google_cloud', {}) or {}

        total_files = len(files_snapshot)
        success_count = 0

        try:
            self.log("Initializing DLP Processor...")
            self.log(f"🔒 Data region: {cloud_config.get('location', 'global')} - all DLP inspection is pinned to this region.")
            if trans_config.get('enabled', False):
                self.log(f"⚠ Translation is ON: redacted copies will be sent to Google Translation in {TRANSLATION_REGION} (US).")

            processor = ClinicalDocumentProcessor(
                project_id=cloud_config.get('project_id'),
                location=cloud_config.get('location', 'global'),
                credentials_file=cloud_config.get('service_account_key_file'),
                log_callback=self.on_processor_log,
                translation_location=TRANSLATION_REGION
            )

            output_folder = self.get_output_folder()
            os.makedirs(output_folder, exist_ok=True)

            for idx, filename in enumerate(files_snapshot):
                if self.should_stop:
                    self.log("Processing halted by user.")
                    break

                self.log(f"Processing {idx+1}/{total_files}: {filename}")
                self.emit("file_status", name=filename, status="processing")
                file_path = os.path.join(self.source_folder, filename)
                file_size = os.path.getsize(file_path) / (1024 * 1024)

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
                    merged_terms = list(set(global_kws + (per_file_kws.get(filename, []) or [])))

                    results = processor.process_document(file_path, custom_terms=merged_terms, output_config=output_options)

                    if results:
                        doc_stats = results.get("stats", {})
                        # PDF outputs must have exactly as many pages as the input
                        expected_pages = doc_stats.get("pages") if filename.lower().endswith(".pdf") else None

                        if results.get("selectable"):
                            target = os.path.join(output_folder, f"anonymized_{filename}")
                            saved = self.save_output(target, results["selectable"], expected_pages=expected_pages)
                            if saved:
                                saved_paths.append(("selectable", saved))
                                success = True

                        if results.get("non_selectable"):
                            target = os.path.join(output_folder, f"anonymized_flattened_{filename}")
                            saved = self.save_output(target, results["non_selectable"], expected_pages=expected_pages)
                            if saved:
                                saved_paths.append(("non_selectable", saved))
                                success = True

                        # VERIFICATION SCAN
                        if success and app_settings.get("verification_scan", True) and filename.lower().endswith('.pdf') and results.get("selectable"):
                            try:
                                self.log(f"Running verification scan on output of {filename}...")
                                verification = processor.verify_output(results["selectable"], merged_terms)
                                if verification["dlp_findings"] == 0 and verification["keyword_hits"] == 0:
                                    self.verification_status[filename] = "verified"
                                    self.log(f"✔ Verification passed: no residual sensitive text detected in {filename}.")
                                else:
                                    self.verification_status[filename] = "review"
                                    self.review_files.append(filename)
                                    self.log(f"⚠ Verification: {verification['dlp_findings']} possible InfoType hit(s) and {verification['keyword_hits']} keyword hit(s) remain in {filename}. Manual review recommended.")
                            except Exception as ve:
                                self.log(f"Verification scan failed for {filename}: {ve}")

                        # TRANSLATION
                        doc_to_translate = results.get("selectable") or results.get("non_selectable")
                        if trans_config.get('enabled', False) and filename.lower().endswith('.pdf') and doc_to_translate:
                            try:
                                target_lang = trans_config.get('target_language_code', 'en')
                                trans_results = processor.translate_document(doc_to_translate, target_language=target_lang)

                                trans_redact_iters = output_options.get("translation_redaction_iterations", 0)
                                if trans_redact_iters > 0:
                                    self.log(f"Re-redacting translated document ({trans_redact_iters} passes)...")
                                    trans_output_config = {
                                        "redaction": True,
                                        "redaction_iterations": trans_redact_iters,
                                        "selectable_text_copy": True,
                                        "non_selectable_text_copy": False
                                    }
                                    new_trans_results = []
                                    for label, trans_bytes in trans_results:
                                        try:
                                            redacted_trans_dict = processor.process_bytes(trans_bytes, custom_terms=merged_terms, output_config=trans_output_config)
                                            final_bytes = redacted_trans_dict.get("selectable") or redacted_trans_dict.get("non_selectable") or trans_bytes
                                            new_trans_results.append((label, final_bytes))
                                        except Exception as re_err:
                                            self.log(f"Failed to re-redact translation chunk {label}: {re_err}")
                                            new_trans_results.append((label, trans_bytes))
                                    trans_results = new_trans_results

                                if len(trans_results) == 1 and trans_results[0][0] == "":
                                    _, trans_bytes = trans_results[0]
                                    trans_target = os.path.join(output_folder, f"translated_{target_lang}_{filename}")
                                    saved = self.save_output(trans_target, trans_bytes)
                                    if saved:
                                        saved_paths.append(("translated", saved))
                                        self.log(f"Translated copy saved: {os.path.basename(saved)}")
                                else:
                                    folder_base = os.path.splitext(filename)[0]
                                    subfolder_name = f"{target_lang}_anonymized_{folder_base}"
                                    subfolder_path = os.path.join(output_folder, subfolder_name)
                                    os.makedirs(subfolder_path, exist_ok=True)

                                    chunk_bytes_list = []
                                    for label, trans_bytes in trans_results:
                                        chunk_filename = f"{label}_translated_{target_lang}_{filename}"
                                        saved = self.save_output(os.path.join(subfolder_path, chunk_filename), trans_bytes)
                                        if saved:
                                            saved_paths.append(("translated_chunk", saved))
                                        chunk_bytes_list.append(trans_bytes)

                                    self.log(f"Large document split into {len(trans_results)} translated chunks in: {subfolder_name}")

                                    if output_options.get("generate_full_translated_document", False):
                                        try:
                                            merged_bytes = processor.merge_pdf_bytes(chunk_bytes_list)
                                            full_name = f"FULL_translated_{target_lang}_{filename}"
                                            saved = self.save_output(os.path.join(subfolder_path, full_name), merged_bytes)
                                            if saved:
                                                saved_paths.append(("translated_full", saved))
                                                self.log(f"Merged full translated document saved: {full_name}")
                                        except Exception as me:
                                            self.log(f"Error merging translated chunks: {me}")
                            except Exception as te:
                                self.log(f"Translation error: {te}")

                        if not saved_paths:
                            self.log(f"Completed {filename} but no output was saved - check Output settings.")
                    else:
                        self.log(f"Completed {filename} but no content returned?")

                except Exception as e:
                    error_text = str(e)
                    self.log(f"Failed {filename}: {error_text[:120]}")

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
                        self.log(f"Could not write audit entry: {ae}")

                if success:
                    success_count += 1
                else:
                    self.failed_files.append(filename)

                self.emit("file_status", name=filename, status=self.status_tag_for(filename, success))
                self.emit_progress()
                self.save_performance_metrics()

            self.save_performance_metrics()

            elapsed = time.time() - self.start_time_global
            self.log(f"Batch Processing Complete! ({success_count} success, {len(self.failed_files)} failed, {len(self.review_files)} to review)")
            self.emit("summary",
                      total=total_files,
                      success=success_count,
                      failed=list(self.failed_files),
                      review=list(self.review_files),
                      elapsed=int(elapsed),
                      output_folder=self.get_output_folder())

        except Exception as e:
            self.emit("error", message=str(e))
            self.log(f"Error: {e}")

        finally:
            self.is_processing = False
            self.should_stop = False
            self.files_to_process = []
            self.emit("batch_finished")

    # ------------------------------------------------------------------
    # Source preview & click-to-tag (pre-processing keyword picking)
    # ------------------------------------------------------------------

    def _resolve_source_path(self, filename):
        """Join filename to the source folder, refusing traversal outside it."""
        if not self.source_folder:
            return None
        base = os.path.abspath(self.source_folder)
        path = os.path.abspath(os.path.join(base, filename))
        try:
            if os.path.commonpath([path, base]) != base:
                return None
        except ValueError:
            return None
        return path if os.path.isfile(path) else None

    def _ocr_cache_key(self, path, page_number):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0
        return (path, mtime, page_number)

    def _ocr_credentials_ready(self):
        gc = self.config.get("google_cloud", {})
        key = gc.get("service_account_key_file", "")
        return bool(gc.get("project_id")) and bool(key) and os.path.exists(os.path.abspath(key))

    def _get_ocr_processor(self):
        """Lazy processor for preview OCR - same region pinning as batch processing."""
        if self._ocr_processor is None:
            gc = self.config.get("google_cloud", {})
            self._ocr_processor = ClinicalDocumentProcessor(
                project_id=gc.get("project_id"),
                location=gc.get("location", "global"),
                credentials_file=gc.get("service_account_key_file"),
                log_callback=self.on_processor_log,
                translation_location=TRANSLATION_REGION,
            )
        return self._ocr_processor

    # Cap the rendered long side so one page never becomes a multi-MB bridge
    # payload (real scans at 300dpi would otherwise be huge as base64).
    MAX_PREVIEW_PX = 2200
    PREVIEW_JPEG_QUALITY = 80

    def _render_preview_page(self, path, page_number, zoom, with_words, display_name):
        """Shared paged renderer for source (click-to-tag) and output previews.
        Returns one page as a JPEG data URI - single-page payloads keep the UI
        bridge fast even for large scanned documents."""
        zoom = max(0.5, min(3.0, float(zoom)))
        doc = fitz.open(path)
        try:
            page_count = len(doc)
            page_number = max(0, min(int(page_number), page_count - 1))
            page = doc.load_page(page_number)

            long_side_pts = max(page.rect.width, page.rect.height) or 1
            effective = max(0.2, min(zoom, self.MAX_PREVIEW_PX / long_side_pts))

            pix = page.get_pixmap(matrix=fitz.Matrix(effective, effective))
            image_b64 = base64.b64encode(
                pix.tobytes("jpeg", jpg_quality=self.PREVIEW_JPEG_QUALITY)).decode()

            words = []
            has_text_layer = True
            ocr_done = True
            if with_words:
                # Local text layer: (x0, y0, x1, y1, word, block, line, word_no)
                words = [
                    {"text": w[4],
                     "x0": w[0] * effective, "y0": w[1] * effective,
                     "x1": w[2] * effective, "y1": w[3] * effective}
                    for w in page.get_text("words") if w[4].strip()
                ]
                has_text_layer = len(words) > 0
                ocr_done = has_text_layer

                if not has_text_layer:
                    cached = self._ocr_words_cache.get(self._ocr_cache_key(path, page_number))
                    if cached is not None:
                        ocr_done = True
                        words = [
                            {"text": w["text"],
                             "x0": w["x0"] * effective, "y0": w["y0"] * effective,
                             "x1": w["x1"] * effective, "y1": w["y1"] * effective}
                            for w in cached
                        ]

            return {
                "filename": display_name,
                "page": page_number,
                "page_count": page_count,
                "image": f"data:image/jpeg;base64,{image_b64}",
                "width": pix.width,
                "height": pix.height,
                "words": words,
                "has_text_layer": has_text_layer,
                "ocr_done": ocr_done,
                "ocr_available": self._ocr_credentials_ready() if with_words else True,
            }
        finally:
            doc.close()

    def get_source_preview(self, filename, page_number=0, zoom=1.5):
        """Render one page of the ORIGINAL document with word hit-boxes for
        click-to-tag. Rendering and text-layer extraction are local (PyMuPDF);
        scanned pages get word boxes only after an explicit ocr_source_page call."""
        path = self._resolve_source_path(filename)
        if not path:
            return None
        return self._render_preview_page(path, page_number, zoom,
                                         with_words=True, display_name=filename)

    def get_output_preview(self, filename, page_number=0, zoom=1.5):
        """Render one page of the anonymized OUTPUT of filename (paged, local)."""
        for path in self.expected_outputs(filename):
            if os.path.exists(path) and self.is_valid_output(path):
                return self._render_preview_page(path, page_number, zoom,
                                                 with_words=False,
                                                 display_name=os.path.basename(path))
        return None

    def ocr_source_page(self, filename, page_number=0):
        """Explicit user action: OCR one source page (region-pinned Vision) so a
        scanned page becomes click-to-tag-able. Words are cached in RAM only."""
        path = self._resolve_source_path(filename)
        if not path:
            raise ValueError("File not found in the source folder.")

        key = self._ocr_cache_key(path, int(page_number))
        if key in self._ocr_words_cache:
            return len(self._ocr_words_cache[key])

        if not self._ocr_credentials_ready():
            raise ValueError("Google credentials are not configured - see the README to set up access.")

        zoom = 3.0
        doc = fitz.open(path)
        try:
            page_number = max(0, min(int(page_number), len(doc) - 1))
            page = doc.load_page(page_number)
            img_bytes = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")
        finally:
            doc.close()

        self.log(f"Detecting text on page {page_number+1} of {filename} (Cloud OCR, region-pinned)...")
        words = self._get_ocr_processor().ocr_words(img_bytes, zoom=zoom)
        self._ocr_words_cache[self._ocr_cache_key(path, page_number)] = words
        self.log(f"Found {len(words)} words on page {page_number+1} - click any of them to tag.")
        return len(words)

