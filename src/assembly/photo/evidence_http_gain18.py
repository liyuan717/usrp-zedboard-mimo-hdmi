"""Short-lived read-only gain18 evidence endpoint for the direct host."""

from pathlib import Path
import http.server
import shutil
import time

ROOT = Path("/tmp/link-share/photo-seu-reproduce-r2-au-hdmi")
NAMES = {
    "new-adc-output.rxth",
    "new-adc-output.rxth.failure.rxth",
    "received-live.rfd",
    "result.json",
    "native.log",
    "video.log",
    "video-report.json",
    "hdmi-proof.json",
    "kernel-before.log",
    "kernel-after.log",
    "rxth-before.json",
    "rxth-after.json",
    "interrupts-after.log",
    "health-samples.json",
    "supervisor.log",
}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.client_address[0] != "169.254.209.83" or not self.path.startswith(
            "/photo-seu-reproduce-r2/"
        ):
            self.send_error(403)
            return
        name = self.path[len("/photo-seu-reproduce-r2/") :]
        path = (
            Path("/tmp/link-share/photo-seu-reproduce-r2-supervisor.log")
            if name == "supervisor.log"
            else ROOT / name
        )
        if (
            name not in NAMES
            or not path.is_file()
            or path.is_symlink()
            or path.stat().st_size > 32 * 1024 * 1024
        ):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as stream:
            shutil.copyfileobj(stream, self.wfile, 65536)

    def log_message(self, *args):
        pass


class Server(http.server.HTTPServer):
    allow_reuse_address = True


with Server(("169.254.209.84", 8001), Handler) as server:
    server.timeout = 0.25
    deadline = time.monotonic() + 300
    print("FINITE_VIDEO_R1_HTTP_READY", flush=True)
    while (
        time.monotonic() < deadline
        and not Path("/tmp/link-live/photo-seu-reproduce-r2-http.stop").exists()
    ):
        server.handle_request()
