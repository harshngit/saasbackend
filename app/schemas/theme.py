"""Organization theme & appearance customization.

GET always returns a complete configuration — the documented defaults for an
organization that has never customized its theme, or the stored values for
one that has. `custom_enabled` is what the frontend checks: false means
"use the existing CRM look regardless of what else this response contains".

PATCH is a partial update: send only what you are changing. Unknown fields
are rejected with HTTP 422 (extra="forbid").
"""

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.organization_theme import OrganizationTheme
from app.services.theme_service import THEME_MODES

# 6-digit hex only (#rrggbb) — normalized to lowercase. 3-digit shorthand (#fff) is rejected.
_HEX_COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"
HexColor = Annotated[str, Field(pattern=_HEX_COLOR_PATTERN)]

ThemeMode = Annotated[str, Field(pattern=f"^({'|'.join(THEME_MODES)})$")]


class ThemeBackground(BaseModel):
    url: str | None = Field(
        default=None,
        description="/files/{id} — host-independent relative path in DB, absolutized on response if PUBLIC_BASE_URL is set",
    )
    overlay_opacity: float = Field(default=0.45, ge=0.0, le=0.9)

    @field_validator("url", mode="after")
    @classmethod
    def _normalize_url(cls, v: str | None) -> str | None:
        """Response-only: absolutize against PUBLIC_BASE_URL — see
        app.core.files.normalize_file_url. organization_themes.background_image_url
        keeps holding the relative value in the database."""
        from app.core.files import normalize_file_url

        return normalize_file_url(v)


class OrganizationThemeOut(BaseModel):
    custom_enabled: bool
    mode: ThemeMode
    primary_color: str | None = None
    background: ThemeBackground
    updated_at: datetime | None = None

    @field_validator("primary_color", mode="after")
    @classmethod
    def _normalize_primary_color(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.lower()

    @classmethod
    def from_theme(cls, theme: OrganizationTheme | None) -> "OrganizationThemeOut":
        """Build the canonical response shape from an OrganizationTheme row or None."""
        if theme is None:
            return cls(
                custom_enabled=False,
                mode="light",
                primary_color=None,
                background=ThemeBackground(url=None, overlay_opacity=0.45),
                updated_at=None,
            )
        return cls(
            custom_enabled=bool(theme.custom_enabled),
            mode=theme.mode,
            primary_color=theme.primary_color.lower() if theme.primary_color else None,
            background=ThemeBackground(
                url=theme.background_image_url,
                overlay_opacity=theme.overlay_opacity if theme.overlay_opacity is not None else 0.45,
            ),
            updated_at=theme.updated_at,
        )

    @classmethod
    def from_theme_dict(
        cls, data: dict[str, Any], updated_at: datetime | None = None
    ) -> "OrganizationThemeOut":
        """Build the canonical response shape from a theme dictionary."""
        return cls(
            custom_enabled=bool(data["custom_enabled"]),
            mode=data["mode"],
            primary_color=data["primary_color"].lower() if data.get("primary_color") else None,
            background=ThemeBackground(
                url=data["background_image_url"],
                overlay_opacity=data["overlay_opacity"]
                if data.get("overlay_opacity") is not None
                else 0.45,
            ),
            updated_at=updated_at,
        )


class OrganizationThemeUpdate(BaseModel):
    """Partial update — every field optional.

    Strictly forbids unknown or removed fields (e.g. heading_font, theme_name, logo_url),
    returning HTTP 422 if supplied.
    """

    model_config = ConfigDict(extra="forbid")

    custom_enabled: bool | None = None
    mode: ThemeMode | None = None
    primary_color: HexColor | None = None
    overlay_opacity: float | None = Field(default=None, ge=0.0, le=0.9)

    @field_validator("primary_color", mode="after")
    @classmethod
    def _normalize_color(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.lower()
