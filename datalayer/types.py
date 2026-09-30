import strawberry
from datalayer import models
from kante.types import Info
import kante
from typing import cast
from datalayer import base_models
from datalayer.datalayer import get_current_datalayer


@kante.pydantic_type(base_models.MediaAccessGrant, description="Temporary S3 credentials for reading a media object.")
class MediaAccessGrant:
    """Temporary S3 credentials for a media object."""

    status: str
    access_key: str
    secret_key: str
    session_token: str
    region: str
    bucket: str
    key: str
    path: str
    expires_in: int
    store: str | None


@kante.pydantic_type(base_models.GeneralMediaAccessGrant, description="Temporary S3 credentials for reading a media object.")
class GeneralMediaAccessGrant:
    """Temporary S3 credentials for a media object."""

    status: str
    access_key: str
    secret_key: str
    session_token: str
    region: str
    bucket: str
    path: str
    expires_in: int
    store: str | None


@kante.pydantic_type(base_models.MediaUploadGrant, description="A presigned PUT grant for uploading a media object.")
class MediaUploadGrant:
    """A presigned PUT grant for a media upload."""

    region: str
    status: str
    access_key: str
    secret_key: str
    session_token: str
    bucket: str
    key: str
    path: str
    expires_in: int
    max_bytes: int
    original_file_name: str | None
    upload_file_name: str
    upload_content_type: str | None
    upload_form_field: str
    store: str


@kante.django_type(models.MediaStore)
class MediaStore:
    """A media object stored behind the S3 datalayer."""

    id: strawberry.auto
    path: str
    bucket: str
    key: str
    original_file_name: str | None
    content_type: str | None

    @kante.django_field(description="Get temporary S3 read credentials for the media object.")
    def access_grant(self, info: Info, host: str | None = None) -> MediaAccessGrant:
        """Return a signed read grant for the media object."""
        del info, host
        datalayer = get_current_datalayer()
        grant = cast(models.MediaStore, self).get_access_grant(datalayer=datalayer)
        return MediaAccessGrant(**grant.model_dump())

    @kante.django_field(description="Compatibility field returning the canonical S3 object path.")
    def presigned_url(self, info: Info, host: str | None = None) -> str:
        """Compatibility field returning the canonical S3 object path."""
        datalayer = get_current_datalayer()
        return cast(models.MediaStore, self).get_presigned_url(datalayer=datalayer, host=host)


