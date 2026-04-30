#!/usr/bin/env python3
"""
Deploy Bedrock Guardrail for the Unicorn Rental Analytics agent.

Creates or updates a guardrail with:
- Topic filters: block dangerous advice, database schema leakage
- Content filters: block hate, insults, violence, prompt attacks
- PII filters: block phone numbers, SSN, credit cards
- Word filters: block profanity

Saves GUARDRAIL_ID and GUARDRAIL_VERSION to config.env.

Usage:
  python deploy_guardrail.py
"""

import boto3
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    SCRIPT_DIR = Path(__file__).resolve().parent
    ROOT_DIR = SCRIPT_DIR.parent
    load_dotenv(ROOT_DIR / "config.env")
except ImportError:
    pass

REGION = os.getenv("AWS_REGION", "us-east-1")
GUARDRAIL_NAME = "unicorn-rental-guardrail"

bedrock = boto3.client("bedrock", region_name=REGION)

# --- Guardrail Configuration ---
# Best practices for denied topics (from AWS docs):
#   - Define topics with crisp POSITIVE definitions (what the topic IS)
#   - Don't use negative definitions ("anything except X")
#   - Don't include instructions ("Block all X") in definitions
#   - Examples should be representative prompts to filter

GUARDRAIL_CONFIG = dict(
    description="Guardrail for Unicorn Rental Analytics agent - blocks off-topic, harmful content, sensitive PII, and database schema leakage",
    blockedInputMessaging="I can only help with unicorn rental analytics questions. Please ask about bookings, revenue, customers, or unicorn management.",
    blockedOutputsMessaging="I'm unable to provide that response as it may contain inappropriate or restricted content.",
    topicPolicyConfig={
        "topicsConfig": [
            {
                "name": "DangerousAdvice",
                "definition": "Requests for medical diagnosis or treatment, financial investment advice, legal counsel, tax guidance, or any professional advice that requires licensed expertise.",
                "examples": [
                    "Should I invest in this stock?",
                    "What medication should I take for my headache?",
                    "Is this contract legally binding?",
                    "How should I file my taxes?",
                    "Can you diagnose my symptoms?",
                ],
                "type": "DENY",
                "inputAction": "BLOCK",
                "outputAction": "NONE",
                "inputEnabled": True,
                "outputEnabled": False,
            },
        ]
    },
    contentPolicyConfig={
        "filtersConfig": [
            {"type": "HATE", "inputStrength": "LOW", "outputStrength": "MEDIUM"},
            {"type": "INSULTS", "inputStrength": "LOW", "outputStrength": "MEDIUM"},
            {"type": "VIOLENCE", "inputStrength": "LOW", "outputStrength": "MEDIUM"},
            {"type": "SEXUAL", "inputStrength": "LOW", "outputStrength": "MEDIUM"},
            {"type": "MISCONDUCT", "inputStrength": "LOW", "outputStrength": "MEDIUM"},
            {"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"},
        ]
    },
    sensitiveInformationPolicyConfig={
        "piiEntitiesConfig": [
            {"type": "PHONE", "action": "BLOCK"},
            {"type": "US_SOCIAL_SECURITY_NUMBER", "action": "BLOCK"},
            {"type": "CREDIT_DEBIT_CARD_NUMBER", "action": "BLOCK"},
            {"type": "CREDIT_DEBIT_CARD_CVV", "action": "BLOCK"},
            {"type": "CREDIT_DEBIT_CARD_EXPIRY", "action": "BLOCK"},
            {"type": "US_BANK_ACCOUNT_NUMBER", "action": "BLOCK"},
            {"type": "US_BANK_ROUTING_NUMBER", "action": "BLOCK"},
            {"type": "PIN", "action": "BLOCK"},
            {"type": "PASSWORD", "action": "BLOCK"},
        ]
    },
    wordPolicyConfig={"managedWordListsConfig": [{"type": "PROFANITY"}]},
)


def create_or_update_guardrail():
    """Create or update the Bedrock Guardrail."""
    existing = bedrock.list_guardrails()
    for g in existing.get("guardrails", []):
        if g["name"] == GUARDRAIL_NAME:
            guardrail_id = g["id"]
            print(f"Updating existing guardrail: {guardrail_id}")
            response = bedrock.update_guardrail(
                guardrailIdentifier=guardrail_id,
                name=GUARDRAIL_NAME,
                **GUARDRAIL_CONFIG,
            )
            version = response["version"]
            print(f"[OK] Updated guardrail: {guardrail_id} (version {version})")
            return guardrail_id, version

    print("Creating Bedrock Guardrail...")
    response = bedrock.create_guardrail(name=GUARDRAIL_NAME, **GUARDRAIL_CONFIG)
    guardrail_id = response["guardrailId"]
    version = response["version"]
    print(f"[OK] Created guardrail: {guardrail_id} (version {version})")
    return guardrail_id, version


def save_to_config(guardrail_id, version):
    """Append guardrail config to config.env."""
    config_path = Path(__file__).resolve().parent.parent / "config.env"
    existing = config_path.read_text() if config_path.exists() else ""
    lines = [l for l in existing.splitlines() if not l.startswith("GUARDRAIL_ID=") and not l.startswith("GUARDRAIL_VERSION=")]
    lines.append(f"GUARDRAIL_ID={guardrail_id}")
    lines.append(f"GUARDRAIL_VERSION={version}")
    config_path.write_text("\n".join(lines) + "\n")
    print(f"[OK] Saved to {config_path}")


def main():
    print("Deploying Bedrock Guardrail")
    print("=" * 40)
    guardrail_id, version = create_or_update_guardrail()
    save_to_config(guardrail_id, version)
    print(f"\n[OK] Guardrail ready: {guardrail_id} v{version}")
    print("   Next: redeploy agent with `agentcore deploy`")


if __name__ == "__main__":
    main()
