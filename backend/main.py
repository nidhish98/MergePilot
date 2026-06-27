from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from mergepilot.state import AgentState
from mergepilot.orchestrator import Orchestrator
from mergepilot.__main__ import fetch_issue

app = FastAPI(title="MergePilot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

executor = ThreadPoolExecutor(max_workers=2)
run_queues: dict[str, asyncio.Queue] = {}


class RunRequest(BaseModel):
    issue_url: str
    github_token: str = ""


def validate_github_token(token: str) -> str:
    """Verify token by calling GitHub /user and return the username.

    Token is user-supplied, validated once, used in-memory,
    never persisted. Each run is fully isolated.
    """
    resp = requests.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )
    if resp.status_code in (401, 403):
        raise ValueError(
            "Invalid GitHub token. Please check and try again."
        )
    resp.raise_for_status()
    return resp.json()["login"]


def _build_agent_event(agent_name: str, state: AgentState) -> dict:
    base = {"agent": agent_name, "status": state.status, "error": state.error}
    if agent_name == "issue_analyzer":
        base.update(
            {
                "issue_type": state.issue_type,
                "complexity": state.complexity,
                "relevant_files": state.relevant_files,
                "affected_areas": state.affected_areas,
                "summary": state.summary,
            }
        )
    elif agent_name == "codebase_researcher":
        base.update(
            {
                "num_files": len(state.code_context),
                "low_confidence": state.low_confidence,
                "summary": f"{len(state.code_context)} files with relevant context",
            }
        )
    elif agent_name == "fix_drafter":
        base.update(
            {
                "num_files_fixed": len(state.proposed_fix),
                "fix_summary": state.summary,
            }
        )
    elif agent_name == "test_writer":
        base.update(
            {
                "test_file": state.test_file_path,
                "test_length": len(state.test_code),
            }
        )
    elif agent_name == "pr_creator":
        base.update({"pr_url": state.pr_url})
    return base


async def _run_pipeline(
    run_id: str, issue_url: str, queue: asyncio.Queue,
    github_token: str = "",
) -> None:
    loop = asyncio.get_event_loop()
    try:
        issue = await loop.run_in_executor(executor, fetch_issue, issue_url)
        await queue.put(("pipeline_start", {"issue": issue}))

        orchestrator = Orchestrator(max_retries=3)
        state = AgentState(issue=issue, github_token=github_token)

        while state.status not in ("done", "failed"):
            agent_name = orchestrator.route(state)
            if agent_name is None:
                break

            await queue.put(("agent_start", {"agent": agent_name}))

            await loop.run_in_executor(executor, orchestrator.step, state)

            if state.status == "failed":
                await queue.put(
                    ("agent_error", {"agent": agent_name, "error": state.error})
                )
                break

            event_data = _build_agent_event(agent_name, state)
            await queue.put(("agent_complete", event_data))

        if state.status == "done":
            files = [
                {"path": p, "diff": c[:2000]}
                for p, c in state.proposed_fix.items()
            ]
            await queue.put(
                (
                    "pipeline_done",
                    {
                        "pr_url": state.pr_url,
                        "summary": state.summary,
                        "issue_type": state.issue_type,
                        "complexity": state.complexity,
                        "usage": state.usage,
                        "files": files,
                    },
                )
            )
        else:
            msg = state.error or (
                f"Pipeline ended with status '{state.status}' and no error message. "
                f"proposed_fix={{{', '.join(state.proposed_fix.keys())}}}"
            )
            await queue.put(("pipeline_failed", {"error": msg}))
    except Exception as exc:
        await queue.put(
            ("pipeline_failed", {"error": f"{type(exc).__name__}: {exc}"})
        )
    finally:
        await queue.put(("__done__", {}))


@app.post("/run")
async def run(body: RunRequest):
    try:
        github_username = validate_github_token(body.github_token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    run_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    run_queues[run_id] = queue
    asyncio.ensure_future(
        _run_pipeline(run_id, body.issue_url, queue, body.github_token)
    )
    return {"run_id": run_id, "github_username": github_username}


@app.get("/stream/{run_id}")
async def stream(run_id: str):
    queue = run_queues.get(run_id)
    if queue is None:
        return StreamingResponse(
            iter([f"event: pipeline_failed\ndata: {json.dumps({'error': 'Run not found'})}\n\n"]),
            media_type="text/event-stream",
        )

    async def generate():
        while True:
            event, data = await queue.get()
            if event == "__done__":
                break
            yield f"event: {event}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
