"""Setup authentication for Shikimori integration."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUTH_FILE = Path(__file__).resolve().parent / "shiki_auth.json"
TIMEOUT = 10


def main() -> None:
    """Run interactive CLI auth setup for Shikimori."""
    print("=== Shikimori MPV Updater Setup ===")
    print("To use this script, you need a Shikimori Client ID and Client Secret.")
    print("1. Go to https://shikimori.io/oauth/applications")
    print("2. Create a new Application (Redirect URI: 'urn:ietf:wg:oauth:2.0:oob', Scopes: 'user_rates')")
    print("3. Copy the Application Client ID and Client Secret and paste them below.\n")

    client_id = input("Enter your Shikimori Client ID: ").strip()
    client_secret = input("Enter your Shikimori Client Secret: ").strip()
    if not client_id or not client_secret:
        print("Client ID and Client Secret cannot be empty. Exiting.")
        return

    auth_url = (
        "https://shikimori.io/oauth/authorize"
        f"?client_id={urllib.parse.quote(client_id)}"
        "&redirect_uri=urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob&response_type=code&scope=user_rates"
    )

    print("\n--- AUTHORIZATION STEP ---")
    print("1. Go to this URL in your web browser and click 'Authorize':\n")
    print(auth_url)
    print("\n2. After allowing, copy the authorization code displayed on the page.")
    print("3. Paste the authorization code below.\n")

    auth_code = input("Paste the code here: ").strip()
    if not auth_code:
        print("Error: Authorization code cannot be empty.")
        return

    print("\nRequesting tokens from Shikimori...")
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": auth_code,
            "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        }
    ).encode()
    request = urllib.request.Request(
        "https://shikimori.io/oauth/token",
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

    access_token = token_data["access_token"]
    refresh_token = token_data["refresh_token"]

    print("Fetching user ID from Shikimori...")
    whoami_req = urllib.request.Request(
        "https://shikimori.io/api/users/whoami",
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": "mpv-anilist-updater"},
    )
    user_id = None
    try:
        with urllib.request.urlopen(whoami_req, timeout=TIMEOUT) as whoami_res:  # ruff: ignore[suspicious-url-open-usage]
            user_data = json.load(whoami_res)
            user_id = user_data.get("id")
            print(f"Successfully retrieved Shikimori User ID: {user_id}")
    except urllib.error.HTTPError as e:
        print("Warning: Failed to fetch user ID via whoami API.")
        print(e.read().decode(errors="replace"))
    except OSError as e:
        print(f"Warning: Failed to fetch user ID via whoami API: {e}")

    auth_payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "user_id": user_id,
    }
    if "expires_in" in token_data:
        auth_payload["expires_at"] = int(time.time()) + int(token_data["expires_in"])

    AUTH_FILE.write_text(json.dumps(auth_payload, indent=4), encoding="utf-8")
    print(f"\nSuccess! Authentication data saved to {AUTH_FILE}")
    print("You are now ready to use Shikimori integration!")


if __name__ == "__main__":
    main()
