---
title: "Step 0: Getting Started"
weight: 10
---

## Learning Objectives

By the end of this step, you will:
- Access your pre-provisioned AWS account through Workshop Studio
- Access your pre-configured VS Code editor running on EC2
- Understand the project structure and key files you'll work with
- Set up the Python environment and verify all infrastructure is ready

::alert[This workshop is designed to run in AWS-hosted events with pre-provisioned sandbox accounts. All infrastructure (Aurora, Cognito, Glue, Bedrock KB, EC2 Code Editor) is deployed automatically via CloudFormation when the event starts.]{type="info"}

## Access Your AWS Account

1. Visit the one-click join link provided by your workshop administrator (e.g., `https://catalog.us-east-1.prod.workshops.aws/join?access-code=xxxx-xxxxxx-xx`)
2. Choose **Email one-time password (OTP)**, enter your email address, and choose **Send passcode**
3. Check your email for the OTP, enter it, and choose **Sign in**
4. On the **Review and join** page, select **I agree with the Terms and Conditions**, then choose **Join event**
5. From the left sidebar, choose **Open AWS console**

::alert[This account will expire at the end of the workshop and all resources will be automatically cleaned up. You will not be able to access this account after the event ends.]{type="warning"}

## Access Your Code Editor

Your AWS account comes with a pre-configured VS Code editor running on EC2, accessible via CloudFront.

1. From the Workshop Studio dashboard, find the **CodeEditorUrl** output
2. Click the URL — it opens VS Code in your browser
3. The password token is embedded in the URL, so you should be logged in automatically

::alert[If the URL doesn't work or you see a password prompt, go to the CloudFormation console, find the `agentic-analytics` stack, and check the **Outputs** tab for the `CodeEditorUrl`.]{type="info"}

## Explore the Environment

Click hamburger button (triple stacked lines) on top left. Open a terminal in Code Editor (**Terminal → New Terminal**) and verify the infrastructure outputs:

```bash
cd /workshop/agentic-analytics/app/agentcore_strands

# Check config.env has all values
cat config.env
```

You should see output like:

```
AWS_REGION=us-east-1
AWS_DEFAULT_REGION=us-east-1
AURORA_ENDPOINT=agentic-analytics-cluster.cluster-xxxxx.us-east-1.rds.amazonaws.com
AURORA_SECRET_ARN=arn:aws:secretsmanager:us-east-1:xxxx:secret:agentic-analytics/aurora/credentials-xxxxx
DATABASE_NAME=timely_unicorn
KNOWLEDGE_BASE_ID=XXXXXXXXXX
GLUE_DATABASE=timely_unicorn
COGNITO_USER_POOL_ID=us-east-1_XXXXXXXXX
VPC_ID=vpc-xxxxxxxxx
SECURITY_GROUP_ID=sg-xxxxxxxxx
SUBNET_IDS=subnet-xxx,subnet-xxx
COGNITO_USER_LOGIN_CLIENT_ID=xxxxxxxxxxxxxxxxx
COGNITO_DOMAIN=agentic-analytics-xxxxxxxxxxxx.auth.us-east-1.amazoncognito.com
```

::alert[If any values are missing, the CloudFormation stack may still be deploying. Wait a few minutes and check again.]{type="warning"}

## Set Up the Python Environment

The Python virtual environment is pre-installed on the EC2 instance. Activate it and install the `uv` package manager (needed by AgentCore for build process without Docker):

```bash
cd /workshop/agentic-analytics
source .venv/bin/activate
export AWS_DEFAULT_REGION=us-east-1
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

::alert[The :code[export AWS_DEFAULT_REGION=us-east-1]{showCopyAction=true} is needed because the AgentCore CLI defaults to `us-west-2`. The `uv` install enables AgentCore's direct code deploy mode — without it, deployment falls back to a container build.]{type="warning"}

## Explore the Codebase

Click the files icons on top left in the Code Editor UI to view the directory. Here are the key files you'll work with throughout the workshop:

```
app/agentcore_strands/
├── unicorn_rental_agent.py             # Main agent (TODOs throughout)
├── unicorn_rental_analytics.sop.md     # Agent behavior instructions
├── tools/                             # Lambda functions — you'll edit for RLS
│   ├── prebaked_sql_toolset_lambda.py  # 20+ prebaked analytics tools
│   ├── api_integration_toolset_lambda.py # API integrations (booking, etc.)
│   └── custom_sql_toolset_lambda.py    # Custom SQL queries
├── infra/                             # Deploy scripts — run as-is
│   ├── deploy_gateway.py              # Creates MCP Gateway
│   ├── deploy_data_toolset.py         # Deploys 27 analytics tools│   ├── deploy_api_toolset.py          # API integration tools
│   ├── deploy_sql_toolset.py
│   └── deploy_interceptor.py
├── policy/                            # Access control
│   └── deploy_policy.py
├── guardrails/                        # Content safety
│   └── deploy_guardrail.py
├── config.env                         # Environment configuration
└── requirements.txt
```

Open `unicorn_rental_agent.py` in the editor. You'll notice several `# TODO` comments — these are the integration points you'll complete in each step.

## Verification

- Code Editor opens in your browser without errors
- `config.env` contains values for `AWS_REGION`, `AURORA_ENDPOINT`, `AURORA_SECRET_ARN`, `KNOWLEDGE_BASE_ID`, `GLUE_DATABASE`, and `COGNITO_USER_POOL_ID`
- `source .venv/bin/activate` works without errors

## Troubleshooting

**No venv available**
- Run `python3 -m venv .venv` before `source .venv/bin/activate`

**Code Editor URL returns 403 or blank page**
- The CloudFront distribution may still be deploying. Wait 2-3 minutes and refresh.
- Check the CloudFormation stack status — it should be `CREATE_COMPLETE`.

**`config.env` is empty or missing values**
- The CloudFormation stack may still be running custom resources. Wait for `CREATE_COMPLETE` status.
- Check the CloudFormation **Events** tab for errors.

## Summary

Your environment is ready. You have a VS Code editor with all dependencies installed, connected to a pre-provisioned Aurora PostgreSQL database with sample data for a unicorn rental SaaS platform.

Next, you'll build your first AI agent → [Step 1: Build Your First Strands Agent](../01-test-basic-agent/)

## Reference Materials

- [Amazon Bedrock AgentCore Documentation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/)
- [Strands Agents SDK](https://strandsagents.com/latest/)
- [Workshop Studio User Guide](https://catalog.workshops.aws/docs/en-US/)
