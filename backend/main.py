import os
import logging
import asyncio
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from threading import Thread, Lock
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import fitz  # PyMuPDF
import pytesseract
from PIL import Image
import re
from datetime import datetime

# Set up logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI()

# CORS for frontend communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# State management (thread-safe)
state = {
    "is_watching": False,
    "stats": {"files_processed": 0, "errors": 0},
    "logs": [],
    "observer": None,
    "folder_path": None,
    "observer_thread": None
}
state_lock = Lock()
log_id_counter = 0

def add_log(message):
    global log_id_counter
    with state_lock:
        log_id_counter += 1
        state["logs"].append({
            "id": log_id_counter,
            "timestamp": datetime.now().isoformat(),
            "message": message
        })
    logger.info(f"Log added: {message}")

# PDF Processing Functions
class Step:
    def run(self, data):
        pass

class Loader(Step):
    def __init__(self, path):
        self.path = path

    def run(self, data):
        logger.debug(f"Loading PDF: {self.path}")
        try:
            if not os.path.isfile(self.path):
                logger.error(f"File does not exist: {self.path}")
                raise FileNotFoundError(f"File does not exist: {self.path}")
            doc = fitz.open(self.path)
            aggregated = []
            for page in doc:
                text = page.get_text().strip()
                if text:
                    aggregated.append(text)
                else:
                    pix = page.get_pixmap(dpi=200)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    text = pytesseract.image_to_string(img)
                    aggregated.append(text)
            doc.close()
            data['text'] = "\n".join(aggregated)
            logger.debug(f"Extracted text: {data['text'][:100]}...")  # Log first 100 chars
            return data
        except Exception as e:
            logger.error(f"Error loading PDF {self.path}: {str(e)}")
            raise

class Parser(Step):
    def run(self, data):
        logger.debug("Parsing PDF text")
        try:
            text = data['text'].replace("\n", " ")
            # Extract amount
            amount_re = re.compile(r"(?<!\d)(?:USD|EUR|\$)?\s?([\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\b")
            m = amount_re.search(text)
            data['amount'] = float(m.group(1).replace(',', '')) if m else None
            # Extract vendor
            vendor_re = re.compile(r"(?:Vendor|Company|Supplier):?\s*([A-Za-z\s]+?)(?:\n|$)", re.IGNORECASE)
            m = vendor_re.search(data['text'])
            data['vendor'] = m.group(1).strip() if m else "Unknown"
            # Extract date
            date_re = re.compile(r"(?:Date|Invoice Date):?\s*(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})", re.IGNORECASE)
            m = date_re.search(data['text'])
            data['date'] = m.group(1) if m else "Unknown"
            logger.debug(f"Parsed data: amount={data['amount']}, vendor={data['vendor']}, date={data['date']}")
            return data
        except Exception as e:
            logger.error(f"Error parsing PDF text: {str(e)}")
            raise

class Pipeline:
    def __init__(self, steps):
        self.steps = steps

    def execute(self, initial):
        data = initial
        for step in self.steps:
            data = step.run(data)
        return data

def process_pdf(path):
    logger.debug(f"Attempting to process PDF: {path}")
    try:
        # Wait briefly to ensure file is fully written (e.g., after .crdownload rename)
        time.sleep(1)
        if not path.lower().endswith(".pdf"):
            logger.debug(f"Skipping non-PDF file: {path}")
            return
        if not os.path.isfile(path):
            logger.error(f"File not found during processing: {path}")
            return
        steps = [Loader(path), Parser()]
        result = Pipeline(steps).execute({})
        add_log(f"Processed {path}: Amount = {result.get('amount')}, Vendor = {result.get('vendor')}, Date = {result.get('date')}")
        with state_lock:
            state["stats"]["files_processed"] += 1
    except Exception as e:
        logger.error(f"Error processing {path}: {str(e)}")
        add_log(f"Error processing {path}: {str(e)}")
        with state_lock:
            state["stats"]["errors"] += 1

class PDFHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.src_path.lower().endswith(".pdf"):
            logger.debug(f"PDF detected: {event.src_path}")
            add_log(f"New PDF detected: {event.src_path}")
            process_pdf(event.src_path)
    def on_moved(self, event):
        if event.dest_path.lower().endswith(".pdf"):
            logger.debug(f"PDF renamed to: {event.dest_path}")
            add_log(f"PDF renamed to: {event.dest_path}")
            process_pdf(event.dest_path)

