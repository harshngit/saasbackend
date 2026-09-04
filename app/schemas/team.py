from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TeamMemberBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    email: str


class TeamManagerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    email: str


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    manager_id: str = Field(description="Must belong to this organization; becomes a Team member automatically.")
    member_ids: list[str] = Field(
        default_factory=list,
        description="Other users to add as Team members. The manager is added automatically and need not be repeated here.",
    )


class TeamUpdate(BaseModel):
    """Partial update — only the fields you send change.

    `member_ids`, when sent, is a full replace of the non-manager membership
    (the manager always stays a member). Omit it to leave membership as-is.
    """

    name: str | None = Field(default=None, min_length=1, max_length=150)
    manager_id: str | None = Field(
        default=None,
        description="Changing this makes the new manager a member and leaves the previous manager as an "
                    "ordinary member unless member_ids in the same request removes them.",
    )
    member_ids: list[str] | None = Field(default=None, description="Full replacement of Team membership.")


class TeamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    manager_id: str | None = None
    manager: TeamManagerBrief | None = None
    members: list[TeamMemberBrief] = Field(default_factory=list)
    member_count: int = 0
    created_at: datetime
    updated_at: datetime
