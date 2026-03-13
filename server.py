import http.server
import socketserver
import os

PORT = int(os.environ.get("PORT", 8080))

class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {
        ".html": "text/html",
        ".css": "text/css",
        ".js": "application/javascript",
        ".json": "application/json",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".webp": "image/webp",
        "": "application/octet-stream",
    }

    def do_GET(self):
        # Serve clean URLs: /services -> /services.html
        path = self.path.split("?")[0].split("#")[0]
        if path != "/" and "." not in path.split("/")[-1]:
            file_path = path.lstrip("/") + ".html"
            if os.path.isfile(file_path):
                self.path = "/" + file_path
        super().do_GET()

with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"Serving on port {PORT}")
    httpd.serve_forever()
