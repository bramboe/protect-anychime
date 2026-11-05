#!/usr/bin/env python3
"""Private udhcpc hook. Never invokes a system DHCP/DNS/network hook."""
import os
import sys
from pathlib import Path
from network import handle_dhcp, NetworkError, _write


def main():
    config = os.environ.get("ANYCHIME_NETWORK_CONFIG")
    if not config or len(sys.argv) != 2:
        return 1
    try:
        handle_dhcp(config, sys.argv[1], os.environ)
        return 0
    except Exception as exc:
        # No environment dump: DHCP must not inherit or expose application secrets.
        try:
            _write(Path(config).parent / "hook-error.json", {"reason": str(exc)[:240]})
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
