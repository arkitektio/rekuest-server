from typing import Optional

from pydantic import BaseModel


class RequestMediaUploadInput(BaseModel):
    """Request temporary S3 upload credentials for a media object."""

    original_file_name: str
    file_size: Optional[int] = None
    content_type: Optional[str] = None


class FinishMediaUploadInput(BaseModel):
    """Mark a MediaStore as populated after a successful upload."""

    store_id: str
    valid: bool = True


class RequestMediaAccessInput(BaseModel):
    """Request temporary S3 access credentials for a media object."""

    store_id: str


class RequestGeneralMediaAccessInput(BaseModel):
    """Request temporary S3 access credentials for media objects in the organization."""

    expires_in: Optional[int] = None


class AccessGrant(BaseModel):
    """Temporary S3 credentials scoped to a datalayer action."""

    status: str = "granted"
    access_key: str
    secret_key: str
    session_token: str
    region: str
    bucket: str
    key: str
    path: str
    expires_in: int
    store: str | None = None


class GeneralAccessGrant(BaseModel):
    """Temporary S3 credentials for an existing media object, without a store reference."""

    status: str = "granted"
    access_key: str
    secret_key: str
    session_token: str
    region: str
    bucket: str
    expires_in: int


class GeneralMediaAccessGrant(GeneralAccessGrant):
    """Temporary S3 credentials for an existing media object, without a store reference."""


class MediaAccessGrant(AccessGrant):
    """Temporary S3 credentials for an existing media object."""


class BaseUploadGrant(AccessGrant):
    """Temporary S3 credentials for uploads bound to a specific store."""

    region: str
    max_bytes: int
    original_file_name: str | None = None
    upload_file_name: str
    upload_content_type: str | None = None
    upload_form_field: str = "file"


class MediaUploadGrant(BaseUploadGrant):
    """A presigned PUT grant for a media upload."""


