---
title: "Step 2: Deploy Agent Infrastructure"
weight: 20
---

## Learning Objectives

By the end of this step, you will:
- Deploy the MCP Gateway with Cognito OAuth authentication
- Connect your agent to the Gateway and deploy it to AgentCore Runtime
- Understand why Gateway + Runtime is the production foundation for agentic systems

## Why Deploy Infrastructure First?

In Step 1, you built a basic agent that runs in a development machine and connects directly to the database. That was an exercise. Now you'll build the actual agent: an agent deployed to :link[AgentCore Runtime]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html"} that discovers tools through an :link[MCP Gateway]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html"}.

At this point, the Gateway has **no toolsets yet** — it's empty infrastructure. You'll add toolsets in the next steps. Think of it like deploying a web server before adding any routes.

::alert[**SaaS pattern:** In a pool model, all tenants share the same Gateway and Runtime. You deploy the infrastructure once, then add toolsets incrementally. Each toolset becomes available to all tenants.]{type="info"}

## Lab Procedures

### Step 2.1: Deploy the MCP Gateway

The Gateway is the central MCP endpoint that all toolsets register to. Before deploying, let's understand how authentication flows through the architecture:

```
User (browser)
  → Cognito login (Authorization Code flow)
    → JWT with custom:role (for role-based access control) + custom:account_id (for tenant isolation)
      → React UI passes JWT to AgentCore Runtime
        → Agent forwards user's JWT as Bearer token to Gateway
          → Gateway validates JWT against allowed Cognito client
            → Gateway Interceptor propagates JWT to Lambda targets
              → Lambda extracts JWT claims for RLS
```

The architecture uses a **single token flow** — the user's JWT from Cognito login is passed all the way through:

| Layer | What the JWT does |
|-------|------------------|
| **Gateway authorizer** | Validates the token was issued by the allowed Cognito client |
| **Cedar policies in AgentCore Policy** | Reads `custom:role` to control which tools each user can see and use |
| **Gateway Interceptor** | Propagates the JWT to Lambda targets |
| **Lambda (RLS)** | Reads `custom:account_id` and `custom:role` to filter data per tenant and role |

The **User Pool with test users** was already created by CloudFormation (Step 0). The deploy script below creates the Gateway and configures it to accept tokens from the existing Cognito user login client.

