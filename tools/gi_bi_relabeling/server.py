"""Local server for the Blue/Green Infrastructure Relabeling page.

Standard-library only (no dependencies). Serves the static files in this folder and
adds two endpoints: POST /save_relabel persists a POI's manually-corrected tag to
relabels.json, POST /save_manual_merge persists a manually-defined fragment grouping
to manual_merges.json, both in this same folder.
"""

import http.server
import json
import os

PORT = 8766
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RELABELS_PATH = os.path.join(BASE_DIR, "relabels.json")
MANUAL_MERGES_PATH = os.path.join(BASE_DIR, "manual_merges.json")


def _load_json_dict(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_json_dict(path: str, data: dict) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_path, path)


class Handler(http.server.SimpleHTTPRequestHandler):
    ENDPOINTS = {
        "/save_relabel": (RELABELS_PATH, "source_key"),
        "/save_manual_merge": (MANUAL_MERGES_PATH, "group_id"),
    }

    def do_POST(self):
        endpoint = self.ENDPOINTS.get(self.path)
        if endpoint is None:
            self.send_error(404)
            return
        path, key_field = endpoint

        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
            key = payload[key_field]
        except Exception as exc:
            self.send_error(400, f"Bad request: {exc}")
            return

        data = _load_json_dict(path)
        data[key] = payload
        _save_json_dict(path, data)

        body = json.dumps({"status": "ok"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print(f"[server] {self.address_string()} - {format % args}")


if __name__ == "__main__":
    os.chdir(BASE_DIR)
    with http.server.HTTPServer(("localhost", PORT), Handler) as httpd:
        print(f"Serving on http://localhost:{PORT}")
        httpd.serve_forever()
