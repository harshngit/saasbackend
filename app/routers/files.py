from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.core.files import MAX_UPLOAD_BYTES, save_upload
from app.models import StoredFile, User

router = APIRouter(prefix="/files", tags=["files"])


def _stored_file(db: Session, file_id: str) -> StoredFile:
    stored = db.get(StoredFile, file_id)
    if stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return stored


class FileUploadOut(BaseModel):
    """What every upload hands back. `url` is the only field most callers need —
    put it straight into whichever record field the file belongs to. `file_id` is
    what DELETE /files/{file_id} and the per-document delete routes take."""

    url: str
    file_id: str
    name: str
    filename: str = Field(deprecated=True, description="Same as `name`")
    content_type: str
    size: int


@router.post("/upload", response_model=FileUploadOut, status_code=status.HTTP_201_CREATED)
def upload_file(
    request: Request,
    file: UploadFile = File(..., description="Any image, PDF, document or video up to 10 MB"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileUploadOut:
    """Upload a file and get a URL back — one endpoint for everything.

    Deliberately takes no record id, so the app can upload before the record it
    belongs to exists: upload here, then send the returned `url` in the create or
    update body of whichever field wants it (`profile_photo`, `cover_image`,
    `logo_url`, `receipt_url`, …).

    The per-field endpoints (POST /users/{id}/files/{field} and friends) still
    exist and additionally set the column for you; this one is the generic path.
    """
    url, size = save_upload(
        db,
        user.organization_id,
        file,
        request,
        allow_any=True,  # generic slot — the per-field routes do the type checking
        max_bytes=MAX_UPLOAD_BYTES,
    )
    stored_id = url.rsplit("/", 1)[-1]
    db.commit()
    name = file.filename or "upload"
    return FileUploadOut(
        url=url,
        file_id=stored_id,
        name=name,
        filename=name,
        content_type=file.content_type or "application/octet-stream",
        size=size,
    )


@router.get("/{file_id}")
def get_file(file_id: str, db: Session = Depends(get_db)) -> Response:
    """Serve an uploaded file.

    Deliberately unauthenticated: these URLs go into `<img src>` and PDF viewers,
    which cannot attach a bearer token. Access is by unguessable UUID, the same
    capability-URL model a CDN or an S3 presigned link uses. If a slot ever needs
    stronger protection than that (identity proofs, bank documents), it should
    move to short-lived signed URLs rather than a token on this route.
    """
    stored = _stored_file(db, file_id)
    return Response(
        content=stored.data,
        media_type=stored.content_type,
        headers={
            "Content-Disposition": f'inline; filename="{stored.filename}"',
            # Content is immutable — a new upload gets a new id.
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(
    file_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    """Delete an uploaded file — for the one the form uploaded and then discarded,
    before it was ever attached to a record.

    This removes the file itself, not any reference to it: a record still holding
    the URL will be left pointing at nothing. To detach a file from an employee,
    use DELETE /users/{id}/files/{field} or
    DELETE /users/{id}/documents/{collection}/{document_id} instead.
    """
    stored = _stored_file(db, file_id)
    # Files are uploaded within a firm; only that firm can delete them.
    if stored.organization_id is not None and stored.organization_id != user.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    db.delete(stored)
    db.commit()
