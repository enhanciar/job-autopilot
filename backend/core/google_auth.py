"""One-time Google OAuth (Gmail send/read + Sheets + Drive). Needs data/google_client_secret.json from the user's Cloud project.
Token is cached at data/google_token.json. Nothing here runs until the client secret exists."""
from __future__ import annotations
from backend.core import config

CLIENT = config.DATA / "google_client_secret.json"
TOKEN = config.DATA / "google_token.json"
SERVICE_ACCOUNT = config.DATA / "google_service_account.json"   # for Sheets/Drive only; Gmail needs the OAuth client
SCOPES = ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.file"]


def configured() -> bool:
    return CLIENT.exists()


def sheets_configured() -> bool:
    return SERVICE_ACCOUNT.exists() or CLIENT.exists()


def sheets_credentials():
    """Service account if available (share the sheet with its email), else the user OAuth flow."""
    if SERVICE_ACCOUNT.exists():
        from google.oauth2 import service_account
        return service_account.Credentials.from_service_account_file(str(SERVICE_ACCOUNT), scopes=["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.file"])
    return credentials()


def authorized() -> bool:
    return TOKEN.exists()


def credentials():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request()); TOKEN.write_text(creds.to_json()); return creds
    if creds and creds.valid:
        return creds
    if not CLIENT.exists():
        raise RuntimeError("data/google_client_secret.json missing: create an OAuth client (Desktop app) in Google Cloud with Gmail, Sheets, Drive APIs enabled")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")  # opens the browser once for shanumishra199@gmail.com
    TOKEN.write_text(creds.to_json())
    return creds
