import tkinter as tk
from tkinter import filedialog, messagebox
import os
import sys
import time
import threading
import json


# Note: For actual Google Cloud Healthcare API integration, you would import your module
from dlp_processor import DLPProcessor

class LocalFileProcessorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Clinical Document Processor")
        self.root.geometry("600x500")

        self.source_folder = ""
        self.files_to_process = []
        self.processed_files = []
        self.current_file_index = 0
        self.is_processing = False
        self.config = self.load_config()
        
        # Initialize TTS Engine - REMOVED
        self.tts_engine = None

    def load_config(self):
        try:
            with open('config.json', 'r') as f:
                return json.load(f)
        except Exception as e:
            # messagebox.showwarning("Config Error", f"Could not load config.json: {e}\nUsing defaults.")
            return {"app_settings": {"simulation_mode": True}}

    def create_widgets(self):
        # 1. Folder Selection
        select_frame = tk.Frame(self.root, pady=10)
        select_frame.pack(fill=tk.X, padx=10)
        
    # ... (skipping unchanged parts) ...
    


        self.btn_select = tk.Button(select_frame, text="Select Data Folder", command=self.select_folder)
        self.btn_select.pack(side=tk.LEFT)

        self.lbl_folder = tk.Label(select_frame, text="No folder selected", fg="gray")
        self.lbl_folder.pack(side=tk.LEFT, padx=10)

        # 2. Controls
        ctrl_frame = tk.Frame(self.root, pady=10)
        ctrl_frame.pack(fill=tk.X, padx=10)

        self.btn_start = tk.Button(ctrl_frame, text="Start Processing", command=self.start_processing, state=tk.DISABLED, bg="#dddddd")
        self.btn_start.pack(side=tk.LEFT)
        
        # 3. Status Lists
        list_frame = tk.Frame(self.root, pady=10)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10)

        # Pending Files
        tk.Label(list_frame, text="Pending Documents:").grid(row=0, column=0, sticky="w")
        self.list_pending = tk.Listbox(list_frame, height=15, width=40)
        self.list_pending.grid(row=1, column=0, padx=5, sticky="news")

        # Processed Files
        tk.Label(list_frame, text="Processed Documents:").grid(row=0, column=1, sticky="w")
        self.list_processed = tk.Listbox(list_frame, height=15, width=40)
        self.list_processed.grid(row=1, column=1, padx=5, sticky="news")
        
        list_frame.columnconfigure(0, weight=1)
        list_frame.columnconfigure(1, weight=1)

        # 4. Status Bar
        self.status_var = tk.StringVar()
        self.status_var.set("Ready")
        self.status_bar = tk.Label(self.root, textvariable=self.status_var, bd=1, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def select_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.source_folder = folder
            self.lbl_folder.config(text=folder)
            self.load_files()
            self.btn_start.config(state=tk.NORMAL, bg="#90ee90")
            self.status_var.set(f"Loaded {len(self.files_to_process)} files.")

    def load_files(self):
        self.files_to_process = []
        self.list_pending.delete(0, tk.END)
        self.list_processed.delete(0, tk.END)
        self.processed_files = [] # Clear history
        
        try:
            output_folder = os.path.join(self.source_folder, "processed")
            
            for f in os.listdir(self.source_folder):
                full_path = os.path.join(self.source_folder, f)
                
                # Skip processed folder itself and non-files
                if f == "processed" or not os.path.isfile(full_path):
                    continue
                    
                # Check if already processed
                expected_output = os.path.join(output_folder, f"anonymized_{f}")
                if os.path.exists(expected_output):
                    self.processed_files.append(f)
                    self.list_processed.insert(tk.END, f"{f} (Completed)")
                else:
                    self.files_to_process.append(f)
                    self.list_pending.insert(tk.END, f)
                    
        except Exception as e:
            messagebox.showerror("Error", f"Failed to list files: {e}")

    def start_processing(self):
        if not self.files_to_process:
            messagebox.showinfo("Info", "No files to process.")
            return
        
        self.is_processing = True
        self.btn_start.config(state=tk.DISABLED)
        # We start the recursive processing
        self.process_next_file()


    def process_next_file(self):
        if not self.files_to_process:
            self.status_var.set("All files processed.")
            messagebox.showinfo("Done", "All documents processed!")
            self.is_processing = False
            self.btn_start.config(state=tk.DISABLED)
            return

        current_file_name = self.files_to_process[0]
        self.status_var.set(f"Processing {current_file_name}...")
        self.root.update()

        # --- PROCESSING LOGIC ---
        processed_text = ""
        app_settings = self.config.get('app_settings', {})
        simulation_mode = app_settings.get('simulation_mode', True)
        
        try:
            if simulation_mode:
                # Simulation
                time.sleep(1) # Fake delay
                processed_text = f"SIMULATION: De-identified content for {current_file_name}.\nPatient Name: [REDACTED]\nDate: [OFFSET]"
            
            else:
                # REAL MODE
                # 0. Setup Credentials
                cloud_config = self.config.get('google_cloud', {})
                key_file = cloud_config.get('service_account_key_file', '')
                
                if key_file and os.path.exists(key_file):
                    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = key_file
                
                project_id = cloud_config.get('project_id')
                if not project_id or project_id == "YOUR_PROJECT_ID_HERE":
                    raise ValueError("Please configure a valid Project ID in config.json")
                
                # Initialize DLP
                processor = DLPProcessor(project_id, key_file)
                
                # READ FILE CONTENT (Text support for now)
                file_path = os.path.join(self.source_folder, current_file_name)
                with open(file_path, 'r', encoding='utf-8') as f:
                    original_text = f.read()
                
                print(f"De-identifying {current_file_name} via Google Cloud DLP...")
                processed_text = processor.deidentify_text(original_text)
                
                # Save Result
                output_folder = os.path.join(self.source_folder, "processed")
                os.makedirs(output_folder, exist_ok=True)
                output_path = os.path.join(output_folder, f"anonymized_{current_file_name}")
                
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(processed_text)
                    
                processed_text = f"SUCCESS: Saved to {output_path}"

        except Exception as e:
            messagebox.showerror("Processing Error", f"Error processing {current_file_name}:\n{e}")
            processed_text = "Error in processing."

        # Update UI Stacks
        self.files_to_process.pop(0)
        self.list_pending.delete(0)
        self.processed_files.append(current_file_name)
        self.list_processed.insert(tk.END, current_file_name)
        


        # 4. Ask to Continue
        if self.files_to_process:
            should_continue = messagebox.askyesno(
                "Continue?", 
                f"Finished processing {current_file_name}.\n\nProcessed: {len(self.processed_files)}\nRemaining: {len(self.files_to_process)}\n\nDo you want to process the next file?"
            )
            
            if should_continue:
                self.root.after(100, self.process_next_file)
            else:
                self.status_var.set("Processing paused.")
                self.btn_start.config(state=tk.NORMAL, text="Resume Processing")
                self.is_processing = False
        else:
            self.status_var.set("All files processed.")
            messagebox.showinfo("Done", "Processing Complete!")
            self.is_processing = False
            self.btn_start.config(state=tk.DISABLED)

if __name__ == "__main__":
    root = tk.Tk()
    app = LocalFileProcessorApp(root)
    root.mainloop()
