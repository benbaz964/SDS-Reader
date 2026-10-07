"""Minimal mock Ollama server to validate llm_reconcile.py's HTTP plumbing
(request format, response parsing, JSON-mode recovery, error handling)
without needing a real Ollama install, which isn't reachable in this sandbox."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

MOCK_MODEL = "llama3.1:8b"

# Controls what /api/generate returns, so tests can simulate different model behaviors.
MOCK_RESPONSE_MODE = {"mode": "breakdown_good"}


class MockOllamaHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # silence

    def do_GET(self):
        if self.path == "/api/tags":
            body = json.dumps({"models": [{"name": MOCK_MODEL}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        req = json.loads(raw.decode("utf-8"))

        mode = MOCK_RESPONSE_MODE["mode"]
        if mode == "breakdown_good":
            inner = json.dumps({"ingredients": [
                {"name": "Cadmium oxide", "carcinogenic": "Yes", "mutagenic": "Not stated",
                 "reproductive_toxicant": "Not stated", "sensitiser": "Not stated"},
                {"name": "Zinc chloride", "carcinogenic": "No", "mutagenic": "No",
                 "reproductive_toxicant": "Not stated", "sensitiser": "Not stated"},
            ]})
        elif mode == "repair_good":
            inner = json.dumps({"Exposure limits": "TWA: 0.5 mg/m3 (8hr), STEL: 1.5 mg/m3 (15min)"})
        elif mode == "wrapped_in_fence":
            inner = "```json\n" + json.dumps({"Exposure limits": "cleaned value"}) + "\n```"
        elif mode == "malformed":
            inner = "not valid json at all {{{"
        elif mode == "empty":
            inner = ""
        else:
            inner = "{}"

        body = json.dumps({"model": req.get("model"), "response": inner, "done": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def start_mock_server(port=11499):
    server = HTTPServer(("127.0.0.1", port), MockOllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
