#!/usr/bin/env python3
"""Deploy AgentCore Memory (short-term only).

Creates a Memory resource for conversation history within sessions.
Saves the memory ID to config.env for the agent to use.
"""
import os, sys, time, json
from pathlib import Path
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
load_dotenv(ROOT_DIR / 'config.env')

REGION = os.getenv('AWS_REGION', 'us-east-1')
MEMORY_NAME = "unicorn_rental_agent_memory"


def main():
    try:
        from bedrock_agentcore.memory import MemoryClient
    except ImportError:
        os.system(f"{sys.executable} -m pip install bedrock-agentcore -q")
        from bedrock_agentcore.memory import MemoryClient

    client = MemoryClient(region_name=REGION)

    # Check if memory already exists
    existing = client.list_memories()
    for m in existing:
        if MEMORY_NAME in str(m.get('arn', '')):
            memory_id = m['id']
            print(f"[OK] Memory already exists: {memory_id}")
            _save_memory_id(memory_id)
            return memory_id

    # Create new memory (STM only — no strategies)
    print("Creating AgentCore Memory (short-term only)...")
    memory = client.create_memory_and_wait(
        name=MEMORY_NAME,
        strategies=[],  # No strategies = STM only
        description="Short-term conversation memory for unicorn rental analytics agent",
        event_expiry_days=7,
    )
    memory_id = memory['id']
    print(f"[OK] Memory created: {memory_id}")

    _save_memory_id(memory_id)
    return memory_id


def _save_memory_id(memory_id):
    """Append MEMORY_ID to config.env."""
    env_path = ROOT_DIR / 'config.env'
    env_content = env_path.read_text() if env_path.exists() else ""

    if 'MEMORY_ID=' in env_content:
        lines = env_content.split('\n')
        lines = [l if not l.startswith('MEMORY_ID=') else f'MEMORY_ID={memory_id}' for l in lines]
        env_path.write_text('\n'.join(lines))
    else:
        with open(env_path, 'a') as f:
            f.write(f'\nMEMORY_ID={memory_id}\n')

    print(f"Memory ID saved to config.env: {memory_id}")


if __name__ == "__main__":
    main()
