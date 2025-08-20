# test_watchdog.py
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import logging

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class Handler(FileSystemEventHandler):
    def on_created(self, event):
        logger.debug(f"File created: {event.src_path}")

if __name__ == "__main__":
    folder_path = "/home/umar/personal/pdf-invoice-automator/data"
    logger.debug(f"Setting up observer for {folder_path}")
    observer = Observer()
    observer.schedule(Handler(), path=folder_path, recursive=False)
    logger.debug("Starting observer")
    observer.start()
    logger.debug("Observer started")
    time.sleep(10)
    observer.stop()
    observer.join()
    logger.debug("Observer stopped")
