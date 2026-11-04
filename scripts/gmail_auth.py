"""One-time Gmail sign-in for the support mailbox.

Opens the browser, you approve access, and Google redirects to a temporary localhost port
on this machine. The resulting token is saved to secrets/gmail_token.json (git-ignored)
and refreshed automatically by the Gmail MCP server afterwards.

    python scripts/gmail_auth.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from mcp_servers.gmail_server import CLIENT_FILE, SCOPES, TOKEN_FILE, gmail  # noqa: E402


def main() -> None:
    if not CLIENT_FILE.exists():
        sys.exit(f"Missing {CLIENT_FILE}. Download the Desktop-app OAuth client JSON from Google Cloud first.")
    creds = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES).run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    profile = gmail().users().getProfile(userId="me").execute()
    print(f"Authorised {profile['emailAddress']}. Token saved to {TOKEN_FILE}.")


if __name__ == "__main__":
    main()
