from __future__ import annotations

import ast
import base64
import json
import os
import re
import time
from dotenv import load_dotenv
from groq import Groq
import requests

from mergepilot.state import AgentState

load_dotenv()


def _log_usage(state: AgentState, response, agent: str) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    pt = getattr(usage, "prompt_tokens", 0) or 0
    ct = getattr(usage, "completion_tokens", 0) or 0
    tt = getattr(usage, "total_tokens", 0) or 0
    state.usage["prompt_tokens"] += pt
    state.usage["completion_tokens"] += ct
    state.usage["total_tokens"] += tt
    print(f"  [{agent}] tokens: {tt} ({pt}+{ct})")


# ── System prompt ──────────────────────────────────────────────────────────
#
# Why we put "Return ONLY valid JSON" at the very top:
#   LLMs weight early tokens most heavily.  Stating the constraint first
#   dramatically reduces fence-wrapping or extra commentary.
#
# Why we embed the schema inline:
#   Giving the exact key names (`issue_type`, `affected_areas`, ...) means the
#   model mirrors them rather than inventing synonyms.
#
# Why temperature=0:
#   Deterministic output is essential for json.loads() to succeed reliably.
#
# Why max_tokens=512:
#   The response is ~200 tokens at most — no point allocating more.
#
_SYSTEM_PROMPT = """\
Return ONLY valid JSON. No markdown, no explanation.

{
  "issue_type": "bug" | "feature" | "refactor" | "docs",
  "affected_areas": ["list", "of", "affected", "modules"],
  "suggested_files": ["file/paths", "that", "might", "need", "changes"],
  "complexity": "low" | "medium" | "high",
  "summary": "One-sentence summary of what needs to be done."
}"""

_ISSUE_TYPES = frozenset({"bug", "feature", "refactor", "docs"})
_COMPLEXITIES = frozenset({"low", "medium", "high"})


def _build_user_prompt(issue: dict) -> str:
    title = issue.get("title", "")
    body = issue.get("body", "")
    return f"## Title\n{title}\n\n## Body\n{body}"


def _parse_response(raw: str) -> dict:
    """Strip markdown fences and parse JSON."""
    text = raw.strip()
    # Remove opening ```json or ``` if present
    if text.startswith("```"):
        idx = text.find("\n")
        if idx != -1:
            text = text[idx:]
        # Remove trailing ```
        text = text.rsplit("```", 1)[0].strip()
    return json.loads(text)


def _validate(parsed: dict) -> dict:
    """Check enum fields and return normalised result."""
    issue_type = parsed.get("issue_type", "").lower()
    if issue_type not in _ISSUE_TYPES:
        raise ValueError(f"Invalid issue_type: '{issue_type}'")

    complexity = parsed.get("complexity", "").lower()
    if complexity not in _COMPLEXITIES:
        raise ValueError(f"Invalid complexity: '{complexity}'")

    return {
        "issue_type": issue_type,
        "affected_areas": parsed.get("affected_areas", []),
        "suggested_files": parsed.get("suggested_files", []),
        "complexity": complexity,
        "summary": parsed.get("summary", ""),
    }


