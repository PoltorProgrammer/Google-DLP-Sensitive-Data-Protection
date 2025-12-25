# Clinical Document Anonymizer (Google DLP)

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

### Step 2: Enable the DLP API
1.  In the search bar, type **"Sensitive Data Protection (DLP API)"** (or just "DLP") and select the result with the document icon.
    *   **Direct Link**: [Enable DLP API](https://console.cloud.google.com/apis/library/dlp.googleapis.com)
2.  Click **Enable**.

### Step 3: Get Credentials (Service Account)
1.  Open the **Navigation Menu** (the three horizontal lines **☰** in the top-left corner).
2.  Hover over **IAM & Admin** and select **Service Accounts**.
    *   **Direct Link**: [IAM & Admin > Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts)
3.  Create a Service Account (e.g., `dlp-admin`).
3.  **Permissions** (Grant this service account access to project):
    *   Role: **DLP User** (Allows scanning and content de-identification).
4.  **Create Key**:
    *   Click on the newly created Service Account.
    *   Go to the **KEYS** tab.
    *   Click **ADD KEY** > **Create new key**.
    *   Select **JSON** and click **Create**.
    *   Rename the downloaded file to `credentials.json` and move it into this project folder.

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
*   *Note: `location` forces processing to occur in that region (e.g., `europe-west6` for Zurich, `europe-west3` for Frankfurt) for compliance.*
*   *Set `"simulation_mode": false` to go live.*

---

## How it Works

1.  **Direct Processing**: The app reads your local PDF files and streams them securely to the **Google Cloud DLP** API.
2.  **Transient Redaction**: The API processes the file in-memory (RAM) to redact identifying information (Names, Phones, Emails, Credit Cards), while keeping Dates and Locations visible.
3.  **Result**: The redacted file is returned immediately and saved to your local `processed/` folder. **No data is stored in the cloud.**

---

**Made by Tomás González Bartomeu - PoltorProgrammer**

[![Email](https://img.shields.io/badge/Email-poltorprogrammer%40gmail.com-red?logo=gmail&labelColor=lightgrey)](mailto:poltorprogrammer@gmail.com)
