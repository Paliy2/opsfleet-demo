from pydantic import BaseModel, ConfigDict, Field


class GoldenTrio(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(pattern=r"^[a-z0-9-]{3,80}$")
    category: str
    question: str
    approach: str
    sql: str
    report: str

    @property
    def search_text(self) -> str:
        return f"{self.question}\n{self.approach}"


class GoldenMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    trio: GoldenTrio
    similarity: float
    score: float
