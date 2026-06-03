import argparse
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app import make_request_with_retry


HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Referer": "https://www.macrotrends.net/",
    "Accept-Language": "en-US,en;q=0.9",
}


def token_is_valid(handler: BaseHTTPRequestHandler) -> bool:
    expected = os.environ.get("MACROTRENDS_PROXY_TOKEN", "").strip()
    if not expected:
        return True
    auth = handler.headers.get("Authorization", "")
    header_token = handler.headers.get("X-Macrotrends-Proxy-Token", "")
    supplied = ""
    if auth.startswith("Bearer "):
        supplied = auth[7:].strip()
    supplied = supplied or header_token.strip()
    return supplied == expected


def is_allowed_macrotrends_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc == "www.macrotrends.net"


class MacrotrendsProxyHandler(BaseHTTPRequestHandler):
    max_retries = 2

    def write_text(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        encoded = body.encode("utf-8", errors="replace")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if not token_is_valid(self):
            self.write_text(401, "Unauthorized")
            return

        parsed = urlparse(self.path)
        if parsed.path not in {"/", "/fetch"}:
            self.write_text(404, "Not found")
            return

        target = parse_qs(parsed.query).get("url", [""])[0]
        if not target:
            self.write_text(400, "Missing url query parameter")
            return
        if not is_allowed_macrotrends_url(target):
            self.write_text(400, "Only https://www.macrotrends.net URLs are allowed")
            return

        logs = []
        html = make_request_with_retry(
            target,
            HEADERS,
            max_retries=self.max_retries,
            log=logs,
            use_proxy=False,
        )
        if not html:
            self.write_text(502, "\n".join(logs) or "Macrotrends fetch failed")
            return
        self.write_text(200, html, "text/html; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Proxy Macrotrends HTML fetches for the Streamlit dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--max-retries", type=int, default=2)
    args = parser.parse_args()

    MacrotrendsProxyHandler.max_retries = args.max_retries
    server = ThreadingHTTPServer((args.host, args.port), MacrotrendsProxyHandler)
    print(f"Macrotrends proxy listening on http://{args.host}:{args.port}/fetch")
    if os.environ.get("MACROTRENDS_PROXY_TOKEN"):
        print("Token auth is enabled.")
    server.serve_forever()


if __name__ == "__main__":
    main()
