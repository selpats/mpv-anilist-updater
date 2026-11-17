"""Setup authentication for MyAnimeList integration."""

import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUTH_FILE = Path(__file__).resolve().parent / "mal_auth.json"
TIMEOUT = 10


def main() -> None:
    """Run interactive CLI auth setup for MyAnimeList."""
    print("=== MyAnimeList MPV Updater Setup ===")
    print("To use this script, you need a MAL Client ID.")
    print("1. Go to https://myanimelist.net/apiconfig")
    print("2. Create a new App (App Type: 'other', Redirect URL: 'http://localhost')")
    print("3. Copy the Client ID and paste it below.\n")

    client_id = input("Enter your MAL Client ID: ").strip()
    if not client_id:
        print("Client ID cannot be empty. Exiting.")
        return

    code_verifier = secrets.token_urlsafe(96)[:128]
    auth_url = (
        "https://myanimelist.net/v1/oauth2/authorize"
        f"?response_type=code&client_id={urllib.parse.quote(client_id)}&code_challenge={code_verifier}"
    )

    print("\n--- AUTHORIZATION STEP ---")
    print("1. Go to this URL in your web browser and click 'Allow':\n")
    print(auth_url)
    print("\n2. After allowing, you will be redirected to an empty/broken localhost page.")
    print("3. Copy the ENTIRE URL from your browser's address bar and paste it below.\n")

    redirect_url = input("Paste the redirected URL here: ").strip()
    try:
        parsed_url = urllib.parse.urlparse(redirect_url)
        auth_code = urllib.parse.parse_qs(parsed_url.query).get("code", [None])[0]
    except Exception:
        auth_code = None

    if not auth_code:
        print(
            "Error: Could not find the authorization code in the URL. Make sure you pasted the full http://localhost... URL."
        )
        return

    print("\nRequesting tokens from MAL...")
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "code": auth_code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
        }
    ).encode()
    request = urllib.request.Request(
        "https://myanimelist.net/v1/oauth2/token",
        body,
        {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "mpv-anilist-updater"},
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # ruff: ignore[suspicious-url-open-usage]
            token_data = json.load(response)
    except urllib.error.HTTPError as e:
        print(f"Failed to get token: {e.code}")
        print(e.read().decode(errors="replace"))
        return
    except OSError as e:
        print(f"Failed to get token: {e}")
        return

    auth_payload = {
        "client_id": client_id,
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
    }
    if "expires_in" in token_data:
        auth_payload["expires_at"] = int(time.time()) + int(token_data["expires_in"])

    AUTH_FILE.write_text(json.dumps(auth_payload, indent=4), encoding="utf-8")
    print(f"\nSuccess! Authentication data saved to {AUTH_FILE}")
    print("You are now ready to use the MPV script!")


if __name__ == "__main__":
    main()
