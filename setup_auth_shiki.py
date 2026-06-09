import json
import os
import urllib.parse
import requests

AUTH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shiki_auth.json")


def main():
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

    # Generate the Auth URL
    auth_url = f"https://shikimori.io/oauth/authorize?client_id={client_id}&redirect_uri=urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob&response_type=code&scope=user_rates"

    print("\n--- AUTHORIZATION STEP ---")
    print("1. Go to this URL in your web browser and click 'Authorize':\n")
    print(auth_url)
    print("\n2. After allowing, copy the authorization code displayed on the page.")
    print("3. Paste the authorization code below.\n")

    auth_code = input("Paste the code here: ").strip()

    if not auth_code:
        print("Error: Authorization code cannot be empty.")
        return

    # Exchange the code for tokens
    print("\nRequesting tokens from Shikimori...")
    headers = {
        "User-Agent": "mpv-anilist-updater",
    }
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": auth_code,
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
    }

    response = requests.post("https://shikimori.io/oauth/token", headers=headers, data=data, timeout=10)

    if response.status_code == 200:
        token_data = response.json()
        access_token = token_data["access_token"]
        refresh_token = token_data["refresh_token"]

        # Fetch the user_id via whoami
        print("Fetching user ID from Shikimori...")
        whoami_headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "mpv-anilist-updater",
        }
        whoami_response = requests.get("https://shikimori.io/api/users/whoami", headers=whoami_headers, timeout=10)
        
        user_id = None
        if whoami_response.status_code == 200:
            user_data = whoami_response.json()
            user_id = user_data.get("id")
            print(f"Successfully retrieved Shikimori User ID: {user_id}")
        else:
            print("Warning: Failed to fetch user ID via whoami API.")
            print(whoami_response.text)

        # Save everything to our auth file
        auth_payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "user_id": user_id
        }

        with open(AUTH_FILE, "w") as f:
            json.dump(auth_payload, f, indent=4)

        print(f"\nSuccess! Authentication data saved to {AUTH_FILE}")
        print("You are now ready to use Shikimori integration!")
    else:
        print(f"Failed to get token: {response.status_code}")
        print(response.text)


if __name__ == "__main__":
    main()
