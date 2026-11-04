"""Create a password hash for DESK_PASSWORD_HASH and a random SECRET_KEY.

Usage:  python scripts/hash_password.py
"""

from __future__ import annotations

import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.security import hash_password  # noqa: E402


def main() -> None:
    password = getpass.getpass("New support dashboard password: ")
    if len(password) < 10:
        sys.exit("Use at least 10 characters.")
    if password != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")
    print("\nAdd these lines to your .env file:\n")
    print(f"DESK_PASSWORD_HASH={hash_password(password)}")
    print(f"SECRET_KEY={secrets.token_urlsafe(32)}")


if __name__ == "__main__":
    main()
