from typing import Generic, TypeVar
from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    model_config = ConfigDict(arbitrary_types_allowed=True, from_attributes=True)

    items: list[T] = Field(default_factory=list, description="Records on the current page")
    total: int = Field(ge=0, description="Total matching records after filter and search")
    page: int = Field(ge=1, default=1, description="Current page number (1-indexed)")
    page_size: int = Field(ge=1, default=10, description="Effective page size")
    total_pages: int = Field(ge=0, default=0, description="Total number of available pages")

    def __iter__(self):
        return iter((self.items, self.total, self.page, self.page_size, self.total_pages))
