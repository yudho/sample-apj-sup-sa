#!/usr/bin/env python3
"""
Deploy AgentCore Policy — Cedar-based role-based access control.

This script:
1. Creates Policy Engine with Cedar policies
2. Attaches Policy Engine to Gateway (LOG_ONLY or ENFORCE)

Prerequisites (already done by CFN + deploy_gateway.py):
- Cognito User Pool with Pre-Token Lambda V2 (CFN)
- User login client with gateway scope (deploy_gateway.py)
- Test users with custom:role attribute (CFN)

Usage:
  python deploy_policy.py           # Deploy in LOG_ONLY mode
  python deploy_policy.py --enforce # Switch to ENFORCE mode
"""

import boto3
import json
import os
import time
from pathlib import Path
try:
    from dotenv import load_dotenv
    SCRIPT_DIR = Path(__file__).resolve().parent
    ROOT_DIR = SCRIPT_DIR.parent
    load_dotenv(ROOT_DIR / 'config.env')
except ImportError:
    pass

REGION = os.getenv("AWS_REGION", "us-east-1")

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
GATEWAY_ID = os.getenv('GATEWAY_ID', '')
GATEWAY_ARN = os.getenv('GATEWAY_ARN', '')

# Auto-discover gateway if not in config.env
if not GATEWAY_ID or not GATEWAY_ARN:
    _client = boto3.client('bedrock-agentcore-control', region_name=REGION)
    _gateways = _client.list_gateways().get('items', [])
    if _gateways:
        GATEWAY_ID = GATEWAY_ID or _gateways[0]['gatewayId']
        _detail = _client.get_gateway(gatewayIdentifier=GATEWAY_ID)
        GATEWAY_ARN = GATEWAY_ARN or _detail.get('gatewayArn', '')
        print(f"[AUTO] Gateway ID: {GATEWAY_ID}")
        print(f"[AUTO] Gateway ARN: {GATEWAY_ARN}")


def create_policy_engine():
    """Create or get existing Policy Engine"""
    client = boto3.client('bedrock-agentcore-control', region_name=REGION)
    engine_name = "TimeyUnicornPolicyEngine"

    try:
        for engine in client.list_policy_engines().get('policyEngines', []):
            if engine['name'] == engine_name:
                print(f"[OK] Using existing Policy Engine: {engine['policyEngineId']}")
                return engine
    except Exception as e:
        print(f"Error listing engines: {e}")

    print("Creating Policy Engine...")
    response = client.create_policy_engine(
        name=engine_name,
        description="Policy engine for Timely-Unicorn role-based access control"
    )
    engine_id = response['policyEngineId']
    print("[WAIT] Waiting for Policy Engine to be ready...")
    for _ in range(60):
        status = client.get_policy_engine(policyEngineId=engine_id)['status']
        if status in ('READY', 'ACTIVE'):
            break
        time.sleep(5)
    print(f"[OK] Created Policy Engine: {engine_id}")
    return {'policyEngineId': engine_id, 'policyEngineArn': response['policyEngineArn']}


def create_policies(policy_engine_id):
    """Create Cedar policies for role-based access control"""
    client = boto3.client('bedrock-agentcore-control', region_name=REGION)

    # Policy 1: Allow all tools for any principal
    allow_all_policy = f'''permit(
  principal,
  action,
  resource == AgentCore::Gateway::"{GATEWAY_ARN}"
);'''

    # TODO 10: Uncomment the forbid policy below to block analysts from creating bookings
    # Policy 2: Forbid create_booking for analyst role
    forbid_write_policy = f'''forbid(
  principal is AgentCore::OAuthUser,
  action == AgentCore::Action::"APIInteg___create_booking_tool",
  resource == AgentCore::Gateway::"{GATEWAY_ARN}"
)
when {{
  principal.hasTag("custom:role") &&
  principal.getTag("custom:role") == "analyst"
}};'''

    # Policy 3: Forbid Custom SQL tools for staff role
    forbid_text2sql_staff = f'''forbid(
  principal is AgentCore::OAuthUser,
  action in [
    AgentCore::Action::"CustomSQL___text_to_sql_tool",
    AgentCore::Action::"CustomSQL___get_schema_context_tool",
    AgentCore::Action::"CustomSQL___execute_sql_tool"
  ],
  resource == AgentCore::Gateway::"{GATEWAY_ARN}"
)
when {{
  principal.hasTag("custom:role") &&
  principal.getTag("custom:role") == "staff"
}};'''

    policy_definitions = [
        ('allow_all_tools', 'Allow all tools on the gateway', allow_all_policy),
        ('forbid_write_analyst', 'Forbid create_booking for analyst role', forbid_write_policy),
        ('forbid_text2sql_staff', 'Forbid Custom SQL tools for staff role', forbid_text2sql_staff),
    ]

    # Delete existing policies first
    existing = client.list_policies(policyEngineId=policy_engine_id)
    for p in existing.get('policies', []):
        if p['name'] in [pd[0] for pd in policy_definitions] or p['name'] == 'forbid_write_non_admin':
            try:
                client.delete_policy(policyEngineId=policy_engine_id, policyId=p['policyId'])
                print(f"  Deleted existing policy: {p['name']}")
            except Exception:
                pass
    time.sleep(3)

    for name, desc, cedar in policy_definitions:
        for attempt in range(3):
            try:
                client.create_policy(
                    policyEngineId=policy_engine_id, name=name, description=desc,
                    definition={'cedar': {'statement': cedar}},
                    validationMode='IGNORE_ALL_FINDINGS'
                )
                print(f"[OK] Created policy: {name}")
                break
            except client.exceptions.ConflictException:
                time.sleep(2)
            except Exception as e:
                print(f"Error creating policy {name}: {e}")
                break


