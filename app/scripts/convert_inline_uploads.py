"""Manually run the inline-upload -> stored-file conversion.

    python -m app.scripts.convert_inline_uploads

Does exactly what the (now startup-gated, see CONVERT_INLINE_UPLOADS_ON_STARTUP
in app/core/config.py) startup step used to do unconditionally: scans every
model in app.services.file_migration_service.TARGETS for legacy `data:` URLs
and replaces them with stored-file links. Safe to run repeatedly — already-
converted values are skipped (see that module's own docstring).

Uses the application's normal SessionLocal via convert_inline_uploads() itself
(app/core/database.py) — no separate DB connection is opened here.
"""

import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)-5.5s [%(name)s] %(message)s")


def main() -> int:
    from app.services.file_migration_service import convert_inline_uploads

    result = convert_inline_uploads()
    print(f"Converted {result.converted} inline upload(s).")
    if result.failed_models:
        print(f"FAILED for model(s): {', '.join(result.failed_models)} — see the log above for details.")
        return 1
    print("No failures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
