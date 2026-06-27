from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class AgentState:
    """Shared state that all agents read from and write to."""

    issue: dict
    github_token: str = ""
    relevant_files: list[str] = field(default_factory=list)
    issue_type: str = ""
    affected_areas: list[str] = field(default_factory=list)
    complexity: str = ""
    summary: str = ""
    code_context: dict[str, str] = field(default_factory=dict)
    original_files: dict[str, str] = field(default_factory=dict)
    proposed_fix: dict[str, str] = field(default_factory=dict)
    test_code: str = ""
    test_file_path: str = ""
    pr_url: str = ""
    low_confidence: bool = False
    usage: dict = field(default_factory=lambda: {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    })
    status: str = "pending"
    error: str | None = None
    retry_count: int = 0
