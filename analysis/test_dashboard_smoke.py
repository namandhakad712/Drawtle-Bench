"""Quick smoke test for the new test_only probe behaviour (server-backed)."""
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.parse
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from web.server import serve, _Handler
import socketserver


def run_server(port):
    os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
    httpd = socketserver.TCPServer(("127.0.0.1", port), _Handler)
    httpd.serve_forever()


def main():
    port = 18765
    t = threading.Thread(target=run_server, args=(port,), daemon=True)
    t.start()
    time.sleep(1.5)
    try:
        # Get registry to find a discoverable provider
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/registry", timeout=5) as r:
            reg = json.loads(r.read())
        providers = reg["providers"]
        target = None
        for p in providers:
            if p.get("models_url"):
                target = p["name"]
                break
        if not target:
            print("SKIP: no discoverable provider")
            return

        # Full probe
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/probe?provider={urllib.parse.quote(target)}", timeout=20) as r:
            full = json.loads(r.read())
        assert "models" in full, "full probe missing models"

        # Test-only probe
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/probe?provider={urllib.parse.quote(target)}&test_only=1", timeout=20) as r:
            test = json.loads(r.read())
        assert "models" not in test, "test_only probe should not contain models"
        assert test.get("ok") == full.get("ok")
        assert test.get("provider") == full.get("provider")
        print(f"PASS: /api/probe test_only=1 strips models for {target}")
    finally:
        pass


if __name__ == "__main__":
    main()
