#!/usr/bin/env python3
"""Start the host's authenticated, fixed-function read-only query broker."""
from pathlib import Path
import os
import sys

if sys.version_info < (3, 12):
    print("The query broker requires Python 3.12 or newer.", file=sys.stderr)
    raise SystemExit(1)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from octopus.query_host import QueryHost, QueryHTTPServer, read_token


def main():
    token_path = PROJECT_ROOT / ".secrets" / "query_token"
    code_root = Path(os.environ.get("OCTOPUS_CODE_ROOT", str(Path.home() / "Projects" / "rere")))
    port = int(os.environ.get("OCTOPUS_QUERY_PORT", "4501"))
    if not 1 <= port <= 65535:
        raise ValueError("query broker port must be between 1 and 65535")
    host = QueryHost(code_root, dms_policy_path=PROJECT_ROOT / ".secrets" / "dms-policy.json")
    try:
        with QueryHTTPServer(("127.0.0.1", port), host, read_token(token_path)) as server:
            print("Read-only query broker listening on 127.0.0.1:%d" % port, flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
    finally:
        host.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Query broker failed to start; check the host configuration and private token file.", file=sys.stderr)
        raise SystemExit(1)
