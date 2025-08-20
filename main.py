# main.py
import time
import threading
import os
import sys
import socket
import uvicorn
import re 
import gspread 
from google.oauth2.service_account import Credentials 
import pandas as pd 

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import fitz
import pytesseract
from PIL import Image

# --- Tesseract Path Configuration ---
if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
    tesseract_dir = os.path.join(base_path, 'tesseract')
    pytesseract.pytesseract.tesseract_cmd = os.path.join(tesseract_dir, 'tesseract.exe') if sys.platform == 'win32' else os.path.join(tesseract_dir, 'tesseract')
else:
    base_path = os.path.dirname(__file__)

# --- 1. FastAPI App Initialization ---
app = FastAPI(title="PDF Automation Backend", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- 2. Data Models ---
class WatchRequest(BaseModel):
    folder_path: str
    sheet_url: str 

# --- 3. Global State Management ---
class AppState:
    def __init__(self):
        self.observer = None
        self.is_watching = False
        self.logs = []
        self.stats = {"files_processed": 0, "errors": 0}
        self.lock = threading.Lock()
        self.sheet_url = None 

    def add_log(self, level, message):
        with self.lock:
            timestamp = time.strftime("%H:%M:%S")
            log_entry = {"timestamp": timestamp, "level": level, "message": message}
            print(f"[{level}] {message}", flush=True) # Also print to console for debugging
            self.logs.append(log_entry)
            if len(self.logs) > 100:
                self.logs.pop(0)

# --- FIX: Instantiate the state object AFTER the class is defined ---
state = AppState()

# --- 4. Google Sheets Integration ---
def get_gspread_client():
    """Authenticates with Google and returns a gspread client."""
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds_path = os.path.join(base_path, 'credentials.json')
        if not os.path.exists(creds_path):
            state.add_log("ERROR", "credentials.json not found. Cannot connect to Google Sheets.")
            return None
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        client = gspread.authorize(creds)
        return client
    except Exception as e:
        state.add_log("ERROR", f"Google Sheets authentication failed: {e}")
        return None

def append_to_sheet(data: dict):
    """Appends a dictionary of data as a new row to the configured Google Sheet."""
    client = get_gspread_client()
    if not client or not state.sheet_url:
        return

    try:
        state.add_log("INFO", f"Opening Google Sheet...")
        sheet = client.open_by_url(state.sheet_url).sheet1
        
        df = pd.DataFrame([data])
        
        header = sheet.row_values(1)
        if not header: 
            sheet.update([df.columns.values.tolist()] + df.values.tolist())
        else:
            ordered_df = df.reindex(columns=header)
            sheet.append_rows(ordered_df.values.tolist(), value_input_option='USER_ENTERED')

        state.add_log("SUCCESS", "Data successfully written to Google Sheet.")
    except Exception as e:
        state.add_log("ERROR", f"Failed to write to Google Sheet: {e}")
        raise 

# --- 5. Data Parsing Logic ---
def parse_invoice_text(text: str) -> dict:
    """Parses raw text to find invoice details using regex."""
    vendor_pattern = re.compile(r"^(.*?)\n", re.MULTILINE) 
    amount_pattern = re.compile(r"(?:Total|Amount Due|Balance)[\s:]*\$?([\d,]+\.\d{2})", re.IGNORECASE)
    date_pattern = re.compile(r"Date:?\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE)

    vendor = vendor_pattern.search(text)
    amount = amount_pattern.search(text)
    date = date_pattern.search(text)

    return {
        "Vendor": vendor.group(1).strip() if vendor else "N/A",
        "Invoice Date": date.group(1).strip() if date else "N/A",
        "Total Amount": float(amount.group(1).replace(",", "")) if amount else 0.0,
        "Processed Time": time.strftime("%Y-%m-%d %H:%M:%S")
    }

# --- 6. PDF Processing Logic ---
def extract_text_with_ocr(pdf_path: str) -> str:
    text_content = []
    try:
        doc = fitz.open(pdf_path)
        for page_num, page in enumerate(doc):
            text = page.get_text().strip()
            if text:
                text_content.append(text)
            else:
                state.add_log("INFO", f"Page {page_num + 1} has no text layer, falling back to OCR.")
                pix = page.get_pixmap(dpi=300)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                ocr_text = pytesseract.image_to_string(img, lang='eng').strip()
                if ocr_text:
                    text_content.append(ocr_text)
        doc.close()
        return "\n".join(text_content)
    except Exception as e:
        state.add_log("ERROR", f"Error during PDF processing for {os.path.basename(pdf_path)}: {e}")
        raise

# --- 7. Watchdog File Handler ---
class PDFHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory and event.src_path.lower().endswith(".pdf"):
            time.sleep(1)
            file_name = os.path.basename(event.src_path)
            state.add_log("INFO", f"Detected new file: {file_name}")
            try:
                extracted_text = extract_text_with_ocr(event.src_path)
                if not extracted_text.strip():
                     state.add_log("ERROR", f"Could not extract any text from {file_name}.")
                     raise ValueError("Empty text extracted")

                state.add_log("INFO", f"Parsing data from {file_name}...")
                invoice_data = parse_invoice_text(extracted_text)
                state.add_log("SUCCESS", f"Parsed data: {invoice_data}")

                append_to_sheet(invoice_data)
                
                with state.lock:
                    state.stats["files_processed"] += 1

            except Exception as e:
                with state.lock:
                    state.stats["errors"] += 1
                state.add_log("ERROR", f"Full processing pipeline failed for {file_name}: {e}")

# --- 8. API Endpoints ---
@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.get("/status")
def get_status():
    with state.lock:
        current_logs = state.logs[:]
        state.logs.clear()
    return {"is_watching": state.is_watching, "stats": state.stats, "logs": current_logs}

@app.post("/start-watching")
def start_watching(request: WatchRequest):
    if state.is_watching:
        raise HTTPException(status_code=400, detail="Watcher is already running.")
    if not os.path.isdir(request.folder_path):
        raise HTTPException(status_code=404, detail="Directory not found.")
    
    state.sheet_url = request.sheet_url 
    state.add_log("INFO", f"Starting watcher on directory: {request.folder_path}")
    
    handler = PDFHandler()
    state.observer = Observer()
    try:
        state.observer.schedule(handler, request.folder_path, recursive=False)
        state.observer.start()
        state.is_watching = True
        return {"message": "Watcher started successfully."}
    except Exception as e:
        state.add_log("ERROR", f"Failed to start watcher: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/stop-watching")
def stop_watching():
    if not state.is_watching or not state.observer:
        raise HTTPException(status_code=400, detail="Watcher is not running.")
    state.observer.stop()
    state.observer.join()
    state.is_watching = False
    state.add_log("INFO", "Watcher stopped successfully.")
    return {"message": "Watcher stopped successfully."}

# --- 9. Server Startup Logic ---
def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

if __name__ == "__main__":
    port = find_free_port()
    print(f"FASTAPI_PORT={port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port)