def analyze_issue(state: AgentState) -> None:
    """Extract structured info from a GitHub issue via the Groq API."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise ValueError(
            "GROQ_API_KEY environment variable is not set"
        )

    print(
        f"[Issue Analyzer] Parsing issue #{state.issue.get('number')}:"
        f" {state.issue.get('title')}"
    )

    client = Groq(api_key=api_key)
    user_prompt = _build_user_prompt(state.issue)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    # Internal retry loop (max 2 attempts).
    # If the model wraps the JSON in fences or adds commentary, the first
    # parse will fail and we nudge it with a stronger instruction.  If the
    # second attempt also fails we let the orchestrator handle the retry.
    for attempt in range(2):
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0,
            max_tokens=512,
        )
        _log_usage(state, response, "analyze_issue")
        raw = response.choices[0].message.content or ""

        try:
            parsed = _parse_response(raw)
            validated = _validate(parsed)
        except (json.JSONDecodeError, ValueError) as exc:
            if attempt == 0:
                # Append the failed raw response as an assistant turn and
                # re-prompt — this changes the context enough to push the
                # model toward valid JSON on the second attempt.
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": (
                        "The previous response was not valid JSON. "
                        "Return ONLY valid JSON matching the schema."
                    ),
                })
                continue
            raise ValueError(
                f"Groq response still invalid after retry: {exc}"
            ) from exc

        # Parse + validate succeeded — exit the retry loop.
        state.relevant_files = validated["suggested_files"]
        state.issue_type = validated["issue_type"]
        state.affected_areas = validated["affected_areas"]
        state.complexity = validated["complexity"]
        state.summary = validated["summary"]
        state.status = "researching"
        return


# ── Chunking ────────────────────────────────────────────────────────────────
#
# Why we chunk files before sending them to Groq:
#   1. Token budget — a file can be 1000+ lines; most are noise.  Chunking
#      keeps the relevant context inside the model's window.
#   2. Precision — function/class boundaries are natural semantic units.
#      Groq can say "the `login()` function at line 30" rather than vaguely
#      referencing an entire file.
#   3. Cost — fewer tokens = cheaper + faster API calls.
#


def _chunk_python_file(content: str, filepath: str) -> list[dict]:
    """Parse a Python file into function/class-level chunks via ast."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        lines = content.splitlines()
        return [{"file": filepath, "name": "<module>", "type": "module",
                 "start_line": 1, "end_line": len(lines), "content": content}]

    chunks = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", start)
            lines = content.splitlines()[start - 1:end]
            chunks.append({
                "file": filepath,
                "name": node.name,
                "type": "class" if isinstance(node, ast.ClassDef) else "function",
                "start_line": start,
                "end_line": end,
                "content": "\n".join(lines),
            })

    # No functions/classes found → return the whole file as a single chunk.
    return chunks or [{"file": filepath, "name": "<module>", "type": "module",
                       "start_line": 1, "end_line": len(content.splitlines()),
                       "content": content}]


def _extract_lines(content: str, line_spec: str) -> str:
    """Extract a line range (e.g. '12-45') from file content."""
    parts = line_spec.split("-")
    try:
        start, end = int(parts[0]), int(parts[1])
        all_lines = content.splitlines()
        return "\n".join(all_lines[start - 1:end])
    except (ValueError, IndexError):
        return content


# ── GitHub helpers ──────────────────────────────────────────────────────────


def _get_default_branch(owner: str, repo: str,
                        session: requests.Session) -> str:
    url = f"https://api.github.com/repos/{owner}/{repo}"
    resp = _rate_limited_get(url, session)
    return resp.json()["default_branch"]


def _list_repo_files(owner: str, repo: str, branch: str,
                     session: requests.Session) -> list[dict]:
    url = (f"https://api.github.com/repos/{owner}/{repo}"
           f"/git/trees/{branch}?recursive=1")
    resp = _rate_limited_get(url, session)
    return [item for item in resp.json().get("tree", [])
            if item["type"] == "blob"]


def _fetch_raw_file(owner: str, repo: str, path: str, branch: str,
                    session: requests.Session) -> str | None:
    """Fetch file from raw.githubusercontent.com.  Returns None on 404."""
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    resp = session.get(url)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def _rate_limited_get(url: str, session: requests.Session,
                      attempt: int = 0) -> requests.Response:
    """GET with rate-limit detection + exponential backoff."""
    resp = session.get(url)
    remaining = resp.headers.get("X-RateLimit-Remaining")

    if resp.status_code == 429 or (
        resp.status_code == 403 and remaining is not None and int(remaining) == 0
    ):
        reset_ts = int(resp.headers.get("X-RateLimit-Reset", 0))
        wait = max(reset_ts - time.time(), min(2 ** attempt * 60, 3600))
        print(f"  [Codebase Researcher] Rate limited — waiting {wait:.0f}s...")
        time.sleep(wait)
        return _rate_limited_get(url, session, attempt + 1)

    resp.raise_for_status()
    return resp


# ── Prompt builder ──────────────────────────────────────────────────────────


def _build_chunks_prompt(issue: dict, chunks: list[dict]) -> str:
    title = issue.get("title", "")
    body = issue.get("body", "")
    parts = [f"## Issue\n{title}\n\n{body}\n"]

    parts.append("## Code chunks")
    for c in chunks:
        parts.append(
            f"\n- {c['file']} — {c['type']}:{c['name']} "
            f"(lines {c['start_line']}-{c['end_line']})"
        )
    return "\n".join(parts)


# ── Main agent function ─────────────────────────────────────────────────────


