"""One-time script to create a Managed Agent and print its ID for .env configuration."""
from __future__ import annotations

import os
import sys

import anthropic

from claude_slack_bot.agent.prompts import SYSTEM_PROMPT
from claude_slack_bot.agent.tools import CUSTOM_TOOLS


def main() -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable is required.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    print("Creating managed agent...")

    try:
        agent = client.beta.agents.create(  # type: ignore[attr-defined]
            name="claude-slack-bot",
            model="claude-sonnet-4-6",
            system=SYSTEM_PROMPT,
            tools=[
                {"type": "agent_toolset_20260401"},
                *CUSTOM_TOOLS,
            ],
        )

        print(f"\nAgent created successfully!")
        print(f"  Agent ID:      {agent.id}")
        print(f"  Agent Version: {agent.version}")
        print(f"\nAdd these to your .env file:")
        print(f"  AGENT_ID={agent.id}")
        print(f"  AGENT_VERSION={agent.version}")
        print(f"  DEFAULT_BACKEND=managed")

    except anthropic.APIError as e:
        print(f"Error creating agent: {e}")
        print("\nNote: The Managed Agents API requires beta access.")
        print("You can use DEFAULT_BACKEND=messages in the meantime.")
        sys.exit(1)


if __name__ == "__main__":
    main()
