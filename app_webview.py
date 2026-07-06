"""
Clinical Document Processor - modern HTML/CSS/JS interface.

Runs the local web UI (web_ui/) inside a native window via pywebview:
no HTTP server, no open ports, no browser - the UI talks to Python
directly through the js_api bridge. All assets are bundled locally;
the page can never load anything from the internet.

If pywebview is not installed, falls back to the classic Tkinter UI.
"""
import os
import sys
import threading

# Run relative to this file no matter how we were launched (shortcut, terminal...)
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

from batch_core import BatchEngine, APP_VERSION


class Api:
    """Methods callable from JavaScript as window.pywebview.api.<name>(...).
    Only JSON-serializable data crosses the bridge - never document bytes
    (except local previews the user explicitly asks for)."""

    def __init__(self, engine):
        self._engine = engine  # underscore: not exposed via the JS bridge
        self._window = None  # underscore: pywebview must NOT expose this via the JS bridge

    # --- event stream -------------------------------------------------
    def poll_events(self):
        return self._engine.poll_events()

    # --- state --------------------------------------------------------
    def get_state(self):
        return {
            "app_version": APP_VERSION,
            "source_folder": self._engine.source_folder,
            "output_folder": self._engine.get_output_folder(),
            "posture": self._engine.posture(),
            "settings": self._engine.get_settings(),
            "is_processing": self._engine.is_processing,
            "env": {"gpu": self._engine.gpu_name, "ping": self._engine.current_ping},
        }

    @staticmethod
    def _folder_dialog():
        import webview
        try:
            return webview.FileDialog.FOLDER  # pywebview >= 5.4
        except AttributeError:
            return webview.FOLDER_DIALOG      # older versions

    # --- folders --------------------------------------------------------
    def choose_source_folder(self):
        result = self._window.create_file_dialog(self._folder_dialog())
        if result:
            folder = result[0] if isinstance(result, (list, tuple)) else result
            self._engine.set_source_folder(folder)
            return {"source_folder": folder, "output_folder": self._engine.get_output_folder()}
        return None

    def set_source_folder(self, folder):
        """Used by drag & drop when the browser exposes the real path."""
        if folder and os.path.isdir(folder):
            self._engine.set_source_folder(folder)
            return {"source_folder": folder, "output_folder": self._engine.get_output_folder()}
        raise ValueError("Not a folder.")

    def choose_output_folder(self):
        result = self._window.create_file_dialog(self._folder_dialog())
        if result:
            folder = result[0] if isinstance(result, (list, tuple)) else result
            src = self._engine.source_folder
            if src and os.path.abspath(folder) == os.path.abspath(src):
                raise ValueError("Saving directly into the source folder mixes originals and anonymized copies. Pick a subfolder or a separate folder.")
            self._engine.config["app_settings"]["output_folder"] = folder
            self._engine.save_config()
            self._engine.log(f"Output folder set to: {folder}")
            if src:
                self._engine.scan_folder_async()
            return {"output_folder": folder}
        return None

    def reset_output_folder(self):
        self._engine.config["app_settings"]["output_folder"] = ""
        self._engine.save_config()
        if self._engine.source_folder:
            self._engine.scan_folder_async()
        return {"output_folder": self._engine.get_output_folder()}

    def open_output_folder(self):
        folder = self._engine.get_output_folder()
        if not folder or not os.path.isdir(folder):
            raise ValueError("No output folder exists yet - process a document first.")
        import subprocess
        if sys.platform == "win32":
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
        return True

    # --- settings -------------------------------------------------------
    def get_settings(self):
        return self._engine.get_settings()

    def save_settings(self, settings):
        return self._engine.save_settings(settings)

    # --- batch ------------------------------------------------------
    def start_batch(self, keywords):
        self._engine.start_batch(keywords)
        return True

    def stop_batch(self):
        self._engine.stop()
        return True

    def retry_failed(self):
        return self._engine.retry_failed()

    # --- credentials hygiene ------------------------------------------
    def credentials_status(self):
        return self._engine.credentials_status()

    def move_credentials(self):
        return self._engine.move_credentials_to_secure_location()

    # --- document viewer (paged: source for tagging, output for review) --
    def get_source_preview(self, filename, page=0, zoom=1.5):
        return self._engine.get_source_preview(filename, page_number=page, zoom=zoom)

    def get_output_preview(self, filename, page=0, zoom=1.5):
        return self._engine.get_output_preview(filename, page_number=page, zoom=zoom)

    def ocr_source_page(self, filename, page=0):
        return self._engine.ocr_source_page(filename, page_number=page)


def run_webview():
    import webview

    engine = BatchEngine()
    api = Api(engine)

    window = webview.create_window(
        title=f"Clinical Document Processor v{APP_VERSION} - Google DLP",
        url=os.path.join(PROJECT_DIR, "web_ui", "index.html"),
        js_api=api,
        width=1320,
        height=840,
        min_size=(1024, 660),
        text_select=True,  # allow selecting/copying text (log, paths, filenames)
    )
    api._window = window

    def on_closing():
        if engine.is_processing:
            engine.should_stop = True
        return True

    window.events.closing += on_closing
    webview.start(debug=False)


def run_tkinter_fallback(reason):
    print(f"Modern UI unavailable ({reason}) - starting the classic interface instead.")
    import tkinter as tk
    from batch_processor_gui import LocalFileProcessorApp
    root = tk.Tk()
    app = LocalFileProcessorApp(root)
    app.create_widgets()
    root.mainloop()


if __name__ == "__main__":
    try:
        import webview  # noqa: F401
    except ImportError:
        run_tkinter_fallback("pywebview is not installed - run: pip install pywebview")
        sys.exit(0)

    try:
        run_webview()
    except Exception as e:
        # e.g. Linux without GTK/QT WebKit, or a broken WebView2 runtime:
        # 'import webview' succeeds but starting the window fails.
        run_tkinter_fallback(f"could not start the web view: {e}")