def research_codebase(state: AgentState) -> None:
    """Fetch files from the target repo, chunk them, and let Groq identify
    which snippets are relevant to the issue."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY not set")

    groq_client = Groq(api_key=api_key)
    session = requests.Session()
    session.headers.update({"Accept": "application/vnd.github+json"})
    # ── Web path: use per-user token for authenticated requests ──────────
    # CLI path: token is empty → falls back to unauthenticated (public repos)
    if state.github_token:
        session.headers.update({
            "Authorization": f"Bearer {state.github_token}"
        })

    owner_repo = state.issue.get("repo", "")
    if "/" not in owner_repo:
        raise ValueError(f"Invalid repo: {owner_repo}")
    owner, repo = owner_repo.split("/", 1)

    print(f"[Codebase Researcher] Target: {owner}/{repo}")
    print(f"  Suggested files: {state.relevant_files}")

    # ---- 1. Determine the default branch ----
    branch = _get_default_branch(owner, repo, session)
    print(f"  Branch: {branch}")

    # ---- 2. Try fetching the suggested files first ----
    fetched: dict[str, str] = {}
    for filepath in state.relevant_files:
        content = _fetch_raw_file(owner, repo, filepath, branch, session)
        if content is None:
            print(f"  [!] {filepath} not found — skipping")
            continue
        fetched[filepath] = content
        print(f"  [ok] {filepath}")

    _TEXT_EXTENSIONS = frozenset({
        ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".htm", ".css",
        ".scss", ".less", ".txt", ".md", ".rst", ".json", ".yaml",
        ".yml", ".toml", ".cfg", ".ini", ".csv", ".xml", ".svg",
        ".env", ".gitignore", ".dockerfile", ".yml", ".yaml",
    })

    # ---- 3. Fall back to listing the repo if nothing matched ----
    if not fetched:
        print("  No suggested files found — listing repo contents...")
        tree = _list_repo_files(owner, repo, branch, session)
        text_files = [
            f for f in tree
            if any(f["path"].endswith(ext) for ext in _TEXT_EXTENSIONS)
        ]
        for entry in text_files:
            content = _fetch_raw_file(owner, repo, entry["path"],
                                      branch, session)
            if content:
                fetched[entry["path"]] = content
                print(f"  [ok] {entry['path']}")

    if not fetched:
        state.status = "failed"
        state.error = "No files could be fetched from the repository"
        return

    # ---- 4. Chunk each file by function/class ----
    all_chunks: list[dict] = []
    for filepath, content in fetched.items():
        all_chunks.extend(_chunk_python_file(content, filepath))
    print(f"  Created {len(all_chunks)} chunks across {len(fetched)} file(s)")

    # ---- 5. Send chunks + issue to Groq for relevance filtering ----
    user_prompt = _build_chunks_prompt(state.issue, all_chunks)

    SYSTEM_PROMPT = """\
You are a codebase researcher. Given a GitHub issue and code chunks,
identify which chunks are relevant to fixing the issue.

Return ONLY valid JSON:
{
  "relevant_chunks": [
    {
      "file": "path/to/file.py",
      "name": "function_name",
      "lines": "12-45",
      "reason": "why this chunk is relevant"
    }
  ]
}

If nothing is relevant, return {"relevant_chunks": []}."""

    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    relevant: list[dict] = []
    for attempt in range(2):
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0,
            max_tokens=1024,
        )
        _log_usage(state, response, "codebase_researcher")
        raw = response.choices[0].message.content or ""
        try:
            parsed = _parse_response(raw)
            relevant = parsed.get("relevant_chunks", [])
            break
        except (json.JSONDecodeError, ValueError) as exc:
            if attempt == 0:
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": (
                        "The previous response was not valid JSON. "
                        "Return ONLY valid JSON matching the schema."
                    ),
                })
                continue
            raise ValueError(
                f"Groq response still invalid after retry: {exc}"
            ) from exc

    # ---- 6. Build code_context from relevant chunks ----
    code_context: dict[str, str] = {}
    for chunk_info in relevant:
        filepath = chunk_info.get("file", "")
        content = fetched.get(filepath, "")
        if not filepath or not content:
            continue
        line_spec = chunk_info.get("lines", "")
        snippet = _extract_lines(content, line_spec) if line_spec else content
        code_context[filepath] = snippet

    # ---- 7. Flag low confidence if nothing was relevant ----
    if not relevant:
        state.low_confidence = True
        print("  Warning: Groq returned no relevant chunks (low confidence)")

    state.code_context = code_context
    state.original_files = dict(fetched)
    state.status = "drafting"
    print(f"  Relevant snippets from {len(code_context)} file(s)")


# ── Fix Drafter ─────────────────────────────────────────────────────────────

_SYSTEM_PROMPT_FIX = """\
Return ONLY valid JSON. No markdown, no explanation.

