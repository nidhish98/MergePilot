import sys
from urllib.parse import urlparse

import requests

from mergepilot.orchestrator import Orchestrator
from mergepilot.state import AgentState


def fetch_issue(url: str) -> dict:
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 4 or parts[2] != "issues":
        raise ValueError(f"Not a valid GitHub issue URL: {url}")

    owner, repo, _, number = parts
    api_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
    resp = requests.get(api_url)
    resp.raise_for_status()
    data = resp.json()

    return {
        "title": data["title"],
        "body": data.get("body", ""),
        "number": data["number"],
        "repo": f"{owner}/{repo}",
    }


def visualize_run(history: list[tuple[str, AgentState]]) -> None:
    """Print a clean timeline of what each agent produced."""
    print("\n" + "=" * 60)
    print("  Pipeline Timeline")
    print("=" * 60)
    for agent_name, state in history:
        if agent_name == "issue_analyzer":
            print(
                f"  [IssueAnalyzer]     type={state.issue_type}"
                f"  complexity={state.complexity}"
                f"  suggested={state.relevant_files}"
            )
        elif agent_name == "codebase_researcher":
            print(
                f"  [CodebaseResearcher] files={len(state.code_context)}"
                f"  low_confidence={state.low_confidence}"
            )
        elif agent_name == "fix_drafter":
            if not state.proposed_fix:
                print(f"  [FixDrafter]        FAILED: {state.error}")
            else:
                print(
                    f"  [FixDrafter]        fix={len(state.proposed_fix)} file(s)"
                )
        elif agent_name == "test_writer":
            print(
                f"  [TestWriter]        tests={len(state.test_code)} chars"
                f"  → {state.test_file_path or 'tests/test_fix.py'}"
            )
        elif agent_name == "pr_creator":
            print(f"  [PR Creator]        pr={state.pr_url}")

    final = history[-1][1] if history else None
    if final:
        print(f"\n  ── Result: status={final.status}")
        if final.error:
            print(f"     error={final.error}")


def main() -> None:
    if len(sys.argv) > 1:
        url = sys.argv[1]
    else:
        url = input("GitHub issue URL: ").strip()

    issue = fetch_issue(url)
    print(f"\nFetched issue #{issue['number']}: {issue['title']}\n")

    orchestrator = Orchestrator(max_retries=3)
    final_state = orchestrator.run(issue)

    visualize_run(orchestrator.state_history)

    print("\n=== Pipeline finished ===")
    print(f"  Status:        {final_state.status}")
    print(f"  Error:         {final_state.error}")
    print(f"  Issue type:    {final_state.issue_type}")
    print(f"  Affected:      {final_state.affected_areas}")
    print(f"  Complexity:    {final_state.complexity}")
    print(f"  Summary:       {final_state.summary}")
    print(f"  Suggested:     {final_state.relevant_files}")
    print(f"  Low confidence:{final_state.low_confidence}")
    print(f"  Retries:       {final_state.retry_count}")
    print(f"  PR URL:        {final_state.pr_url}")
    u = final_state.usage
    print(f"  Token usage:   {u['total_tokens']} total "
          f"({u['prompt_tokens']} prompt + {u['completion_tokens']} completion)")


if __name__ == "__main__":
    main()
