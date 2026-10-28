from __future__ import annotations

import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path


HOST = "127.0.0.1"
PORT = 8000
URL = f"http://{HOST}:{PORT}/"


def port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_for_server(host: str, port: int, timeout: float = 20.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if port_is_open(host, port):
            return True
        time.sleep(0.25)
    return False


def main() -> None:
    project_root = Path(__file__).resolve().parent
    web_dir = project_root / "web"
    manage_py = web_dir / "manage.py"

    if not manage_py.exists():
        raise FileNotFoundError(f"Could not find manage.py at: {manage_py}")

    if port_is_open(HOST, PORT):
        print(f"PhishViz already appears to be running at {URL}")
        webbrowser.open(URL)
        return

    python_exe = sys.executable

    print("Starting PhishViz launcher...")
    print("Applying migrations if needed...")
    subprocess.run(
        [python_exe, "manage.py", "migrate", "--noinput"],
        cwd=web_dir,
        check=True,
    )

    print("Starting local web server...")
    server_process = subprocess.Popen(
        [python_exe, "manage.py", "runserver", f"{HOST}:{PORT}", "--noreload"],
        cwd=web_dir,
    )

    try:
        if wait_for_server(HOST, PORT):
            print(f"Opening browser at {URL}")
            webbrowser.open(URL)
        else:
            print("Warning: server did not become ready in time.")
            print(f"You can still try opening {URL} manually.")

        print()
        print("PhishViz is running.")
        print("Close this window or press Ctrl+C to stop the local server.")

        server_process.wait()

    except KeyboardInterrupt:
        print("\nStopping PhishViz...")
        server_process.terminate()
        try:
            server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_process.kill()
        print("PhishViz stopped.")


if __name__ == "__main__":
    main()
