#!/usr/bin/env python3
"""Deploy AgentCore MCP Gateway with Cognito user-based OAuth.

Creates the Gateway infrastructure (no targets). Targets are added
separately by deploy_data_toolset.py, deploy_api_toolset.py, etc.

Uses the existing Cognito User Pool (from CFN) for JWT validation.
The gateway accepts user login tokens — no M2M credentials needed.
"""
import os, sys, json, time, logging, boto3
from pathlib import Path
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
load_dotenv(dotenv_path=ROOT_DIR / 'config.env', override=True)

REGION = os.getenv('AWS_REGION', 'us-east-1')
GATEWAY_NAME = f"AgenticAnalyticsMCPGateway-{int(time.time())}"

try:
    from bedrock_agentcore_starter_toolkit.operations.gateway.client import GatewayClient, create_gateway_execution_role
except ImportError:
    print("Installing bedrock-agentcore-starter-toolkit...")
    os.system(f"{sys.executable} -m pip install bedrock-agentcore-starter-toolkit -q")
    from bedrock_agentcore_starter_toolkit.operations.gateway.client import GatewayClient, create_gateway_execution_role


def setup_user_login_client(pool_id):
    """Add CloudFront callback URL to the CFN-created user login client."""
    cognito = boto3.client('cognito-idp', region_name=REGION)
    user_login_client_id = os.getenv('COGNITO_USER_LOGIN_CLIENT_ID')

    if not user_login_client_id:
        print("[SKIP] No COGNITO_USER_LOGIN_CLIENT_ID in config.env")
        return None

    try:
        desc = cognito.describe_user_pool_client(UserPoolId=pool_id, ClientId=user_login_client_id)
        client_config = desc['UserPoolClient']

        callbacks = list(client_config.get('CallbackURLs', []))
        logouts = list(client_config.get('LogoutURLs', []))

        # Add CloudFront /app URL to callbacks
        try:
            cfn = boto3.client('cloudformation', region_name=REGION)
            outputs = cfn.describe_stacks(StackName='main-stack')['Stacks'][0]['Outputs']
            code_editor_url = next((o['OutputValue'] for o in outputs if o['OutputKey'] == 'CodeEditorUrl'), None)
            if code_editor_url:
                cf_domain = code_editor_url.split('?')[0].rstrip('/')
                app_url = f"{cf_domain}/app"
                if app_url not in callbacks:
                    callbacks.append(app_url)
                    logouts.append(app_url)
                    print(f"[OK] Added callback URL: {app_url}")
        except Exception as e:
            print(f"Warning: Could not detect CloudFront URL: {e}")

        cognito.update_user_pool_client(
            UserPoolId=pool_id,
            ClientId=user_login_client_id,
            ClientName=client_config.get('ClientName', 'user-login'),
            ExplicitAuthFlows=client_config.get('ExplicitAuthFlows', []),
            SupportedIdentityProviders=client_config.get('SupportedIdentityProviders', ['COGNITO']),
            CallbackURLs=callbacks,
            LogoutURLs=logouts,
            AllowedOAuthFlows=client_config.get('AllowedOAuthFlows', ['code']),
            AllowedOAuthScopes=client_config.get('AllowedOAuthScopes', []),
            AllowedOAuthFlowsUserPoolClient=True,
            ReadAttributes=client_config.get('ReadAttributes', []),
            WriteAttributes=client_config.get('WriteAttributes', []),
        )
        print(f"[OK] Updated user login client: {user_login_client_id}")
    except Exception as e:
        print(f"Warning: Failed to update user login client: {e}")

    return user_login_client_id


def main():
    print("Deploying AgentCore MCP Gateway")
    print("=" * 70)

    pool_id = os.getenv('COGNITO_USER_POOL_ID')
    user_login_client_id = os.getenv('COGNITO_USER_LOGIN_CLIENT_ID')

    if not pool_id or not user_login_client_id:
        print("Error: COGNITO_USER_POOL_ID and COGNITO_USER_LOGIN_CLIENT_ID required in config.env")
        sys.exit(1)

    # Setup callback URLs on user login client
    setup_user_login_client(pool_id)

    # Build authorizer — accepts user login tokens only
    discovery_url = f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}/.well-known/openid-configuration"
    authorizer_config = {
        'customJWTAuthorizer': {
            'discoveryUrl': discovery_url,
            'allowedClients': [user_login_client_id],
        }
    }
    print(f"[OK] Authorizer: Cognito pool {pool_id}, client {user_login_client_id}")

    # Create execution role
    role_arn = create_gateway_execution_role(boto3.Session(region_name=REGION), logging.getLogger())
    print(f"[OK] Execution role: {role_arn}")

    # Create gateway
    print("Creating Gateway...")
    agentcore = boto3.client('bedrock-agentcore-control', region_name=REGION)

    gateway = agentcore.create_gateway(
        name=GATEWAY_NAME,
        roleArn=role_arn,
        protocolType='MCP',
        authorizerType='CUSTOM_JWT',
        authorizerConfiguration=authorizer_config,
        description='AgentCore Gateway for Agentic Analytics',
    )
    print(f"[OK] Created Gateway: {gateway['gatewayId']}")

    # Wait for READY
    for _ in range(60):
        status = agentcore.get_gateway(gatewayIdentifier=gateway['gatewayId']).get('status')
        if status == 'READY':
            break
        time.sleep(5)
    print(f"Gateway status: {status}")

    # Save to config.env
    env_path = ROOT_DIR / 'config.env'
    env_content = env_path.read_text() if env_path.exists() else ""
    lines = [l for l in env_content.splitlines()
             if not l.startswith("GATEWAY_URL=") and not l.startswith("GATEWAY_ID=") and not l.startswith("GATEWAY_ARN=")]
    lines.append(f"GATEWAY_URL={gateway['gatewayUrl']}")
    lines.append(f"GATEWAY_ID={gateway['gatewayId']}")
    lines.append(f"GATEWAY_ARN={gateway.get('gatewayArn', '')}")
    env_path.write_text("\n".join(lines) + "\n")

    print(f"\nGateway URL: {gateway['gatewayUrl']}")
    print(f"Gateway ID: {gateway['gatewayId']}")
    print(f"Configuration saved to: config.env")
    print("\nNext: Run deploy_data_toolset.py to add analytics tools")


if __name__ == "__main__":
    main()
