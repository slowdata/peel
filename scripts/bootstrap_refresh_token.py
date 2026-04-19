#!/usr/bin/env python3
"""Bootstrap script para gerar o refresh token do Spotify.

USAR UMA ÚNICA VEZ, localmente, para autorizar a app e obter o refresh_token.

Fluxo:
1. Script abre o browser em http://127.0.0.1:8888/callback
2. Tu autorizas a app (clicas "Accept")
3. O Spotify redireciona para a callback URL com um authorization code
4. Script troca o code por refresh_token e imprime-o
5. Tu colas o refresh_token no .env

Requisitos:
- Ter SPOTIFY_CLIENT_ID e SPOTIFY_CLIENT_SECRET válidos (criar em
  https://developer.spotify.com/dashboard).
- A Redirect URI http://127.0.0.1:8888/callback tem de estar registada
  no Dashboard.

NÃO commita este script com tokens reais. .env fica .gitignore'd.
"""

import json
import os
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

# Constantes de configuração
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-modify-private playlist-modify-public"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"


class CallbackHandler(BaseHTTPRequestHandler):
    """Handler HTTP que recebe o authorization code na redirect URI."""

    auth_code: str | None = None

    def do_GET(self) -> None:
        """Recebe GET /callback?code=... ou /callback?error=..."""
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "error" in params:
            error = params["error"][0]
            self.send_response(400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"Error: {error}".encode())
            print(f"\n✗ Authorization failed: {error}")
            return

        if "code" in params:
            CallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html = """
            <html><body>
            <h1>✓ Authorization successful!</h1>
            <p>You can close this window. The script will print your refresh token.</p>
            </body></html>
            """
            self.wfile.write(html.encode())
            print("\n✓ Authorization code received. Exchanging for refresh token...")
            return

        # Sem code ou error — invalid request
        self.send_response(400)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Invalid callback request")

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default HTTP logging."""
        pass


def main() -> None:
    """Orquestra o fluxo de autorização."""
    # Carrega Client ID e Secret: prefere variáveis de env, senão pede input
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    if not client_id:
        client_id = input("Spotify Client ID: ").strip()

    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_secret:
        client_secret = input("Spotify Client Secret: ").strip()

    print("\n" + "=" * 70)
    print("Spotify Refresh Token Bootstrap")
    print("=" * 70)

    # 1. Constrói a URL de autorização
    auth_params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
    }
    auth_url = f"{AUTH_URL}?{urlencode(auth_params)}"

    # 2. Inicia o HTTP server
    server = HTTPServer(("127.0.0.1", 8888), CallbackHandler)
    print(f"\n1. Starting callback server at {REDIRECT_URI}")
    print("   (server will wait for authorization code)")

    # 3. Abre o browser para autorização
    print("\n2. Opening browser for authorization...\n")
    webbrowser.open(auth_url)

    # 4. Aguarda o callback (receive authorization code)
    print("   Waiting for authorization callback...")
    while CallbackHandler.auth_code is None:
        server.handle_request()
    auth_code = CallbackHandler.auth_code
    server.server_close()

    # 5. Troca authorization code por refresh_token
    print("\n3. Exchanging authorization code for refresh token...")
    token_data = {
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "client_secret": client_secret,
    }

    response = httpx.post(TOKEN_URL, data=token_data, timeout=10)
    if response.status_code != 200:
        print(f"✗ Token exchange failed: {response.status_code}")
        print(f"  Response: {response.text}")
        return

    tokens = response.json()
    refresh_token = tokens.get("refresh_token")

    if not refresh_token:
        print("✗ No refresh_token in response. Check your scopes and app settings.")
        print(f"  Response: {json.dumps(tokens, indent=2)}")
        return

    # 6. Imprime o refresh token
    print("\n" + "=" * 70)
    print("✓ SUCCESS! Here's your refresh token:\n")
    print(f"SPOTIFY_REFRESH_TOKEN={refresh_token}\n")
    print("=" * 70)
    print("\nNext steps:")
    print("1. Copy the SPOTIFY_REFRESH_TOKEN above")
    print("2. Paste it into your .env file")
    print("3. Make sure SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are also in .env")
    print("4. Delete this script or save it safely — não o commites com tokens reais!\n")


if __name__ == "__main__":
    main()