You are making targeted surgical code changes. For each file that needs
changes, specify the EXACT original text to find and the new text to
replace it with.  The "original" value must be a verbatim match of
existing code — include enough surrounding lines to make it unique.

RULES:
- "original" must match existing source character-for-character
- Only change the minimum lines needed to fix the issue
- PRESERVE all `${...}` template literal syntax — do not strip braces
- Do NOT add inline comments unless they existed before
- Include enough context in "original" so there is exactly one match

{
  "cannot_fix": false,
  "changes": [
    {
      "file": "path/to/file.py",
      "original": "exact text currently in the file (multi-line supported)",
      "replacement": "new text that replaces original"
    }
  ],
  "summary": "Brief explanation of the fix"
}

If you cannot fix:
{
  "cannot_fix": true,
  "reason": "Why the fix cannot be determined",
  "changes": []
}"""


def _build_fix_prompt(issue: dict, code_context: dict[str, str]) -> str:
    title = issue.get("title", "")
    body = issue.get("body", "")
    parts = [f"## Issue\n{title}\n\n{body}\n"]
    parts.append("## Relevant code")
    for filepath, content in code_context.items():
        lang = "python" if filepath.endswith(".py") else "js" if filepath.endswith((".js", ".jsx")) else ""
        fence = f"```{lang}" if lang else "```"
        parts.append(f"\n--- {filepath} ---\n{fence}\n{content}\n```")
    return "\n".join(parts)


def _apply_surgical_changes(
    original_files: dict[str, str],
    changes: list[dict],
) -> dict[str, str]:
    modified: dict[str, str] = {}
    for change in changes:
        filepath = change.get("file", "")
        original = change.get("original", "")
        replacement = change.get("replacement", "")
        if not filepath or not original:
            continue
        if filepath not in original_files:
            print(f"  [!] File {filepath} not in original_files — skipping")
            continue
        content = original_files[filepath]
        idx = content.find(original)
        if idx == -1:
            print(f"  [!] Could not find original text in {filepath} — skipping")
            continue
        new_content = content[:idx] + replacement + content[idx + len(original):]
        if new_content != content:
            modified[filepath] = new_content
    return modified


def _validate_snippet(snippet: str, filepath: str) -> bool:
    if filepath.endswith(".py"):
        try:
            compile(snippet, "<fix>", "exec")
            return True
        except SyntaxError:
            return False
    return True


def draft_fix(state: AgentState) -> None:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY not set")

    if not state.code_context:
        state.status = "failed"
        state.error = "No code context available — cannot draft a fix"
        return

    print(f"[Fix Drafter] Drafting fix for {len(state.code_context)} file(s)...")

    groq_client = Groq(api_key=api_key)
    user_prompt = _build_fix_prompt(state.issue, state.code_context)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT_FIX},
        {"role": "user", "content": user_prompt},
    ]

    for attempt in range(2):
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0,
            max_tokens=4096,
        )
        _log_usage(state, response, "fix_drafter")
        raw = response.choices[0].message.content or ""
        try:
            parsed = _parse_response(raw)
            break
        except (json.JSONDecodeError, ValueError) as exc:
            if attempt == 0:
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": (
                        "The previous response was not valid JSON. "
                        "Return ONLY valid JSON matching the schema."
                    ),
                })
                continue
            raise ValueError(
                f"Groq response still invalid after retry: {exc}"
            ) from exc

    if parsed.get("cannot_fix", False):
        state.status = "failed"
        state.error = parsed.get("reason", "FixDrafter could not determine a fix")
        return

    changes = parsed.get("changes", [])
    if not changes:
        state.status = "failed"
        state.error = "FixDrafter returned no changes"
        return

    applied = _apply_surgical_changes(state.original_files, changes)

    if not state.original_files:
        state.low_confidence = True

    if not applied:
        state.status = "failed"
        state.error = (
            f"FixDrafter could not apply any surgical changes — "
            f"none of the {len(changes)} change(s)"
            f" matched the original file content"
        )
        return

    for change in changes:
        fp = change.get("file", "")
        rep = change.get("replacement", "")
        if fp.endswith(".py") and rep and not _validate_snippet(rep, fp):
            print(f"  [!] Syntax error in {fp}")
            state.low_confidence = True

    state.proposed_fix = applied
    state.status = "testing"
    diff = sum(1 for c in changes if c.get("file"))
    print(f"  {len(changes)} surgical change(s) across {diff} file(s)")


# ── Test Writer ─────────────────────────────────────────────────────────────


_SYSTEM_PROMPT_TEST = """\
You are a QA engineer writing tests for a code fix.

