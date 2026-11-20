"""Server-side Google OpenID Connect with an authorization code and PKCE."""
import base64
import hashlib
import os
import re
import secrets
from urllib.parse import urlencode


CALLBACK_PATH = "/api/session/google/callback"
AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleLoginError(RuntimeError):
    pass


class GoogleLogin:
    def __init__(self, client_id, client_secret, origin):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = origin + CALLBACK_PATH

    @classmethod
    def configured(cls, origin):
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
        if client_id and client_secret:
            return cls(client_id, client_secret, origin)
        return None

    def authorization_url(self, state, nonce, verifier):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return AUTHORIZATION_URL + "?" + urlencode({
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "openid profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })

    def exchange(self, code, nonce, verifier):
        from functools import partial
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import Request
        from google.oauth2.id_token import verify_oauth2_token
        import requests

        try:
            with requests.Session() as session:
                response = session.post(TOKEN_URL, data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "code_verifier": verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": self.redirect_uri,
                }, timeout=8, allow_redirects=False)
                if response.status_code != 200:
                    raise GoogleLoginError("Google rejected the authorization code.")
                token = response.json().get("id_token")
                if not isinstance(token, str) or len(token) > 12000:
                    raise GoogleLoginError("Google did not return an ID token.")
                claims = verify_oauth2_token(token, partial(Request(session=session), timeout=8), audience=self.client_id)
        except (requests.RequestException, GoogleAuthError, ValueError, AttributeError):
            raise GoogleLoginError("Google sign-in could not be verified.") from None
        returned_nonce = claims.get("nonce")
        subject = claims.get("sub")
        if (not isinstance(returned_nonce, str) or not returned_nonce.isascii()
                or not secrets.compare_digest(returned_nonce, nonce)
                or claims.get("azp", self.client_id) != self.client_id
                or not isinstance(subject, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", subject)):
            raise GoogleLoginError("Google returned an unexpected sign-in identity.")
        return subject, claims.get("name")
