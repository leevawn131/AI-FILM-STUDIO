"""
Optional Google Drive Cloud Backup & Asset Synchronization Service.
Enforces:
- Google Drive is strictly optional; never required for local studio operation
- Uses official Google Authentication (Service Account or OAuth credentials)
- Never exposes tokens, secrets, or raw credentials to frontend or logs
- Provides backup archiving, remote listing, and archive restoration
"""

import os
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from backend.app.config import settings

logger = logging.getLogger("google_drive_service")


class GoogleDriveService:
    def __init__(self):
        self.enabled = getattr(settings, "google_drive_enabled", False)
        self.creds_file = getattr(settings, "google_drive_credentials_file", None)
        self._service = None

    @property
    def is_configured(self) -> bool:
        """Returns True if Google Drive service credentials are valid and ready."""
        if not self.enabled or not self.creds_file:
            return False
        return Path(self.creds_file).is_file()

    def get_status(self) -> Dict[str, Any]:
        """Returns connection and configuration status without exposing sensitive credentials."""
        return {
            "enabled": self.enabled,
            "is_configured": self.is_configured,
            "credentials_present": bool(self.creds_file and Path(self.creds_file).is_file()),
            "status": "ready" if self.is_configured else "not_configured",
            "message": (
                "Google Drive cloud backup is ready."
                if self.is_configured else
                "Google Drive integration is optional. To enable, set GOOGLE_DRIVE_ENABLED=true and provide a credentials file path in backend/.env."
            )
        }

    def upload_project_backup(self, project_id: str, zip_path: Path) -> Dict[str, Any]:
        """
        Uploads a project backup package (.zip) to Google Drive.
        """
        if not self.is_configured:
            return {
                "status": "skipped",
                "message": "Google Drive is not configured. Local backup was saved successfully on disk.",
                "file_path": str(zip_path)
            }

        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload

            creds = service_account.Credentials.from_service_account_file(
                self.creds_file,
                scopes=["https://www.googleapis.com/auth/drive.file"]
            )
            drive = build("drive", "v3", credentials=creds)

            # Check or create "AI Film Studio Backups" folder
            folder_id = self._get_or_create_backup_folder(drive)

            file_metadata = {
                "name": zip_path.name,
                "parents": [folder_id] if folder_id else []
            }
            media = MediaFileUpload(str(zip_path), mimetype="application/zip", resumable=True)
            file = drive.files().create(body=file_metadata, media_body=media, fields="id, name, size, createdTime").execute()

            logger.info(f"Project backup uploaded to Google Drive: {file.get('id')}")
            return {
                "status": "success",
                "drive_file_id": file.get("id"),
                "drive_file_name": file.get("name"),
                "size_bytes": file.get("size"),
                "created_time": file.get("createdTime")
            }

        except Exception as e:
            logger.error(f"Google Drive upload failed: {e}")
            return {
                "status": "failed",
                "error": str(e),
                "message": "Failed to upload to Google Drive. Local backup remains available on disk."
            }

    def list_remote_backups(self) -> List[Dict[str, Any]]:
        """Lists available project backup archives stored in Google Drive."""
        if not self.is_configured:
            return []

        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            creds = service_account.Credentials.from_service_account_file(
                self.creds_file,
                scopes=["https://www.googleapis.com/auth/drive.readonly"]
            )
            drive = build("drive", "v3", credentials=creds)

            response = drive.files().list(
                q="name contains 'project_backup_' and mimeType = 'application/zip' and trashed = false",
                spaces="drive",
                fields="files(id, name, size, createdTime, modifiedTime)",
                pageSize=30
            ).execute()

            return response.get("files", [])
        except Exception as e:
            logger.error(f"Failed to list Google Drive backups: {e}")
            return []

    def _get_or_create_backup_folder(self, drive) -> Optional[str]:
        """Finds or creates the studio backups folder in Google Drive."""
        try:
            q = "name = 'AI Film Studio Backups' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            res = drive.files().list(q=q, spaces="drive", fields="files(id, name)").execute()
            files = res.get("files", [])
            if files:
                return files[0]["id"]

            folder_meta = {
                "name": "AI Film Studio Backups",
                "mimeType": "application/vnd.google-apps.folder"
            }
            folder = drive.files().create(body=folder_meta, fields="id").execute()
            return folder.get("id")
        except Exception as e:
            logger.warning(f"Could not create Google Drive folder: {e}")
            return None


google_drive_service = GoogleDriveService()