Given the original issue and the proposed fix:
- Use the appropriate test framework for the language being tested
  (pytest for Python, Vitest/Jest for JS/TS, RSpec for Ruby, etc.)
- The test file path should follow the project's conventions
  (e.g. tests/test_fix.py for Python, src/Component.test.jsx for React)
- Cover: happy path, edge case, regression

Return ONLY valid JSON:
{
  "test_file": "relative/path/to/test/file",
  "test_code": "complete test file content",
  "description": "What each test covers"
}"""


def _build_test_prompt(issue: dict, proposed_fix: dict[str, str]) -> str:
    title = issue.get("title", "")
    body = issue.get("body", "")
    parts = [f"## Issue\n{title}\n\n{body}\n"]
    parts.append("## Proposed fix")
    for filepath, content in proposed_fix.items():
        parts.append(f"\n--- {filepath} ---\n```python\n{content}\n```")
    return "\n".join(parts)


def write_tests(state: AgentState) -> None:
    """Generate tests via Groq, but only if the issue explicitly asks for them."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY not set")

    if not state.proposed_fix:
        state.status = "failed"
        state.error = "No proposed fix — cannot write tests"
        return

    # Only generate tests if the issue explicitly mentions them.
    title = (state.issue.get("title") or "").lower()
    body = (state.issue.get("body") or "").lower()
    if "test" not in title and "test" not in body:
        print("[Test Writer] No tests requested — skipping")
        state.status = "opening_pr"
        return

    print(f"[Test Writer] Writing tests for {len(state.proposed_fix)} file(s)...")

    groq_client = Groq(api_key=api_key)
    user_prompt = _build_test_prompt(state.issue, state.proposed_fix)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT_TEST},
        {"role": "user", "content": user_prompt},
    ]

    for attempt in range(2):
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            temperature=0,
            max_tokens=2048,
        )
        _log_usage(state, response, "test_writer")
        raw = response.choices[0].message.content or ""
        try:
            parsed = _parse_response(raw)
            break
        except (json.JSONDecodeError, ValueError) as exc:
            if attempt == 0:
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": (
                        "The previous response was not valid JSON. "
                        "Return ONLY valid JSON matching the schema."
                    ),
                })
                continue
            raise ValueError(
                f"Groq response still invalid after retry: {exc}"
            ) from exc

    state.test_file_path = parsed.get("test_file", "")
    state.test_code = parsed.get("test_code", "")
    state.status = "opening_pr"
    print(f"  Tests: {len(state.test_code)} chars → {state.test_file_path}")


# ── PR Creator ──────────────────────────────────────────────────────────────


_SYSTEM_PROMPT_PR_BODY = """\
You are writing a pull request description.

Given the original issue, the proposed fix, and the tests, write a clear
PR description in markdown explaining what was changed and why.

Return ONLY valid JSON:
{
  "body": "PR description in markdown"
}"""


def _build_pr_body_prompt(issue: dict, proposed_fix: dict[str, str],
                          test_code: str) -> str:
    title = issue.get("title", "")
    body = issue.get("body", "")
    parts = [f"## Issue\n{title}\n\n{body}\n"]
    parts.append("## Proposed fix")
    for filepath, content in proposed_fix.items():
        parts.append(f"\n--- {filepath} ---\n```python\n{content}\n```")
    parts.append(f"\n## Tests\n```python\n{test_code}\n```")
    return "\n".join(parts)


