"""Synthetic upstream for the opt-in Docker acceptance job; never a model server."""
import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from check_litellm import Fixture  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or not args.key_file.is_file() or args.key_file.stat().st_size > 4096:
        raise SystemExit("Invalid synthetic upstream configuration")
    fixture = Fixture()
    fixture.key = args.key_file.read_text().strip()
    if not fixture.key:
        raise SystemExit("Missing synthetic upstream credential")
    server = ThreadingHTTPServer(("0.0.0.0", args.port), fixture.handler())
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        fixture.release.set()
        server.server_close()


if __name__ == "__main__":
    main()