def attach_policy_engine(policy_engine_arn, mode="LOG_ONLY"):
    """Attach Policy Engine to Gateway"""
    client = boto3.client('bedrock-agentcore-control', region_name=REGION)
    print(f"\nAttaching Policy Engine to Gateway ({mode} mode)...")

    gateway = client.get_gateway(gatewayIdentifier=GATEWAY_ID)
    update_params = {
        'gatewayIdentifier': GATEWAY_ID,
        'name': gateway['name'],
        'roleArn': gateway['roleArn'],
        'protocolType': gateway['protocolType'],
        'authorizerType': gateway['authorizerType'],
        'authorizerConfiguration': gateway.get('authorizerConfiguration', {}),
        'policyEngineConfiguration': {'arn': policy_engine_arn, 'mode': mode},
    }
    # Preserve interceptor config if exists
    if gateway.get('interceptorConfigurations') and gateway['interceptorConfigurations'] != 'NONE':
        update_params['interceptorConfigurations'] = gateway['interceptorConfigurations']
    client.update_gateway(**update_params)
    while client.get_gateway(gatewayIdentifier=GATEWAY_ID)['status'] != 'READY':
        time.sleep(3)
    print(f"[OK] Policy Engine attached in {mode} mode")


def main():
    print("=" * 70)
    print("AgentCore Policy Deployment — Cedar RBAC")
    print("=" * 70)
    print(f"\nGateway: {GATEWAY_ID}")
    print(f"Region: {REGION}\n")

    policy_engine = create_policy_engine()
    create_policies(policy_engine['policyEngineId'])
    attach_policy_engine(policy_engine['policyEngineArn'], mode="LOG_ONLY")

    # Save config
    # Save policy engine info to config.env
    env_path = ROOT_DIR / 'config.env'
    env_content = env_path.read_text() if env_path.exists() else ""
    lines = [l for l in env_content.splitlines() if not l.startswith("POLICY_ENGINE_ID=") and not l.startswith("POLICY_ENGINE_ARN=")]
    lines.append(f"POLICY_ENGINE_ID={policy_engine['policyEngineId']}")
    lines.append(f"POLICY_ENGINE_ARN={policy_engine['policyEngineArn']}")
    env_path.write_text("\n".join(lines) + "\n")

    print(f"""
{"=" * 70}
[OK] Deployment Complete!
{"=" * 70}

Policy Engine: {policy_engine['policyEngineId']}
Mode: LOG_ONLY (use --enforce to enable)

Cedar Policies:
  allow_all_tools       — permit all tools for any principal
  forbid_write_analyst  — forbid create_booking for analyst role
  forbid_text2sql_staff — forbid Custom SQL for staff role

Test Users (from CFN — password: Unicorn123! for all):
  lyra.starwhisper@example-mythicalunicorns.com (rental_admin)
  orion.moonshadow@example-mythicalunicorns.com (analyst)
  aria.skybloom@example-mythicunicorns.com (rental_admin)

Next: python deploy_policy.py --enforce
""")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--enforce':
        # Re-read config.env to pick up POLICY_ENGINE_ARN saved by previous run
        try:
            load_dotenv(ROOT_DIR / 'config.env', override=True)
        except NameError:
            pass
        policy_engine_arn = os.getenv('POLICY_ENGINE_ARN', '')
        if policy_engine_arn:
            attach_policy_engine(policy_engine_arn, mode="ENFORCE")
        else:
            print("Run without --enforce first to create policy engine")
    else:
        main()
