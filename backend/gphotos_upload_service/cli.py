"""Console-script entry point: `gphotos-upload-service`."""
from __future__ import annotations

import sys

from gphotos_upload_service.dbus_service import Service


def main() -> int:
    return Service().run()


if __name__ == "__main__":
    sys.exit(main())
