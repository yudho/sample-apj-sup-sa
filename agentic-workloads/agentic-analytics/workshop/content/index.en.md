---
title: "Self-Service Analytics with Agentic AI on AWS"
weight: 0
---

## Give Your Business Users the Power to Ask Questions in Plain English

Welcome! In this workshop, you'll build an AI assistant that lets business users access data through natural conversation — no SQL required. Instead of waiting days for a BI team to run a report, your users simply ask: *"Who are my top customers this month?"* and get an instant, accurate answer.

This is **agentic data access** — an AI agent that understands your business context, selects the right database query, enforces security policies, and returns formatted insights. You'll build it step by step, starting with a simple local agent and progressively adding enterprise capabilities: centralized tool management, role-based access control, tenant data isolation for multi-tenant environment, content safety, and custom SQL with human approval.

::alert[This workshop uses :link[Amazon Bedrock AgentCore]{href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html"} and :link[Strands Agents SDK]{href="https://strandsagents.com/latest/"} — but the focus is on the **data access patterns**, not the tools The techniques you learn here apply to agentic analytics system in general.]{type="info"}

## The Scenario: Timely-Unicorn

**Timely-Unicorn** is a multi-tenant SaaS platform for unicorn rental businesses. Two rental companies — Mythical Unicorns and Mythic Unicorns — each manage their own fleet of unicorns, customers, bookings, and revenue on the platform. The unicorns will take the customers from a pick-up point to their destination.

Their staff and analysts need answers: *"Who are my top 3 customers this month"*, *"What unicorn breed gives the most revenue"*, *"Create a 3-hour booking for customer X with unicorn Y for tomorrow 1 pm."* But they don't know SQL, and the BI team is backlogged. **You are the Timely-Unicorn SaaS platform team** — you'll build the AI assistant that solves this for all your tenants.

## Architecture

:image[Full Architecture]{src="/static/images/full-architecture.png"}

**Pre-provisioned by CloudFormation:** Aurora PostgreSQL, Glue Data Catalog, Bedrock Knowledge Base, Cognito, EC2 Code Editor

**Built by you as SaaS platform team:** Agent, Gateway, Runtime, Policy, Guardrails, Custom SQL, RLS wiring

## What You'll Build

### Agent & Infrastructure (Steps 0-3)

| Step | What You Build | Why It Matters |
|------|---------------|---------------|
| 0 | Environment setup | Access your pre-configured workspace |
| 1 | Test a basic agent (exercise) | Understand how an LLM selects the right tool |
| 2 | Deploy agent infrastructure | Gateway + Runtime — the production foundation |
| 3 | Connect the chat UI | Give business users a conversational interface |

### Toolsets (Steps 4-6)

| Step | What You Build | Why It Matters |
|------|---------------|---------------|
| 4 | Deploy Prebaked SQL toolset | Analytics tools backed by database Views |
| 5 | Integrate with existing APIs | Connect agent to business APIs (booking example) |
| 6 | Custom SQL with RAG | Ad-hoc queries with Glue schema + Bedrock KB + human approval |

### Security, Multitenancy, & Governance (Steps 7-10)

| Step | What You Build | Why It Matters |
|------|---------------|---------------|
| 7 | Multi-Tenant Isolation & User Access | Cedar policies + JWT → PostgreSQL RLS |
| 8 | Guardrails | Block off-topic, PII |
| 9 | Observability | Trace agent behavior in CloudWatch |
| 10 | Evaluation | Measure quality with LLM-as-a-Judge |

### Advanced Data Access (Step 14)

| Step | What You Build | Why It Matters |
|------|---------------|---------------|
| 14 | Semantic Layer with Cube Core | Structured ad-hoc analytics — more flexible than Prebaked SQL, more reliable than Custom SQL |

## Target Audience

- Solutions Architects building AI-powered data access for business users
- **SaaS startups** adding self-service analytics to their multi-tenant platforms or internal analytics tooling
- Developers implementing agentic applications on AWS
- Anyone who wants to understand how to secure and govern AI agents in production

::alert[**For SaaS builders:** The pattern in this workshop — tenant isolation via RLS, role-based tool access via Cedar, shared infrastructure with per-session isolation — maps to the :link[SaaS Lens]{href="https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/saas-lens.html"} of the AWS Well-Architected Framework. You'll build a pool model where all tenants share the same agent, database, and Gateway — with isolation enforced at the data and policy layers.]{type="success"}

## Prerequisites

- Basic Python programming
- Fundamental understanding of AWS services
- No prior AI/ML experience required

## Supported Regions

This workshop runs in **us-east-1** (N. Virginia) and **us-west-2** (Oregon). Amazon Bedrock AgentCore and the required foundation models are available in both regions.

## Estimated Time

- **Agent & Infrastructure (Steps 0-3):** ~0.5 hours
- **Toolsets (Steps 4-7):** ~1 hours
- **Security, Multitenancy, & Governance (Steps 7-10):** ~1.5 hour
- **Total:** ~3 hours

## Cost Information

::alert[This workshop is designed to run at AWS-hosted events with pre-provisioned accounts at no cost to you. All resources are automatically cleaned up when the event ends.]{type="success"}

For reference, the estimated AWS cost for running the full workshop infrastructure for 6 hours is approximately **$15–20**, primarily from :link[Amazon Bedrock]{href="https://aws.amazon.com/bedrock/pricing/"} model invocations ($12), :link[Amazon EC2]{href="https://aws.amazon.com/ec2/pricing/"} instances ($1.50), and :link[Aurora Serverless v2]{href="https://aws.amazon.com/rds/aurora/pricing/"} ($0.70).

---

Let's start by understanding the business scenario → [Introduction](introduction/)
