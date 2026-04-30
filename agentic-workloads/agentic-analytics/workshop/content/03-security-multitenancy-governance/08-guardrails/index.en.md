---
title: "Step 8: Guardrails"
weight: 55
---

## Learning Objectives

By the end of this step, you will:
- Understand why content safety matters for production AI agents
- Deploy a Bedrock Guardrail with topic blocking, content filtering, and PII protection
- See how native Bedrock model integration applies guardrails automatically

## The Problem

A tenant user asks: "What's the weather today?" The agent tries to answer — even though it's an analytics assistant, not a general chatbot. Worse, the user may ask: "What is the right medication for this medical condition?". In production, this wastes compute, confuses users, and could cause legal issue with wrong professional advice.

There's also a data protection concern: the agent could inadvertently process or leak sensitive information from the user or from the database.

::alert[**SaaS pattern:** In a shared-infrastructure model, guardrails protect your platform from tenant misuse — intentional or accidental. Guardrails enforce this at the model layer, complementing RLS at the data layer.]{type="info"}

## The Solution: Bedrock Guardrails

:link[Amazon Bedrock Guardrails]{href="https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html"} provide content filtering, topic blocking, and PII protection. They evaluate both user inputs and agent outputs, providing a defense in depth layer on top of SOPs and policies.

| Protection | What It Catches |
|-----------|----------------|
| **Topic filter** | Off-topic conversations (medical or legal advice) |
| **Content filter** | Hate speech, violence, insults |
| **PII filter** | Phone numbers, SSN, credit cards (allows names/emails needed for bookings) |
| **Profanity filter** | Offensive language |

## Lab Procedures

### Step 8.1: Configure the Guardrail Topics

Open :code[guardrails/deploy_guardrail.py]{showCopyAction=true} and find `TODO 8.1`. You'll see one commented-out denied topic that defines what the guardrail blocks. Read the topic name, definition, and examples:

- **DangerousAdvice** — blocks medical, legal, financial advice — requires licensed expertise

::::expand{header="💡 Need help with TODO 8.1? Click to see the solution"}
Uncomment the topic block (the dictionary inside `topicsConfig`). The topic has a `name`, `definition` (what to block), `examples` (training examples for the model), and `type: DENY`.
::::

After uncommenting, deploy the guardrail:

```bash
python3 guardrails/deploy_guardrail.py
```

Expected output:

```
Deploying Bedrock Guardrail
========================================
Creating Bedrock Guardrail...
[OK] Created guardrail: xxxxxxxxxxxx (version DRAFT)
[OK] Saved to /workshop/agentic-analytics/app/agentcore_strands/config.env

[OK] Guardrail ready: xxxxxxxxxxxx DRAFT
   Next: redeploy agent with `agentcore deploy`
```

The guardrail ID is saved to `config.env` so the agent can reference it.

::alert[**Guardrail version:** In this workshop we use DRAFT version of the guardrail. In production one can consider using a pinned numbered version for consistency and controlled versioning.]{type="info"}


### Step 8.2: Examine the Agent's Guardrail Integration

Open :code[unicorn_rental_agent.py]{showCopyAction=true} and look at **lines 71-90**:

```python
GUARDRAIL_ID = os.getenv("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.getenv("GUARDRAIL_VERSION", "DRAFT")

bedrock_model_kwargs = dict(model_id=model_id, temperature=0.3, streaming=True)
if GUARDRAIL_ID:
    bedrock_model_kwargs.update(
        guardrail_id=GUARDRAIL_ID,
        guardrail_version=GUARDRAIL_VERSION,
        guardrail_redact_input=True,
        guardrail_redact_input_message="I can only help with unicorn rental analytics...",
        guardrail_latest_message=True,
    )

bedrock_model = BedrockModel(**bedrock_model_kwargs)
```

This is **native Bedrock model integration** — the guardrail is applied at the model layer, not as a separate hook. When `GUARDRAIL_ID` is set (by `deploy_guardrail.py` saving to `config.env`), every Bedrock API call automatically evaluates the guardrail. Key parameters:

- `guardrail_redact_input=True` — blocked inputs are replaced with the safe message instead of being sent to the model
- `guardrail_latest_message=True` — only the latest user message is evaluated (not the full conversation history)

::alert[**No code changes needed.** The guardrail integration is already wired in — deploying the guardrail and redeploying the agent is all it takes. This is the advantage of native model-level integration over custom hooks.]{type="info"}

### Step 8.3: Redeploy and Test

The agent code already has native Bedrock guardrail integration — when `GUARDRAIL_ID` is set in `config.env`, the `BedrockModel` automatically applies guardrails to every request. No agent code changes needed.

```bash
agentcore deploy
```

::alert[**Start fresh:** It is best to clear the chatbot conversation from the previous step by clicking the small bin icon next to the chat input field or by refreshing the application demo browser tab.]{type="info"}

Now try these questions in the demo UI:

**Test 1: Normal queries still work**
- "Who is my top customer?" → Works normally — guardrails don't interfere with legitimate analytics

**Test 2: Dangerous advice blocking**
- "Is it a good time to invest in gold?" → Blocked by DangerousAdvice topic filter (financial investment advice requires licensed expertise)

## Verification

- `deploy_guardrail.py` creates the guardrail and saves the ID to `config.env`
-  Off-topic questions are blocked with a safe response
-  Normal analytics questions still work
-  The guardrail appears in the AWS Console: **Amazon Bedrock → Guardrails**

## Troubleshooting

**Guardrail doesn't seem to block anything**
- Verify `GUARDRAIL_ID` is in your `config.env` file (written by `deploy_guardrail.py`). If you're running locally, `cp config.env .env` again.
- Redeploy with `agentcore deploy`.

**Agent blocks legitimate analytics questions**
- The topic filter may be too aggressive. Check the guardrail configuration in the Bedrock console and adjust the denied topics.

**`deploy_guardrail.py` fails with "topic definition too long"**
- Bedrock has a character limit for topic definitions. The script uses a shortened definition — if you've modified it, keep it concise.

## Summary

You added a defense-in-depth layer with Bedrock Guardrails that evaluates every user message and agent response. Combined with SOPs (behavior guidance), Cedar policies (tool access), and RLS (data isolation), your agent now has four layers of protection:

| Layer | What It Controls | Enforcement |
|-------|-----------------|-------------|
| **SOP** | Agent behavior and response format | Best-effort (LLM follows instructions) |
| **Policy** | Which tools each role can access | Hard (Gateway hides tools) |
| **RLS** | Which data rows each tenant sees | Hard (database filters rows) |
| **Guardrails** | Content safety and topic boundaries | Hard (blocks violating content) |

Next, you'll learn to leverage observability to monitor your agent → [Step 9: Observability](../09-observability/)

## Reference Materials

- [Amazon Bedrock Guardrails](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html)
- [Bedrock Guardrails — Topic Filters](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-topic-filters.html)
- [Bedrock Guardrails — Content Filters](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-content-filters.html)
- [Strands Agents — Hooks](https://strandsagents.com/latest/user-guide/concepts/agents/hooks/)
