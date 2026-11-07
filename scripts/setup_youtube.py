#!/usr/bin/env python3
"""
YouTube OAuth2 Setup Helper for Erika YouTube Shorts Automation
---------------------------------------------------------------
This interactive script helps you connect your YouTube account in 2 minutes:
1. Prompts for your Google OAuth Client ID & Client Secret (from Google Cloud Console)
2. Opens the Google consent screen in your browser
3. Automatically captures the auth code via a local callback server
4. Exchanges it for a permanent Refresh Token
5. Displays the exact GitHub Secrets to add to your repository
"""

import sys
import webbrowser
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube.upload"
REDIRECT_URI = "http://localhost:8080"

auth_code = None


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if "code" in qs:
            auth_code = qs["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html = """
            <html>
            <body style="font-family:sans-serif;text-align:center;padding:50px;background:#0f172a;color:#f8fafc;">
                <h1 style="color:#10b981;">✓ Authorization Successful!</h1>
                <p>You can close this tab and return to your terminal.</p>
            </body>
            </html>
            """
            self.wfile.write(html.encode("utf-8"))
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Authorization failed or cancelled.")

    def log_message(self, format, *args):
        pass  # Quiet down server logs


def main():
    print("\n" + "=" * 65)
    print("   YouTube Shorts Automation Setup (Erika)")
    print("=" * 65)
    print("\n[Step 1] Google Cloud Console Prerequisites:")
    print("  1. Go to: https://console.cloud.google.com/")
    print("  2. Create or select a project (e.g. 'Erika-Shorts').")
    print("  3. Go to 'Enabled APIs & Services' -> Click '+ ENABLE APIS'")
    print("     Search for 'YouTube Data API v3' and click ENABLE.")
    print("  4. Go to 'OAuth consent screen':")
    print("     - User Type: External -> Create")
    print("     - App name: 'Erika Bot' -> Add your email")
    print("     - In 'Test users': Add your Google/YouTube email address.")
    print("  5. Go to 'Credentials' -> Click '+ CREATE CREDENTIALS' -> 'OAuth client ID'")
    print("     - Application type: 'Web application' or 'Desktop app'")
    print("     - Authorized redirect URIs: http://localhost:8080")
    print("\n" + "-" * 65)

    client_id = input("\nEnter your Google OAuth Client ID: ").strip()
    if not client_id:
        print("Error: Client ID cannot be empty.")
        return

    client_secret = input("Enter your Google OAuth Client Secret: ").strip()
    if not client_secret:
        print("Error: Client Secret cannot be empty.")
        return

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"

    print("\n" + "-" * 65)
    print("[Step 2] Authorize with your YouTube Account:")
    print("Opening browser for authorization...")
    print(f"If browser does not open automatically, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", 8080), OAuthCallbackHandler)
    print("Waiting for authorization callback on http://localhost:8080...")

    while not auth_code:
        server.handle_request()

    print("\n[Step 3] Exchanging authorization code for permanent Refresh Token...")
    token_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": auth_code,
        "grant_type": "authorization_code",
        "redirect_uri": REDIRECT_URI,
    }
    res = requests.post(TOKEN_ENDPOINT, data=token_data, timeout=30)
    if res.status_code != 200:
        print(f"Error fetching tokens ({res.status_code}): {res.text}")
        return

    tokens = res.json()
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("Error: No refresh token returned. Did you accept consent?")
        return

    print("\n" + "=" * 65)
    print("   SUCCESS! YOUR YOUTUBE SECRETS ARE READY")
    print("=" * 65)
    print("\nAdd these 3 secrets to your GitHub Repository:")
    print("Go to: GitHub Repo -> Settings -> Secrets and variables -> Actions -> 'New repository secret'\n")
    print(f"  Name:  YOUTUBE_CLIENT_ID")
    print(f"  Value: {client_id}\n")
    print(f"  Name:  YOUTUBE_CLIENT_SECRET")
    print(f"  Value: {client_secret}\n")
    print(f"  Name:  YOUTUBE_REFRESH_TOKEN")
    print(f"  Value: {refresh_token}\n")
    print("=" * 65)
    print("Once added, every reel posted will automatically cross-post to YouTube Shorts!")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
