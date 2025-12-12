# User Manual & Configuration Guide

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
    *   For Text-to-Speech support, you may need `espeak` (`sudo apt install espeak`).

---

## Configuration Guide (Administrator)

This section explains how to set up the Google Cloud resources required for **Real Mode**. If you are just testing the app in Simulation Mode, you can skip this.

### Step 1: Create a Google Cloud Project
1.  Go to the [Google Cloud Console](https://console.cloud.google.com/).
2.  Click the project dropdown at the top of the page and select **"New Project"**.
3.  Give it a name (e.g., `clinical-data-app`) and click **Create**.
4.  Copy the **Project ID** (it might be `clinical-data-app-12345`). You will need this for `config.json`.

### Step 2: Enable the Healthcare API
1.  In the search bar at the top, type **"Cloud Healthcare API"** and select it.
2.  Click **Enable**.

### Step 3: Create Dataset & FHIR Stores
1.  Go to the **[Healthcare Browser](https://console.cloud.google.com/healthcare/browser)** in the console.
2.  Click **Create Dataset**.
    *   **ID**: e.g., `main-dataset`.
    *   **Region**: e.g., `us-central1`.
    *   Click **Create**.
3.  Click on your new dataset to open it.
4.  Click **Create Data Store**.
    *   **Type**: Select **FHIR**.
    *   **ID**: `source-store` (This is where you upload raw documents).
    *   Click **Create**.
5.  Repeat to create a second store for the output:
    *   **Type**: FHIR.
    *   **ID**: `dest-store` (This is where cleaned data goes).
    *   Click **Create**.

### Step 4: Get Credentials (The Key File)
The app needs permission to access these stores.
1.  Go to **[IAM & Admin > Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts)**.
2.  Click **Create Service Account**.
    *   **Name**: `app-access`.
    *   **Access**: Give it the role **"Healthcare FHIR Store Editor"** (or Owner for testing).
    *   Click **Done**.
3.  Click on the email address of the service account you just created.
4.  Go to the **Keys** tab (at the top).
5.  Click **Add Key** > **Create new key**.
6.  Select **JSON** and click **Create**.
7.  A file will download to your computer.
    *   **Action**: Rename this file to `credentials.json` and move it into the `Google-HealthCare-API` folder (where the `.bat` files are).

### Step 5: Update config.json
Open `config.json` with Notepad (or TextEdit on Mac) and fill in the values you just created:

```json
{
    "google_cloud": {
        "project_id": "YOUR_PROJECT_ID_FROM_STEP_1",
        "location": "us-central1",
        "dataset_id": "main-dataset",
        "fhir_store_id": "source-store",
        "destination_dataset_id": "main-dataset",
        "destination_fhir_store_id": "dest-store",
        "service_account_key_file": "credentials.json"
    },
    "app_settings": {
        "simulation_mode": false, 
        ...
    }
}
```
*Note: Set `"simulation_mode": false` to actually use the Google Cloud connection.*

---
**Made by Tomás González Bartomeu - PoltorProgrammer**
