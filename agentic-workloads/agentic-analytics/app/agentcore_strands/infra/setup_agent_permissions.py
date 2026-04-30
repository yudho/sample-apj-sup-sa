#!/usr/bin/env python3
"""
Setup additional IAM permissions for AgentCore agent execution role.
Run this after 'agentcore configure' and before 'agentcore launch'.
"""

import boto3
import yaml
from pathlib import Path

def setup_permissions():
    config_path = Path(__file__).parent / ".bedrock_agentcore.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)
    
    default_agent = config.get('default_agent')
    agent_config = config['agents'].get(default_agent)
    execution_role_arn = agent_config['aws'].get('execution_role')
    role_name = execution_role_arn.split('/')[-1]
    
    print(f"Adding permissions to role: {role_name}")
    iam = boto3.client('iam')
    
    try:
        iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn='arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess'
        )
        print("[OK] Added AmazonS3ReadOnlyAccess policy")
    except Exception as e:
        if 'already' in str(e).lower() or 'attached' in str(e).lower():
            print("[OK] AmazonS3ReadOnlyAccess already attached")
        else:
            print(f"⚠️ {e}")

if __name__ == "__main__":
    setup_permissions()