@app.get("/health")
async def health():
    logger.debug("Health check requested")
    return {"status": "ok"}

@app.post("/start-watching")
async def start_watching(body: dict):
    folder_path = body.get("folder_path")
    sheet_url = body.get("sheet_url")
    logger.debug(f"Start watching requested for folder: {folder_path}, sheet_url: {sheet_url}")
    if not folder_path:
        logger.error("Folder path is required")
        raise HTTPException(status_code=400, detail="Folder path is required")
    if not os.path.isdir(folder_path):
        logger.error(f"Folder does not exist: {folder_path}")
        raise HTTPException(status_code=400, detail=f"Folder does not exist: {folder_path}")
    try:
        logger.debug(f"Checking permissions for {folder_path}")
        if not os.access(folder_path, os.R_OK | os.W_OK):
            logger.error(f"Permission denied for {folder_path}")
            raise HTTPException(status_code=403, detail=f"Permission denied for folder: {folder_path}")
    except Exception as e:
        logger.error(f"Permission check failed: {str(e)}")
        raise HTTPException(status_code=403, detail=f"Permission check failed: {str(e)}")
    with state_lock:
        if state["is_watching"]:
            logger.error("Already watching")
            raise HTTPException(status_code=400, detail="Already watching")
        # Clean up any stale observer
        if state["observer"] is not None:
            try:
                state["observer"].stop()
                state["observer"].join(timeout=2.0)
                logger.debug("Cleaned up stale observer")
            except:
                logger.warning("Failed to clean up stale observer")
            state["observer"] = None
            state["observer_thread"] = None
        logger.debug(f"Setting up observer for {folder_path}")
        state["folder_path"] = folder_path
        state["sheet_url"] = sheet_url
        observer = Observer()
        handler = PDFHandler()
        try:
            observer.schedule(handler, path=folder_path, recursive=False)
            logger.debug("Starting observer in async-friendly way")
            # Start observer in a daemon thread
            def run_observer():
                try:
                    observer.start()
                    logger.debug("Observer running in thread")
                    while observer.is_alive():
                        time.sleep(0.5)
                except Exception as e:
                    logger.error(f"Observer failed in thread: {str(e)}")
                    observer.stop()
            thread = Thread(target=run_observer, daemon=True)
            thread.start()
            # Wait up to 3 seconds for observer to start
            start_time = time.time()
            while time.time() - start_time < 3:
                if observer.is_alive():
                    break
                time.sleep(0.1)
            if not observer.is_alive():
                logger.error("Observer failed to start")
                try:
                    observer.stop()
                    observer.join(timeout=1.0)
                except:
                    pass
                thread.join(timeout=1.0)
                raise Exception("Observer failed to start")
            state["observer"] = observer
            state["observer_thread"] = thread
            state["is_watching"] = True
            add_log("Watcher started")
            logger.info("Watcher started successfully")
            return {"status": "success"}
        except Exception as e:
            logger.error(f"Failed to start observer: {str(e)}")
            try:
                observer.stop()
                observer.join(timeout=1.0)
            except:
                pass
            thread.join(timeout=1.0)
            raise HTTPException(status_code=500, detail=f"Failed to start watcher: {str(e)}")

@app.post("/stop-watching")
async def stop_watching():
    logger.debug("Stop watching requested")
    with state_lock:
        if not state["is_watching"]:
            logger.error("Not watching")
            raise HTTPException(status_code=400, detail="Not watching")
        try:
            if state["observer"] is not None:
                state["observer"].stop()
                state["observer"].join(timeout=3.0)
                if state["observer"].is_alive():
                    logger.error("Observer failed to stop within timeout")
                    raise Exception("Observer failed to stop")
            state["is_watching"] = False
            state["observer"] = None
            state["observer_thread"] = None
            add_log("Watcher stopped")
            logger.info("Watcher stopped successfully")
            return {"status": "success"}
        except Exception as e:
            logger.error(f"Failed to stop observer: {str(e)}")
            raise HTTPException(status_code=500, detail=f"Failed to stop watcher: {str(e)}")

@app.get("/status")
async def get_status():
    logger.debug("Status requested")
    with state_lock:
        return {
            "is_watching": state["is_watching"],
            "stats": state["stats"],
            "logs": state["logs"]
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
