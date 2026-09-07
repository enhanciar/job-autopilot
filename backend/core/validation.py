"""Strict models for AI-produced application content."""
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

class Violation(BaseModel):
    claim: StrictStr
    why: StrictStr

class FactCheck(BaseModel):
    ok: StrictBool
    violations: list[Violation] = Field(default_factory=list)

class TailoredResume(BaseModel):
    headline: StrictStr = Field(min_length=1, max_length=120)
    summary: StrictStr = Field(min_length=1, max_length=1600)
    skills_order: list[StrictStr]
    experience_bullets: dict[str, list[StrictStr]]
    cover_note: StrictStr = Field(min_length=1, max_length=3000)
    why_company: StrictStr

    def validate_profile(self, profile):
        if not set(self.skills_order) <= set(profile['skills']): raise ValueError('Unknown skill category')
        if not set(self.experience_bullets) <= {e['company'] for e in profile['experience']}: raise ValueError('Unknown employer')
        return self.model_dump()
