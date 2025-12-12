# Google HealthCare API Document Anonymizer

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

This section explains how to set up the **Google Cloud Healthcare API** resources required for **Real Mode**. 

### Step 1: Create a Google Cloud Project
1.  Go to the [Google Cloud Console](https://console.cloud.google.com/).
2.  Create a **New Project**.
3.  Copy the **Project ID**.

### Step 2: Enable the Healthcare API
1.  In the search bar, type **"Cloud Healthcare API"** and select it.
2.  Click **Enable**.

### Step 3: Create Dataset and FHIR Stores
1.  Go to the **Healthcare Browser** in the console.
2.  **Create Dataset**: e.g., `clinical-dataset` (Region: `us-central1`).
3.  **Create Input FHIR Store**: Inside the dataset, create a FHIR Store named `input-store`.
    *   **Type**: Select "FHIR".
    *   **ID**: Enter `input-store`.
    *   **Version**: Select "R4" (Recommended).
    *   **Other Settings**: Leave all checkboxes and advanced settings as default (do not change them).
4.  *(Optional)*: The application handles the creation of output stores, or you can create a dedicated `anonymized-store` if you wish to configure it specifically.

### Step 4: Get Credentials (Service Account)
1.  Go to **IAM & Admin > Service Accounts**.
2.  Create a Service Account (e.g., `healthcare-admin`).
3.  **Grant Roles**:
    *   **Healthcare Dataset Administrator** (This single role provides full access to manage the dataset and stores).
4.  **Create Key**:
    *   Click on the newly created Service Account (the email address link).
    *   Go to the **KEYS** tab (top menu).
    *   Click **ADD KEY** > **Create new key**.
    *   Select **JSON** and click **Create**.
    *   The file will automatically download.
5.  **Action**: Rename this file to `credentials.json` and move it into the project folder.

### Step 5: Update config.json
Open `config.json` and fill in your details:

```json
{
    "google_cloud": {
        "project_id": "YOUR_PROJECT_ID",
        "location": "us-central1",
        "dataset_id": "clinical-dataset",
        "fhir_store_id": "input-store",
        "destination_store_id": "anonymized-store",
        "service_account_key_file": "credentials.json"
    },
    "app_settings": {
        "simulation_mode": false
    }
}
```
*Set `"simulation_mode": false` to go live.*

---

## How it Works

1.  **Batch Upload**: The app scans your local folder and securely uploads text files to your Google Cloud **Input FHIR Store** as `DocumentReference` resources.
2.  **Server-Side De-identification**: It triggers a powerful, asynchronous **De-identify** operation on the Cloud Healthcare API. This creates a new FHIR Store containing only anonymized data.
3.  **Result Retrieval**: The app waits for the job to finish, then downloads the anonymized text from the output store and saves it to your local `processed/` folder.

---
**Made by Tomás González Bartomeu - PoltorProgrammer** - [![Email](https://img.shields.io/badge/Email-poltorprogrammer%40gmail.com-EA4335?logo=gmail&labelColor=lightgrey)](mailto:poltorprogrammer@gmail.com)
