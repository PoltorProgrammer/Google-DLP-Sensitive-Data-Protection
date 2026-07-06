# Clinical Document Anonymizer (Google DLP)

[![CI](https://github.com/PoltorProgrammer/Google-DLP-Sensitive-Data-Protection/actions/workflows/ci.yml/badge.svg)](https://github.com/PoltorProgrammer/Google-DLP-Sensitive-Data-Protection/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## Installation & Usage Guide

### 🪟 Windows
1.  **Download & Extract**: Download the project folder and unzip it to a location of your choice.
2.  **Run the Installer**: Locate the file named **`Start_Windows.bat`**.
3.  **Double-Click**: Run the file.
    *   *Note*: The script will automatically check if Python 3.11 is installed. If not, it will attempt to install it for you (you may be asked to approve the installation).
4.  **Desktop Shortcut**: On the first run, the script will create a shortcut named **"Start Clinical Processor"** on your Desktop. You can use this for future access.
5.  **Use**: The application window will open automatically.

### 🍎 macOS
1.  **Download & Extract**: Download the project folder and unzip it.
2.  **Run the Installer**: Locate the file named **`Start_Mac.command`**.
3.  **Double-Click**: Run the file.
    *   *Security Note*: If you see a warning saying the file "can’t be opened because it is from an unidentified developer", **Right-Click** the file and select **Open**, then click **Open** again in the dialog.
4.  **Desktop Shortcut**: The script will create an alias on your Desktop for easy access.
5.  **Use**: The application will launch. The first run may take a moment to set up the virtual environment.

### 🐧 Linux
1.  **Download & Extract**: Unzip the project folder.
2.  **Open Terminal**: Navigate to the project folder.
3.  **Permissions**: Ensure the scripts are executable by running:
    ```bash
    chmod +x Start_Linux.sh scripts/*.sh
    ```
4.  **Run**: Execute the launch script:
    ```bash
    ./Start_Linux.sh
    ```
5.  **Prerequisites**:
    *   Ensure you have Python 3 and `venv` installed (`sudo apt install python3-venv` on Debian/Ubuntu).


---

## Configuration Guide (Administrator)

This section explains how to set up the **Google Cloud DLP (Data Loss Prevention)** API required for **Real Mode**.

### Step 1: Create a Google Cloud Project
1.  Go to the [Google Cloud Console](https://console.cloud.google.com/).
2.  Create a **New Project**.
3.  Copy the **Project ID**.

### Step 2: Enable the required APIs
The app uses three Google Cloud services. Enable each one for your project:
1.  **Sensitive Data Protection (DLP API)** — [Enable DLP API](https://console.cloud.google.com/apis/library/dlp.googleapis.com) (redaction)
2.  **Cloud Vision API** — [Enable Vision API](https://console.cloud.google.com/apis/library/vision.googleapis.com) (OCR overlay for selectable text)
3.  **Cloud Translation API** — [Enable Translation API](https://console.cloud.google.com/apis/library/translate.googleapis.com) (only needed if you use the optional translation feature)

### Step 3: Get Credentials (Service Account)
1.  Open the **Navigation Menu** (the three horizontal lines **☰** in the top-left corner).
2.  Hover over **IAM & Admin** and select **Service Accounts**.
    *   **Direct Link**: [IAM & Admin > Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts)
3.  Create a Service Account (e.g., `dlp-admin`).
4.  **Permissions** (Grant this service account access to project — least privilege):
    *   Role: **DLP User** (`roles/dlp.user`) — scanning and content de-identification.
    *   Role: **Cloud Translation API User** (`roles/cloudtranslate.user`) — only if translation is used.
    *   Vision OCR needs no extra role, only the enabled API.
5.  **Create Key**:
    *   Click on the newly created Service Account.
    *   Go to the **KEYS** tab.
    *   Click **ADD KEY** > **Create new key**.
    *   Select **JSON** and click **Create**.
    *   Rename the downloaded file to `credentials.json` and move it into this project folder.

> 🔒 **Key hygiene**: on first launch the app will offer to move the key to a protected
> per-user folder (`%APPDATA%\MediXtract` on Windows, `~/.config/medixtract` elsewhere)
> and lock down its file permissions. Accept this — it keeps the key out of the app folder
> so it can never be accidentally zipped, shared or synced together with the app.
> Never commit the key to git (it is `.gitignore`d) and never email it.

### Step 4: Update config.json
Open `config.json` and fill in your details:

```json
{
    "google_cloud": {
        "project_id": "YOUR_PROJECT_ID",
        "location": "europe-west6",
        "service_account_key_file": "credentials.json"
    },
    "app_settings": {
        "simulation_mode": false
    }
}
```
*   *Note: `location` forces DLP processing to occur in that region (e.g., `europe-west6` for Zurich, `europe-west3` for Frankfurt) for compliance. Every DLP request carries this region, and the Vision OCR is pinned to the matching EU/US endpoint.*
*   *Exception: the optional **translation** feature is processed by Google in `us-central1` (US) — document translation is not available in EU regions. Only already-redacted copies are ever sent there, and the app shows a warning whenever translation is enabled.*

All other options (output folder, overwrite policy, outputs to generate, redaction passes, translation, verification scan, audit trail) can be changed from the **⚙ Settings** dialog inside the app — no manual JSON editing needed.

---

## How it Works

1.  **Direct Processing**: The app reads your local PDF files and streams them securely to the **Google Cloud DLP** API in your configured region.
2.  **Transient Redaction**: The API processes the file in-memory (RAM) to redact identifying information (Names, Phones, Emails, Credit Cards, national IDs), while keeping Dates and Locations visible.
3.  **Trustworthy Saving**: Results are validated and written atomically — a file is only marked **Completed** if its output exists, opens cleanly and has **exactly as many pages as the input**. A page that fails to process aborts the whole document (after automatic retries) instead of silently disappearing from the output.
4.  **Verification Scan** (optional, on by default): after saving, the app re-inspects the finished output's text layer and flags any document with possible residual sensitive text for manual review.
5.  **Audit Trail** (optional, on by default): one JSON line per document (`audit_log.jsonl` in the output folder) records timestamps, SHA-256 hashes of input/outputs, the region used, redaction settings and verification results — never document content or keyword values.
6.  **Result**: The redacted file is saved to your chosen output folder (default: `processed/` next to the source). **No data is stored in the cloud.**

### Security posture at a glance
The strip at the top of the app window always shows the live configuration, e.g.:

`🔒 Region: europe-west6 | Redaction: ON x1 | Translation: OFF | Verify: ON | Audit: ON`

If anything reduces your protection (redaction off, translation to the US enabled, verification off), the strip turns orange/red with a ⚠ so it can never change silently.

---

## The Interface

The app uses a modern HTML interface rendered in a **native window** (via `pywebview`):

*   **No server, no open ports, no browser** — the UI talks to Python directly in-process. Nothing is exposed on the network.
*   **Fully offline UI** — every style and script is bundled locally and a strict Content-Security-Policy blocks any remote resource, so the interface itself can never contact the internet. Only the Python backend talks to Google's APIs.
*   **Built-in review workflow** — documents flagged by the verification scan get a **Preview** button that renders the anonymized output right in the app (locally, via PyMuPDF) so you can check flagged pages without hunting through folders.
*   Drag & drop a folder onto the app, live per-file status badges, progress bar with time estimation, and a settings panel for everything.
*   **Cloud-sync warnings** — if the source or output folder sits inside OneDrive, Dropbox, Google Drive or similar, the app warns you: synced folders copy the unredacted originals to the cloud outside the app's control.

If `pywebview` is not installed, the launcher automatically falls back to the classic Tkinter interface (`batch_processor_gui.py`).

**Architecture note**: all processing logic lives in `batch_core.py` (engine) and `dlp_processor.py` (Google Cloud calls). The interfaces (`app_webview.py` + `web_ui/`, or the Tkinter fallback) are thin layers on top — the security guarantees are identical in both.

---

## Development

- Run the test suite: `pip install -r requirements-dev.txt && pytest tests/ -v`
- CI runs on every push/PR (Ubuntu + Windows, Python 3.11/3.12); Dependabot keeps dependencies patched.
- To report security issues, see [SECURITY.md](SECURITY.md). Licensed under [MIT](LICENSE).

---

**Made by Tomás González Bartomeu - PoltorProgrammer**

[![Email](https://img.shields.io/badge/Email-poltorprogrammer%40gmail.com-red?logo=gmail&labelColor=lightgrey)](mailto:poltorprogrammer@gmail.com)
