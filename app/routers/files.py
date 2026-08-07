from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import StoredFile

router = APIRouter(prefix="/files", tags=["files"])


@router.get("/{file_id}")
def get_file(file_id: str, db: Session = Depends(get_db)) -> Response:
    """Serve an uploaded file.

    Deliberately unauthenticated: these URLs go into `<img src>` and PDF viewers,
    which cannot attach a bearer token. Access is by unguessable UUID, the same
    capability-URL model a CDN or an S3 presigned link uses. If a slot ever needs
    stronger protection than that (identity proofs, bank documents), it should
    move to short-lived signed URLs rather than a token on this route.
    """
    stored = db.get(StoredFile, file_id)
    if stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    return Response(
        content=stored.data,
        media_type=stored.content_type,
        headers={
            "Content-Disposition": f'inline; filename="{stored.filename}"',
            # Content is immutable — a new upload gets a new id.
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )
