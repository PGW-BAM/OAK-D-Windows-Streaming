"""Entry point: starts the FastAPI backend with uvicorn."""
import threading
import time
import webbrowser
import uvicorn

URL = "http://localhost:8000"


def _open_browser() -> None:
    # Wait for uvicorn to finish binding before opening the browser
    time.sleep(1.5)
    webbrowser.open(URL)


if __name__ == "__main__":
    print(f"Starting OAK-D Dashboard → {URL}")
    threading.Thread(target=_open_browser, daemon=True).start()
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
