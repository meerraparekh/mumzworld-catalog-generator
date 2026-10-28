"""Start the Mumzworld catalog generator web app: python run.py [--port 5050]"""
import argparse

from mumzworld_catalog.web.app import create_app

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5050)
    args = ap.parse_args()
    create_app().run(host=args.host, port=args.port, threaded=True, debug=False)
