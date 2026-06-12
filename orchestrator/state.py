from typing import Annotated, Any, Literal
from pydantic import BaseModel, Field
from langgraph.graph.message import add_messages

AgentName = Literal[
    "clarifier", "ba", "scrum", "developer",
    "qa", "git", "github", "devops", "done"
]

class Artifact(BaseModel):
    agent: AgentName
    artifact_type: str
    content: str
    status: Literal["ok", "fail"] = "ok"
    metadata: dict[str, Any] = Field(default_factory=dict)

class OrchestratorDecision(BaseModel):
    next_agent: AgentName
    reasoning: str
    priority: Literal["high", "normal", "low"] = "normal"
    context_for_agent: str

class PlatformState(BaseModel):
    project_input: str = ""
    messages: Annotated[list, add_messages] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    current_decision: OrchestratorDecision | None = None
    active_agent: AgentName | None = None
    retry_count: int = 0
    max_retries: int = 3
    errors: list[str] = Field(default_factory=list)
    final_output: str | None = None
    awaiting_clarification: bool = False
    clarification_answers: dict = Field(default_factory=dict)
    # ticket-loop fields
    current_ticket_id: str | None = None
    completed_ticket_ids: list[str] = Field(default_factory=list)
    current_sprint: int = 1
    ticket_attempt_count: int = 0
    sprint_committed_files: list[dict] = Field(default_factory=list)

    class Config:
        arbitrary_types_allowed = True
