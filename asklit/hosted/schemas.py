"""Strict, data-only bot definitions and request contracts."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Profile(Strict):
    key: str = Field(default="default", pattern=r"^[a-z0-9-]{1,100}$")
    label: str = Field(default="Default", min_length=1, max_length=200)
    knowledgebase: str = Field(default="default", pattern=r"^[a-z0-9-]{1,100}$")
    prompt: str = Field(
        default="You are a helpful assistant.", min_length=1, max_length=30000
    )
    conversation_starters: list[str] = Field(default_factory=list, max_length=10)
    connected_files: list[str] = Field(default_factory=list, max_length=100)


class Scenario(Strict):
    input: str = Field(min_length=1, max_length=8000)
    expected: str = Field(default="", max_length=8000)
    description: str = Field(default="", max_length=2000)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=50)


class EvaluationOptions(Strict):
    mode: Literal["single", "matrix"] = "single"
    prompts: list[str] = Field(default_factory=list, max_length=20)
    sources: list[str] = Field(default_factory=list, max_length=20)
    models: list[str] = Field(default_factory=list, max_length=20)
    judge_model: str = Field(default="", max_length=100)
    top_k: int = Field(default=5, ge=1, le=10)


class ScenarioCSV(Strict):
    csv: str = Field(max_length=1_000_000)


class GenerateScenarios(Strict):
    profile: str = Field(max_length=100)
    model: str = Field(max_length=100)
    count: int = Field(default=5, ge=1, le=12)


class BotConfig(Strict):
    show_citations: bool = True
    # Instructor-controlled voice. Participants cannot change these.
    voice_enabled: bool = True  # master switch; free browser voice needs no opt-in, and nobody is required to use it
    voice_listen: Literal["browser", "service"] = "browser"
    voice_speak: Literal["browser", "service"] = "browser"
    voice_persona: Literal["neutral", "feminine", "masculine"] = "neutral"
    student_instructions: str = Field(default="", max_length=30000)
    uploads_enabled: bool = False
    upload_prompt: str = Field(default="", max_length=4000)
    title: str = Field(default="My AskLit", min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    welcome: str = Field(default="How can I help you?", max_length=2000)
    model_preset: str = Field(default="default", max_length=100)
    profiles: list[Profile] = Field(
        default_factory=lambda: [Profile()], min_length=1, max_length=20
    )
    scenarios: list[Scenario] = Field(default_factory=list, max_length=120)
    evaluation: EvaluationOptions = Field(default_factory=EvaluationOptions)
    rubrics: list[str] = Field(default_factory=list, max_length=10)
    footer: str = Field(default="Made with AskLit", max_length=1000)
    top_k: int = Field(default=3, ge=1, le=10)


class SaveBot(Strict):
    instructor_notes: str | None = Field(default=None, max_length=30000)
    instructor_shared: bool | None = None
    instructor_password: str | None = Field(default=None, min_length=12, max_length=200)
    version: int = Field(ge=1)
    config: BotConfig


class NewBot(Strict):
    org_id: str
    title: str = Field(min_length=1, max_length=200)


class Name(Strict):
    name: str = Field(min_length=1, max_length=200)


class Invite(Strict):
    role: Literal["member", "admin"] = "member"


class Role(Strict):
    role: Literal["member", "admin", "owner"]


class Version(Strict):
    version: int = Field(ge=1)


class StartChat(Strict):
    profile: str = "default"
    access_token: str = Field(default="", max_length=200)


class AccessRequest(Strict):
    password: str = Field(default="", max_length=200)


class OrganizationSharing(Strict):
    slug: str = Field(
        min_length=2, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    )
    gallery_enabled: bool = False
    paid_voice_enabled: bool = False
    paid_voice_daily_calls: int = Field(default=100, ge=1, le=5000)
    allowed_domains: list[str] = Field(default_factory=list, max_length=50)
    microsoft_tenants: list[str] = Field(default_factory=list, max_length=50)


class BotSharing(Strict):
    slug: str = Field(
        min_length=2, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
    )
    access_mode: Literal["public", "unlisted", "password", "organization"]


class NewPassword(Strict):
    label: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=4, max_length=200)


class PasswordState(Strict):
    enabled: bool


class Message(Strict):
    message: str = Field(min_length=1, max_length=8000)
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")


class PreviewAttachment(Strict):
    filename: str = Field(min_length=1, max_length=180)
    text: str = Field(min_length=1, max_length=30000)


class Preview(Strict):
    attachments: list[PreviewAttachment] = Field(default_factory=list, max_length=3)
    message: str = Field(min_length=1, max_length=8000)
    profile: str = "default"
    history: list[dict] = Field(default_factory=list, max_length=24)


class ImportWorkspace(Strict):
    version: int = Field(ge=1)
    yaml: str = Field(max_length=1_000_000)


class Transfer(Strict):
    owner_id: str


class AcceptInvite(Strict):
    token: str = Field(min_length=20, max_length=200)


class RenameKnowledgebase(Version):
    old_name: str = Field(pattern=r"^[a-z0-9-]{1,100}$")
    new_name: str = Field(pattern=r"^[a-z0-9-]{1,100}$")


class SpeechRequest(Strict):
    request_id: str = Field(default="", max_length=100)
    text: str = Field(default="", max_length=4000)
