"""HTTP response schemas."""

from pydantic import BaseModel, ConfigDict, Field


class ExtractResponse(BaseModel):
    """Response schema for stateless PDF text extraction."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "content": "Contenido del PDF extraído...",
                "page_count": 3,
            }
        }
    )

    content: str = Field(..., description="Texto extraído del PDF")
    page_count: int = Field(..., ge=0, description="Cantidad de páginas del PDF")
