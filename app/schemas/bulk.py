from pydantic import BaseModel, Field


class BulkDelete(BaseModel):
    ids: list[str] = Field(
        min_length=1,
        max_length=100,
        description="List of 1 to 100 unique entity IDs to delete",
    )


class BulkDeleteResult(BaseModel):
    deleted: int = Field(ge=0, description="Number of entities successfully deleted")