::alert[**Single token, multiple layers.** The same JWT carries both identity (role, tenant) and authorization (permission to call Gateway). No separate machine-to-machine credentials are needed — the user's token authenticates the Gateway call AND provides identity for downstream security layers.]{type="info"}

```bash
cd /workshop/agentic-analytics/app/agentcore_strands
python3 infra/deploy_gateway.py
```

Expected output:

```
Creating Gateway...
[OK] Created Gateway: agenticanalyticsmcpgateway-xxxxxxxxxx-xxxxxxxxxx
Gateway status: READY

Gateway URL: https://agenticanalyticsmcpgateway-xxxxxxxxxx-xxxxxxxxxx.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp
Gateway ID: agenticanalyticsmcpgateway-xxxxxxxxxx-xxxxxxxxxx
```

### Step 2.2: Deploy the Gateway Interceptor

The interceptor passes the user's JWT token from the Gateway to the Lambda targets. Without it, the Lambda functions won't know which tenant and user persona is making the request — write operations like booking creation will fail, and data won't be filtered by tenant.

```bash
python3 infra/deploy_interceptor.py
```

Expected output:

```
============================================================
[OK] Deployment Complete!
============================================================

Configuration:
   Interceptor Lambda: gateway-auth-interceptor
   Interception Points: REQUEST
   Pass Headers: True

The interceptor will now propagate the Authorization header
from incoming requests to all Lambda targets, including
Analytics.

The Lambda can access the token via:
   event.get('headers', {}).get('Authorization')
```

::alert[**Why now?** The interceptor is a Gateway-level component. You'll see how the propagated JWT enables tenant isolation (Step 7) and role-based access control (Step 7) later.]{type="info"}

### Step 2.3: Configure the Agent (TODOs 2.3.1-2.3.4)

Open :code[/workshop/agentic-analytics/app/agentcore_strands/unicorn_rental_agent.py]{showCopyAction=true}. This is the actual agent you'll use for the rest of the workshop.

::alert[**Observability is built in.** Open `requirements.txt` — notice `strands-agents[otel]` and `aws-opentelemetry-distro`. These two libraries enable automatic OpenTelemetry tracing when deployed to AgentCore Runtime. You don't need to write any instrumentation code — the Runtime wraps your agent with `opentelemetry-instrument` automatically. You'll explore the traces in Step 9.]{type="info"}

#### TODO 2.3.1: Load the SOP as the System Prompt

The agent's behavior is defined by a :link[Standard Operating Procedure (SOP)]{href="https://github.com/strands-agents/agent-sop" external=true} — a markdown file with RFC 2119 constraints (MUST, SHOULD, MAY) that guide the agent's decisions. Find `TODO 2.3.1` near the top of the file and replace the basic one-liner with the SOP loader:

::::expand{header="💡 Need help with TODO 2.3.1? Click to see the solution"}
:::code{language=python showCopyAction=true}
SYSTEM_PROMPT = load_system_prompt()
:::
::::

::alert[The SOP file (`unicorn_rental_analytics.sop.md`) is already in the `agent/` folder. Take a moment to open it and skim the structure — you'll see query classification rules, tool priority, response formatting constraints, and the human-in-the-loop workflow for Custom SQL.]{type="info"}

#### TODO 2.3.2: Create the Agent

Wire the components together. Find `TODO 2.3.2` (further down, after TODO 2.4) — replace `None` with an :link[Agent]{href="https://strandsagents.com/latest/user-guide/concepts/agents/agent-loop/" external=true} constructor. Notice how `mcp_client` is included in the tools list alongside `current_datetime` — this is what connects the agent to the Gateway's MCP tools. The Gateway has no toolsets yet (you'll add them in later steps), but the wiring is in place.

::::expand{header="💡 Need help with TODO 2.3.2? Click to see the solution"}
:::code{language=python showCopyAction=true}
request_agent = Agent(model=bedrock_model, system_prompt=SYSTEM_PROMPT, tools=[mcp_client, current_datetime], hooks=[], callback_handler=None, state={"actor_id": actor_id, "session_id": runtime_session_id})
:::
::::

### Step 2.4: Add the Runtime Entrypoint (TODO 2.4)

#### TODO 2.4: Add the @app.entrypoint Decorator

This tells AgentCore Runtime which function handles incoming requests.

Find `TODO 2.4` in `unicorn_rental_agent.py`:

::::expand{header="💡 Need help with TODO 2.4? Click to see the solution"}
:::code{language=python showCopyAction=true}
@app.entrypoint
async def agent_invocation(payload, context):
:::
::::

### Step 2.5: Deploy to AgentCore Runtime

Now deploy the agent to the cloud. This involves two commands: `agentcore configure` (one-time setup) and `agentcore deploy` (builds and deploys).

#### Configure the agent

```bash
cd /workshop/agentic-analytics/app/agentcore_strands
agentcore configure --entrypoint unicorn_rental_agent.py --name unicorn_rental_agent --disable-memory
```

The CLI will prompt you with several questions. Use these answers:

| Prompt | Answer | Why |
|--------|--------|-----|
| Path or Press Enter to use detected dependency file | `requirements.txt` | Points to the Python dependencies |
| Select deployment type | `1` (Direct Code Deploy) | No Docker required — uses `uv` for fast packaging |
| Select Python runtime version | `2` (PYTHON_3_11) | Matches the EC2 Python version |
| Execution role ARN/name | Press Enter | Auto-creates the IAM role |
| S3 URI/path | Press Enter | Auto-creates the S3 bucket |
| Configure OAuth authorizer? | Press Enter (no) | We use IAM auth — the UI exchanges the user's Cognito JWT for IAM credentials via the Identity Pool |
| Configure request header allowlist? | Press Enter (no) | Not needed — the user's JWT is passed in the request payload, not headers |

This step will save the configuration into `.bedrock_agentcore.yaml`. Expected output:

```
│ 📄 Config saved to: /workshop/agentic-analytics/app/agentcore_strands/.bedrock_agentcore.yaml
```                                                                                                      

#### Deploy

```bash
agentcore deploy
```

This takes ~1-3 minutes. Expected output:

```
✅ Deployment completed successfully - Agent: arn:aws:bedrock-agentcore:us-east-1:xxxxxxxxxxxx:runtime/unicorn_rental_agent-xxxxxxxxxx
```

### Step 2.6: Test Without Memory — Follow-Up Fails

Let's test the agent that has only 1 tool right now, to get current date and time. We will also simulate how the agent will not be able to recall the previous question as there is no memory yet.

The agent requires a JWT token to authenticate. Fetch one for a test user via the CLI:

```bash
source /workshop/agentic-analytics/app/agentcore_strands/config.env

TOKEN=$(aws cognito-idp initiate-auth \
  --client-id "$COGNITO_USER_LOGIN_CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=lyra.starwhisper@example-mythicalunicorns.com,PASSWORD=Unicorn123! \
  --query 'AuthenticationResult.AccessToken' \
  --output text \
  --region us-east-1)
```

Now ask the agent a question to test it. This first invocation may take slightly longer time.

```bash
agentcore invoke "{\"prompt\": \"What time is it?\", \"gateway_token\": \"$TOKEN\"}" --session-id test-memory-session-00000000000000
```
Expected output:

A JSON output with message text "The current date and time is . . . UTC"

Continue asking a follow-up question

```bash
agentcore invoke "{\"prompt\": \"What was my question just now?\", \"gateway_token\": \"$TOKEN\"}" --session-id test-memory-session-00000000000000
```

Expected output:

A JSON output with message text "I don't have any record of a previous question from you in our current conversation"

The agent **can't remember** — it says something like "I don't have context from previous conversations." Each invocation is stateless.

### Step 2.7: Deploy Memory

Let's deploy AgentCore Memory. The below command can take a while ~4-5 minutes.

```bash
python3 infra/deploy_memory.py
```

Expected output:

```
Creating AgentCore Memory (short-term only)...

[OK] Memory created: unicorn_rental_agent_memory-xxxxxxxxxx
Memory ID saved to config.env: unicorn_rental_agent_memory-xxxxxxxxxx
```

This creates an :link[AgentCore Memory]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html"} resource with short-term memory (STM) — it stores conversation turns within sessions so the agent can remember what was said.

::alert[AgentCore Memory also supports **long-term memory** (LTM) for extracting and storing insights across sessions — such as user preferences and behavioral patterns. In this workshop we use STM only, but you can enable both types on the same memory resource. See the :link[AgentCore Memory documentation]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html"} for details.]{type="info"}

### Step 2.8: Enable Memory in the Agent (TODO 2.8)

Open :code[unicorn_rental_agent.py]{showCopyAction=true} and find `TODO 2.8`. The `MemoryHookProvider` class is already defined — it loads recent conversation history when the agent starts and saves each new message. You just need to wire it into the Agent constructor.

::::expand{header="💡 Need help with TODO 2.8? Click to see the solution"}
Uncomment the `hooks=memory_hooks,` line (the first one) and delete the `hooks=[],` line below it. Guardrails will be added later in Step 8.
::::

### Step 2.9: Redeploy and Test With Memory

When the agent code changes, we need to redeploy the agent into AgentCore Runtime.

```bash
agentcore deploy
```

Fetch a fresh token and try the same sequence:

```bash
TOKEN=$(aws cognito-idp initiate-auth \
  --client-id "$COGNITO_USER_LOGIN_CLIENT_ID" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=lyra.starwhisper@example-mythicalunicorns.com,PASSWORD=Unicorn123! \
  --query 'AuthenticationResult.AccessToken' \
  --output text \
  --region us-east-1)

agentcore invoke "{\"prompt\": \"What time is it?\", \"gateway_token\": \"$TOKEN\"}" --session-id test-memory-session-00000000000001
agentcore invoke "{\"prompt\": \"What was my question just now?\", \"gateway_token\": \"$TOKEN\"}" --session-id test-memory-session-00000000000001
```

The agent now remembers. The memory hook loaded the previous turn's context from AgentCore Memory before processing the follow-up.

::alert[**How it works:** The `MemoryHookProvider` has two hooks: (1) `on_agent_initialized` loads the last 5 conversation turns from AgentCore Memory into the agent's message history, and (2) `on_message_added` saves each new user/assistant message back to memory. This gives the agent conversation continuity across invocations.]{type="info"}

### Step 2.10: Enable Observability

While tracing is already enabled for AgentCore Runtime, here we will enable log delivery and tracing for the AgentCore Gateway and AgentCore Memory resources so you can monitor agent behavior in CloudWatch:

```bash
python3 infra/deploy_observability.py
```

Expected output:

```
[OK] Observability configuration complete
```

::alert[**Runtime tracing is automatic.** The `strands-agents[otel]` and `aws-opentelemetry-distro` packages in `requirements.txt` enable OpenTelemetry tracing when deployed to AgentCore Runtime — no extra configuration needed. This step enables observability for the Gateway and Memory resources specifically. You'll explore the traces and dashboards in Step 9.]{type="info"}

## Verification

- `deploy_gateway.py` creates the Gateway and saves config to `config.env`
- `agentcore deploy` deploys successfully
- Without memory: follow-up question fails ("I don't have context")
- `deploy_memory.py` creates memory and saves ID to `config.env`
- With memory: follow-up question succeeds
- `deploy_observability.py` enables logs and traces for Gateway and Memory

## Troubleshooting

**`deploy_gateway.py` fails with IAM errors**
- The EC2 role needs `bedrock-agentcore:*` and `cognito-idp:*` permissions (pre-configured by CloudFormation).

**`agentcore deploy` fails with Docker/ECR errors**
- The EC2 instance needs `ecr:*` and `codebuild:*` permissions.
- If you see "repository does not exist", retry — ECR is created automatically.

**`agentcore invoke` times out**
- First invocation may take 30-60 seconds (microVM cold start). Retry.
- Check `AWS_DEFAULT_REGION` is set: `echo $AWS_DEFAULT_REGION`

## Summary

You deployed the production infrastructure: an MCP Gateway (empty, ready for toolsets) and an agent on AgentCore Runtime (with streaming, but no analytics tools yet). In the next step, you'll add the first toolset.

Next → [Step 3: Connect the Chat UI](../03-connect-ui/)

## Reference Materials

- :link[AgentCore Gateway]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html"}
- :link[AgentCore Runtime]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agents-tools-runtime.html"}
- :link[MCP Protocol]{href="https://modelcontextprotocol.io/docs/getting-started/intro" external=true}
- :link[Strands Agents — MCP Client]{href="https://strandsagents.com/latest/user-guide/concepts/tools/mcp-tools/" external=true}
