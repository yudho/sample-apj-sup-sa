---
title: "Step 7: Multi-Tenant Isolation & User Access"
weight: 50
---

## Learning Objectives

By the end of this step, you will:
- Enforce tool-level access control with Cedar policies (analysts can't create bookings)
- Switch from the database owner role to an RLS-enforced role for tenant data isolation
- Deploy a Gateway Interceptor that propagates JWT claims to Lambda targets
- Wire JWT claims through to PostgreSQL session variables for row-level security
- Verify that different tenants see only their own data

## The Problem

Your current analytics assistant up to the previous step has two security gaps:

1. **Any user can use any tool.** Analyst Orion Moonshadow can create bookings — but analysts should only read data, not modify it.
2. **Any user can see all tenants' data.** A user at Mythical Unicorns can see Mythic Unicorns' customers and revenue. In a multi-tenant SaaS platform, this is a data breach.

You need two layers of security:
- **Tool-level access control** — which tools each role can use
- **Data-level isolation** — which rows each tenant and role can see

::alert[**SaaS critical:** In a pool model (shared database), a single misconfigured query could expose one tenant's data to another. You need enforcement at the **infrastructure level** — not in the agent's prompt, not in the application code. :link[AgentCore Policy]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html"} handles tool access. :link[PostgreSQL Row-Level Security]{href="https://www.postgresql.org/docs/current/ddl-rowsecurity.html"} handles data isolation. Together, they make multi-tenant security deterministic and auditable.]{type="warning"}

## The Solution

| Layer | Technology | What It Controls | Enforcement Point |
|-------|-----------|-----------------|-------------------|
| **Tool access** | :link[AgentCore Policy (Cedar)]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html"} | Which tools each role can use | Gateway — tool is hidden from unauthorized users |
| **Data isolation** | :link[PostgreSQL RLS]{href="https://www.postgresql.org/docs/current/ddl-rowsecurity.html"} | Which rows each tenant can see | Database engine — impossible to bypass from application |
| **Identity propagation** | :link[Gateway Interceptor]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-headers.html"} | Passes JWT from Gateway to Lambda targets | Gateway — forwards Authorization header to targets |

The flow:

```
User logs in → Cognito JWT (custom:role [for role-based access control], custom:account_id [for tenant isolation])
  → Agent passes JWT to Gateway as Bearer token
    → Cedar Policy evaluates: can this role use this tool?
    → Interceptor propagates JWT to Lambda target with header injection
      → Lambda extracts claims, SETs PostgreSQL session variables
        → RLS policy: WHERE account_id = get_current_account_id()
          → Only this tenant's rows returned
```

## Lab Procedures

### Step 7.1: Deploy Cedar Policies (TODO 7.1)

:link[AgentCore Policy]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html"} uses :link[Cedar]{href="https://www.cedarpolicy.com/"}, a policy language created by AWS. Cedar policies are **deterministic** — unlike prompt-based restrictions, they use formal logic for safeguarding against prompt injection.

Open :code[policy/deploy_policy.py]{showCopyAction=true} and find `TODO 7.1`. Uncomment the `forbid_write_policy` block:

::::expand{header="💡 Need help with TODO 7.1? Click to see the solution"}
Uncomment the entire `forbid_write_policy = f'''...'''` block. This Cedar policy says: "Forbid any OAuthUser whose `custom:role` tag equals `analyst` from calling `APIInteg___create_booking_tool`." The `when` clause is the condition — only analysts are blocked.
::::

Deploy the Policy Engine:

```bash
python3 policy/deploy_policy.py
```

This creates a Policy Engine with three Cedar policies and attaches it to the Gateway in LOG_ONLY mode. Then switch to enforcement:

```bash
python3 policy/deploy_policy.py --enforce
```

The three policies:

```cedar
// 1. Base permit — allow all tools for any authenticated principal
permit(principal, action, resource == AgentCore::Gateway::"<arn>");

// 2. Forbid booking tool for analysts
forbid(principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"APIInteg___create_booking_tool",
  resource == AgentCore::Gateway::"<arn>")
when { principal.getTag("custom:role") == "analyst" };

// 3. Forbid Custom SQL for staff (too risky for non-technical users)
forbid(principal is AgentCore::OAuthUser,
  action in [AgentCore::Action::"CustomSQL___text_to_sql_tool",
             AgentCore::Action::"CustomSQL___execute_sql_tool"],
  resource == AgentCore::Gateway::"<arn>")
when { principal.getTag("custom:role") == "staff" };
```

::alert[**Forbid wins over permit.** Cedar uses default-deny with forbid-wins semantics. The base permit allows everything, then forbid policies carve out exceptions. If any forbid matches, access is denied — regardless of permits. This is the :link[recommended pattern]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies.html"} for AgentCore Policy.]{type="info"}

### Step 7.2: Switch to the RLS-Enforced Database Role

Currently, your Lambda tools connect to the database as `postgres` — the table owner. In PostgreSQL, **table owners bypass Row-Level Security by default**. This means RLS policies have no effect, and all tenants' data is visible.

To fix this, you'll switch to `app_user` — a non-owner role that was created during CloudFormation deployment. Because `app_user` doesn't own the tables, PostgreSQL automatically enforces RLS policies on every query.

#### TODO 7.2: Switch Secrets Manager ARN to use app_user database user

Open :code[config.env]{showCopyAction=true}, locate `TODO 7.2` and find the commented-out `APP_AURORA_SECRET_ARN` line. Uncomment it:

```bash
# Before (postgres — bypasses RLS):
# APP_AURORA_SECRET_ARN=arn:aws:secretsmanager:...app-credentials-...

# After (app_user — RLS enforced):
APP_AURORA_SECRET_ARN=arn:aws:secretsmanager:...app-credentials-...
```

::alert[**What does this change?** The `AURORA_SECRET_ARN` secret contains `postgres` credentials (table owner, bypasses RLS). The `APP_AURORA_SECRET_ARN` secret contains `app_user` credentials (non-owner, RLS enforced). When the deploy scripts see `APP_AURORA_SECRET_ARN`, they configure the Lambda to use `app_user` instead of `postgres`. The username is stored inside the Secrets Manager secret — the Lambda code doesn't change.]{type="info"}

### Step 7.3: Understand RLS Session Variables

The Lambda tools SET PostgreSQL session variables from the JWT claims before executing queries. The RLS policies use these variables to filter rows:

```sql
-- RLS policy on the customers table (already in the schema):
CREATE POLICY tenant_read_customers ON customers
  FOR SELECT USING (account_id = get_current_account_id());
-- get_current_account_id() reads the session variable SET by the Lambda
```

Open :code[tools/prebaked_sql_toolset_lambda.py]{showCopyAction=true} and look at `get_db_connection`:

```python
def get_db_connection(rls_context=None):
    ...
    if rls_context and (rls_context.get('account_id') or rls_context.get('role')):
        with conn.cursor() as cur:
            if rls_context.get('account_id'):
                cur.execute("SET app.current_account_id = %s", [rls_context['account_id']])
            if rls_context.get('role'):
                cur.execute("SET app.current_user_role = %s", [rls_context['role']])
    return conn
```

This pattern is the same in all three Lambda toolsets. The `rls_context` is extracted from the JWT by `lambda_handler` and passed through the call chain. When the Lambda connects as `app_user` (after the credential switch in Step 7.2), PostgreSQL RLS policies read these session variables to filter rows by tenant.

### Step 7.4: Examine the Gateway Interceptor

The Lambda tools now know how to SET session variables from JWT claims — but the JWT needs to reach the Lambda first. By default, the Gateway authenticates the request but does **not** forward the Authorization header to Lambda targets.

The :link[Gateway Interceptor]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-headers.html"} solves this. You deployed it in Step 2 — it's a Lambda function that runs on every Gateway request and injects headers into the target call. The `Authorization` header from the interceptor response is :link[automatically propagated to the target]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-headers.html#gateway-headers-interceptor-propagation"}.

Open :code[infra/interceptor_lambda.py]{showCopyAction=true} and look at the key section:

```python
# Extract Authorization header (case-insensitive)
auth_header = None
for key, value in headers.items():
    if key.lower() == 'authorization':
        auth_header = value
        break

# Propagate to Lambda targets
response_headers = {}
if auth_header:
    response_headers['Authorization'] = auth_header
```

This is the bridge between the Gateway (which validates the JWT) and the Lambda (which reads the JWT claims for RLS). Without it, `_extract_rls_context_from_jwt()` in the Lambda would receive no headers and RLS would have no tenant or user's role context.

### Step 7.5: Redeploy All Lambda Tools

Re-run the three deploy scripts. They will pick up the new `APP_AURORA_SECRET_ARN` from `config.env` and update the Lambda configuration to use `app_user`:

```bash
python3 infra/deploy_data_toolset.py
python3 infra/deploy_api_toolset.py
python3 infra/deploy_sql_toolset.py
```

### Step 7.6: Test Tool-Level Access Control

All test users share the same password: :code[Unicorn123!]{showCopyAction=true}

| User | Role | Tenant |
|------|------|--------|
| :code[lyra.starwhisper@example-mythicalunicorns.com]{showCopyAction=true} | rental_admin | Mythical Unicorns |
| :code[orion.moonshadow@example-mythicalunicorns.com]{showCopyAction=true} | analyst | Mythical Unicorns |
| :code[aria.skybloom@example-mythicunicorns.com]{showCopyAction=true} | rental_admin | Mythic Unicorns |

::alert[**Start fresh:** It is best to clear the chatbot conversation from the previous step by clicking the small bin icon next to the chat input field or by refreshing the application demo browser tab.]{type="info"}

**Test as Admin (Lyra):**
1. Log in as Lyra, ask: **"Show me top 5 customers by revenue"**
2. You should see customers.
3. Ask: **"Create a booking for my top customer next Sunday 2:30 pm for 30 mins with unicorn Vega Sapphire"** — it should work

**Test as Analyst (Orion):**
4. Log out, log in as Orion
5. Ask: **"Show me top 5 customers by revenue"** — same query, same tenant data
6. Ask: **"Create a booking for my top customer next Sunday 2:30 pm for 30 mins with unicorn Vega Sapphire"** — the agent cannot do this. The `create_booking_tool` is **inaccessible** for the analyst.

### Step 7.7: Test Tenant Data Isolation

This is the most important test. Log in as users from **different tenants** and verify they see different data.

**As Mythical Unicorns (Lyra):**
1. Ask: **"Show me top 3 customers"** — note the customer names
2. Ask: **"What's my total revenue?"** — note the figure

**As Mythic Unicorns (Aria):**
3. Log out, log in as :code[aria.skybloom@example-mythicunicorns.com]{showCopyAction=true}
4. Ask: **"Show me top 3 customers"** — you should see **completely different** names
5. Ask: **"What's my total revenue?"** — the figure should be different

::alert[**This is the pool model in action.** Both tenants share the same agent, same Gateway, same Lambda, same database — but each sees only their own data. The isolation is enforced by PostgreSQL RLS, not by the application. Even if the LLM generates a Custom SQL query without a tenant filter, RLS still protects the data.]{type="success"}

### Step 7.8: The Invisibility Test

The Cedar policy doesn't just *refuse* the booking tool — it makes it **invisible**.

1. Log in as Orion (analyst), ask: **"What tools do you have?"**
2. The agent lists its tools — Create Booking tool is **not in the list**
3. Log in as Lyra (admin), ask the same — The Create Booking tool **appears**

::alert[**Infrastructure-level vs prompt-level security.** A prompt restriction ("don't let analysts create bookings") can be bypassed with prompt injection. Cedar policy cannot — the tool literally doesn't exist in the analyst's session. The agent can't call what it can't see.]{type="info"}

## How It All Fits Together

```
┌─────────────────────────────────────────────────────────┐
│                    Security Layers                        │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  Layer 1: AgentCore Policy (Cedar)                       │
│  ├─ Evaluates JWT claims (custom:role)                   │
│  ├─ Hides unauthorized tools from agent                  │
│  └─ Enforcement: Gateway level                           │
│                                                          │
│  Layer 2: Gateway Interceptor                            │
│  ├─ Propagates Authorization header to Lambda            │
│  └─ Enables identity-aware tool execution                │
│                                                          │
│  Layer 3: PostgreSQL RLS                                 │
│  ├─ Lambda SETs session vars from JWT claims             │
│  ├─ RLS policies filter rows by account_id              │
│  ├─ Views use security_invoker = true                    │
│  └─ Enforcement: Database engine level                   │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

## Verification

- Policy Engine deploys with 3 Cedar policies in ~15 seconds
- `--enforce` switches to enforcement mode
- Admin (Lyra) can create bookings; analyst (Orion) cannot see the tool
- Mythical Unicorns user sees only Mythical Unicorns data
- Mythic Unicorns user sees only Mythic Unicorns data

## Troubleshooting

**Still seeing all tenants' data**
- Did you uncomment `APP_AURORA_SECRET_ARN` in `config.env`?
- Did you re-run all three deploy scripts after the change?
- Did you uncomment the SET statements in all three Lambda files?
- Did you deploy the interceptor (`deploy_interceptor.py`)?

**Analyst can still create bookings**
- Verify the policy is in ENFORCE mode (not LOG_ONLY)
- The user must log in via the Cognito Hosted UI (click Login button). Direct API auth doesn't carry OAuth claims.

**Queries return zero results**
- The interceptor must be deployed for JWT propagation. Without it, the Lambda has no JWT context and RLS blocks all rows (fail-closed).
- Check that you're logged in (not Guest mode).

## Reference Materials

- :link[AgentCore Policy Documentation]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy.html" external=true}
- :link[Cedar Policy Language]{href="https://www.cedarpolicy.com/" external=true}
- :link[AgentCore Gateway — Header Propagation]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-headers.html" external=true}
- :link[AgentCore Gateway — Interceptors]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors.html" external=true}
- :link[PostgreSQL Row-Level Security]{href="https://www.postgresql.org/docs/current/ddl-rowsecurity.html" external=true}
- :link[AWS SaaS Lens — Pool Model Data Isolation]{href="https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/pool-model.html" external=true}
- :link[Amazon Cognito — Pre-Token Generation Lambda]{href="https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-lambda-pre-token-generation.html" external=true}
