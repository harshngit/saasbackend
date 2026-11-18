"""Shared test setup.

POST /products requires a Primary Product Image (cover_image). Most older tests create
products only as fixtures for orders, deliveries, quotations, ... and send none, so the
rule is switched off for them. A test module that checks product creation itself opts in
with REQUIRE_COVER_IMAGE = True.
"""

import pytest


# Module scope, so it is already in place for module-scoped fixtures that create products.
@pytest.fixture(autouse=True, scope="module")
def _cover_image_rule(request):
    if getattr(request.module, "REQUIRE_COVER_IMAGE", False):
        yield
        return
    from app.routers import products

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(products, "_validate_new_cover_image", lambda *a, **k: None)
        yield
