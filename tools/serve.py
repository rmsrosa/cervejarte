"""Local preview server that tells the browser not to cache anything.

Usage (from the repo root):  python3 tools/serve.py [folder] [port]
Defaults: the repo root (the folder containing tools/), port 8000.
Then open http://localhost:8000/blog/  — stop with Ctrl+C.
"""
import functools, http.server, os, sys

here = os.path.dirname(os.path.realpath(__file__))
root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..")
port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000


class NoCache(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


handler = functools.partial(NoCache, directory=os.path.abspath(root))
print("Serving %s at http://localhost:%d/blog/ (no cache). Ctrl+C to stop." % (os.path.abspath(root), port))
http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
