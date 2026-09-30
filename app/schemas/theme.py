"""Organization theme & appearance customization.

GET always returns a complete configuration — the documented defaults for an
organization that has never customized its theme, or the stored values for
one that has. `custom_enabled` is what the frontend checks: false means
"use the existing CRM look regardless of what else this response contains".

PATCH is a partial update: send only what you are changing. Anything you
never set is left exactly as it was (or, for a first-ever PATCH, at its
documented default) — never silently reset.
"""

from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator

from app.services.theme_service import BORDER_RADIUS_VALUES, CARD_STYLES, THEME_MODES, THEME_NAMES

# 3- or 6-digit hex only (#fff or #ffffff) — never a CSS color name, rgb()/
# rgba() function, or arbitrary string, since these values are eventually
# written into generated CSS/inline styles.
_HEX_COLOR_PATTERN = r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"
HexColor = Annotated[str, Field(pattern=_HEX_COLOR_PATTERN, max_length=7)]

# Letters, digits and spaces only — enough for every real font name ("DM
# Sans", "Open Sans", "Times New Roman") while rejecting anything that could
# break out of a CSS font-family declaration.
_FONT_NAME_PATTERN = r"^[A-Za-z0-9 ]{1,50}$"
FontName = Annotated[str, Field(pattern=_FONT_NAME_PATTERN)]

ThemeMode = Annotated[str, Field(pattern=f"^({'|'.join(THEME_MODES)})$")]
ThemeName = Annotated[str, Field(pattern=f"^({'|'.join(THEME_NAMES)})$")]
CardStyle = Annotated[str, Field(pattern=f"^({'|'.join(CARD_STYLES)})$")]
BorderRadius = Annotated[str, Field(pattern=f"^({'|'.join(BORDER_RADIUS_VALUES)})$")]


class ThemeBackground(BaseModel):
    url: str | None = Field(default=None, description="/files/{id} — never a raw file id or a signed R2 URL")
    overlay: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("url", mode="after")
    @classmethod
    def _normalize_url(cls, v: str | None) -> str | None:
        """Response-only: absolutize against PUBLIC_BASE_URL — see
        app.core.files.normalize_file_url. organization_themes.background_image_url
        keeps holding the relative value in the database."""
        from app.core.files import normalize_file_url
        return normalize_file_url(v)


class ThemeColors(BaseModel):
    primary: HexColor | None = None
    secondary: HexColor | None = None


class ThemeFonts(BaseModel):
    heading: FontName = "DM Sans"
    body: FontName = "Open Sans"


class ThemeConfig(BaseModel):
    """The full appearance configuration, applied globally by the frontend
    only when the parent response's `custom_enabled` is true."""

    theme_name: ThemeName = "default"
    mode: ThemeMode = "light"
    background: ThemeBackground
    logo_url: str | None = Field(default=None, description="/files/{id} — theme-specific, never Organization.logo_url")
    colors: ThemeColors
    fonts: ThemeFonts
    card_style: CardStyle = "solid"
    border_radius: BorderRadius = "12px"
    custom_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("logo_url", mode="after")
    @classmethod
    def _normalize_logo_url(cls, v: str | None) -> str | None:
        """Response-only: absolutize against PUBLIC_BASE_URL — see
        app.core.files.normalize_file_url. organization_themes.logo_url keeps
        holding the relative value in the database."""
        from app.core.files import normalize_file_url
        return normalize_file_url(v)


class OrganizationThemeOut(BaseModel):
    custom_enabled: bool
    theme: ThemeConfig

    @classmethod
    def from_theme_dict(cls, data: dict[str, Any]) -> "OrganizationThemeOut":
        """Build the nested response shape from app.services.theme_service's
        flat dict (same shape whether it came from a real row or THEME_DEFAULTS)."""
        return cls(
            custom_enabled=bool(data["custom_enabled"]),
            theme=ThemeConfig(
                theme_name=data["theme_name"],
                mode=data["mode"],
                background=ThemeBackground(
                    url=data["background_image_url"], overlay=data["overlay_opacity"],
                ),
                logo_url=data["logo_url"],
                colors=ThemeColors(primary=data["primary_color"], secondary=data["secondary_color"]),
                fonts=ThemeFonts(heading=data["heading_font"], body=data["body_font"]),
                card_style=data["card_style"],
                border_radius=data["border_radius"],
                custom_config=data["custom_config"],
            ),
        )


class OrganizationThemeUpdate(BaseModel):
    """Partial update — every field optional, `exclude_unset` on the server
    side means an omitted field is never touched. Deliberately has no `id`,
    `organization_id`, `created_at`, `updated_at`, `background_image_url` or
    `logo_url` fields: identity/timestamps are never client-writable, and the
    two file URLs are only ever set by their own upload endpoints."""

    custom_enabled: bool | None = None
    theme_name: ThemeName | None = None
    mode: ThemeMode | None = None
    primary_color: HexColor | None = None
    secondary_color: HexColor | None = None
    heading_font: FontName | None = None
    body_font: FontName | None = None
    card_style: CardStyle | None = None
    border_radius: BorderRadius | None = None
    overlay_opacity: float | None = Field(default=None, ge=0.0, le=1.0)
    custom_config: dict[str, Any] | None = None

    @field_validator("custom_config")
    @classmethod
    def _custom_config_is_plain_dict(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        """A dict[str, Any] parsed from a JSON request body is already
        JSON-serializable by construction — this only guards against a caller
        somehow sending a top-level non-object value for the field."""
        if v is not None and not isinstance(v, dict):
            raise ValueError("custom_config must be a JSON object")
        return v