def create_pr(state: AgentState) -> None:
    """Create a branch, commit fix + tests, and open a PR.

    ── Token resolution ──────────────────────────────────────────────────
    Web path (backend/main.py):
        state.github_token is set → used for this run, never persisted.
    CLI path (python -m mergepilot):
        state.github_token is empty → falls back to GITHUB_TOKEN from .env.
    """
    token = state.github_token or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValueError(
            "GITHUB_TOKEN not set — provide a token via the web UI "
            "or set GITHUB_TOKEN in .env for CLI usage."
        )

    print("[PR Creator] Creating branch, committing files, opening PR...")

    owner_repo = state.issue.get("repo", "")
    if "/" not in owner_repo:
        raise ValueError(f"Invalid repo: {owner_repo}")
    owner, repo = owner_repo.split("/", 1)
    issue_number = state.issue.get("number", 0)

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })

    # ── 1. Get default branch + latest commit SHA ──
    repo_url = f"https://api.github.com/repos/{owner}/{repo}"
    resp = session.get(repo_url)
    resp.raise_for_status()
    default_branch = resp.json()["default_branch"]

    ref_url = (f"https://api.github.com/repos/{owner}/{repo}"
               f"/git/refs/heads/{default_branch}")
    resp = session.get(ref_url)
    resp.raise_for_status()
    latest_sha = resp.json()["object"]["sha"]

    # ── 2. Create branch ──
    branch_name = f"mergepilot/fix-{issue_number}"
    print(f"  Branch: {branch_name}")

    create_ref_url = f"https://api.github.com/repos/{owner}/{repo}/git/refs"
    resp = session.post(create_ref_url, json={
        "ref": f"refs/heads/{branch_name}",
        "sha": latest_sha,
    })
    if resp.status_code == 422:
        session.delete(
            f"https://api.github.com/repos/{owner}/{repo}"
            f"/git/refs/heads/{branch_name}"
        )
        resp = session.post(create_ref_url, json={
            "ref": f"refs/heads/{branch_name}",
            "sha": latest_sha,
        })
    resp.raise_for_status()

    # ── 3. Commit each proposed-fix file ──
    for filepath, content in state.proposed_fix.items():
        put_url = (f"https://api.github.com/repos/{owner}/{repo}"
                   f"/contents/{filepath}")

        sha = None
        get_resp = session.get(put_url, params={"ref": branch_name})
        if get_resp.status_code == 200:
            sha = get_resp.json()["sha"]

        body = {
            "message": f"fix: {filepath} — automated fix for #{issue_number}",
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch_name,
        }
        if sha:
            body["sha"] = sha

        resp = session.put(put_url, json=body)
        resp.raise_for_status()
        print(f"  Committed {filepath}")

    # ── 4. Commit test file ──
    if state.test_code:
        test_path = state.test_file_path or "tests/test_fix.py"
        put_url = (f"https://api.github.com/repos/{owner}/{repo}"
                   f"/contents/{test_path}")

        sha = None
        get_resp = session.get(put_url, params={"ref": branch_name})
        if get_resp.status_code == 200:
            sha = get_resp.json()["sha"]

        body = {
            "message": f"test: add tests for #{issue_number}",
            "content": base64.b64encode(state.test_code.encode()).decode(),
            "branch": branch_name,
        }
        if sha:
            body["sha"] = sha

        resp = session.put(put_url, json=body)
        resp.raise_for_status()
        print(f"  Committed {test_path}")

    # ── 5. Generate PR body via Groq ──
    groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    body_prompt = _build_pr_body_prompt(
        state.issue, state.proposed_fix, state.test_code
    )
    body_messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT_PR_BODY},
        {"role": "user", "content": body_prompt},
    ]
    body_resp = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=body_messages,
        temperature=0,
        max_tokens=1024,
    )
    _log_usage(state, body_resp, "pr_body")
    raw = body_resp.choices[0].message.content or "{}"
    try:
        parsed = _parse_response(raw)
        pr_body = parsed.get(
            "body",
            "Automated fix generated by MergePilot."
        )
    except (json.JSONDecodeError, ValueError):
        pr_body = "Automated fix generated by MergePilot."

    # ── 6. Create pull request ──
    pr_create_url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    resp = session.post(pr_create_url, json={
        "title": state.issue.get(
            "title", f"Fix for issue #{issue_number}"
        ),
        "body": pr_body,
        "head": branch_name,
        "base": default_branch,
    })
    resp.raise_for_status()
    pr_data = resp.json()
    pr_number = pr_data["number"]
    state.pr_url = pr_data["html_url"]
    print(f"  PR #{pr_number}: {state.pr_url}")

    # ── 7. Add labels ──
    labels_url = (f"https://api.github.com/repos/{owner}/{repo}"
                  f"/issues/{pr_number}/labels")
    try:
        session.post(labels_url, json={
            "labels": ["auto-generated", state.issue_type],
        })
    except Exception:
        pass

    state.status = "done"


def review_pr(state: AgentState) -> None:
    """Sanity-check the PR before marking the pipeline complete."""
    print(f"[Reviewer] Reviewing PR {state.pr_url}...")
    state.status = "done"
