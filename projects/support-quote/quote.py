"""Read-only synthetic quote data for a LibreChat Action example."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit


class QuoteHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        request = urlsplit(self.path)
        if request.path == "/healthz":
            self.send_json(200, {"ok": True})
        elif request.path != "/v1/quote":
            self.send_json(404, {"error": "Unknown endpoint"})
        elif parse_qs(request.query).get("plan") != ["BLUE-SEAL-3"]:
            self.send_json(404, {"error": "Unknown demo plan"})
        else:
            self.send_json(
                200, {"base_cny": 137, "quote_id": "QUOT-731", "demo": True}
            )

    def send_json(self, status, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class QuoteServer(HTTPServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(3)
        return connection, address


if __name__ == "__main__":
    with QuoteServer(("0.0.0.0", 8080), QuoteHandler) as server:
        server.serve_forever()
