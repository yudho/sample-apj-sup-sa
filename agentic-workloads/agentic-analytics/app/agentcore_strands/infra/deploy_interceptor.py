#!/usr/bin/env python3
"""
Deploy Gateway Interceptor to pass auth token to Lambda targets.

The interceptor extracts the Authorization header from incoming requests
and propagates it to targets (like Analytics) so they can access
user identity information.

Usage:
  python deploy_interceptor.py
"""

import boto3
import json
import os
from pathlib import Path
import time
import zipfile
import io

try:
    from dotenv import load_dotenv
    SCRIPT_DIR = Path(__file__).resolve().parent
    ROOT_DIR = SCRIPT_DIR.parent
    load_dotenv(ROOT_DIR / 'config.env')
except ImportError:
    pass  # dotenv not available in Lambda

REGION = os.getenv("AWS_REGION", "us-east-1")

# Load gateway config (when run standalone)
# When imported by CFN Lambda, these are set by the caller
try:
    SCRIPT_DIR = Path(__file__).resolve().parent
    ROOT_DIR = SCRIPT_DIR.parent
    GATEWAY_ID = os.getenv('GATEWAY_ID') #
    # GATEWAY_ID already set from env
except FileNotFoundError:
    gateway_config = {}
    GATEWAY_ID = ''

# Interceptor Lambda code - loaded from separate file
INTERCEPTOR_LAMBDA_CODE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'interceptor_lambda.py')).read()


def deploy_interceptor_lambda():
    """Deploy the interceptor Lambda function"""
    lambda_client = boto3.client('lambda', region_name=REGION)
    iam = boto3.client('iam', region_name=REGION)
    
    function_name = 'gateway-auth-interceptor'
    role_name = 'GatewayInterceptorRole'
    
    print("Deploying Interceptor Lambda...")
    
    # 1. Create IAM role
    try:
        role = iam.get_role(RoleName=role_name)
        role_arn = role['Role']['Arn']
        print(f"  Using existing role: {role_name}")
    except iam.exceptions.NoSuchEntityException:
        assume_policy = json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        })
        role = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=assume_policy)
        iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn='arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
        )
        role_arn = role['Role']['Arn']
        print(f"  Created IAM role, waiting for propagation...")
        time.sleep(10)
    
    # 2. Create Lambda zip
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('lambda_function.py', INTERCEPTOR_LAMBDA_CODE)
    zip_buffer.seek(0)
    zip_bytes = zip_buffer.read()
    
    # 3. Create or update Lambda
    try:
        lambda_client.get_function(FunctionName=function_name)
        lambda_client.update_function_code(FunctionName=function_name, ZipFile=zip_bytes)
        response = lambda_client.get_function(FunctionName=function_name)
        lambda_arn = response['Configuration']['FunctionArn']
        print(f"  Updated Lambda: {function_name}")
    except lambda_client.exceptions.ResourceNotFoundException:
        response = lambda_client.create_function(
            FunctionName=function_name,
            Runtime='python3.11',
            Role=role_arn,
            Handler='lambda_function.lambda_handler',
            Code={'ZipFile': zip_bytes},
            Timeout=30
        )
        lambda_arn = response['FunctionArn']
        print(f"  Created Lambda: {function_name}")
    
    # 4. Add permission for Gateway to invoke
    try:
        lambda_client.add_permission(
            FunctionName=function_name,
            StatementId='GatewayInvoke',
            Action='lambda:InvokeFunction',
            Principal='bedrock-agentcore.amazonaws.com'
        )
    except lambda_client.exceptions.ResourceConflictException:
        pass
    
    print(f"[OK] Lambda deployed: {lambda_arn}")
    return lambda_arn


def update_gateway_with_interceptor(lambda_arn):
    """Update Gateway to use the interceptor"""
    client = boto3.client('bedrock-agentcore-control', region_name=REGION)
    
    print("\nUpdating Gateway with interceptor...")
    
    # Get current gateway config
    gateway = client.get_gateway(gatewayIdentifier=GATEWAY_ID)
    
    # Update gateway with interceptor
    update_params = {
        'gatewayIdentifier': GATEWAY_ID,
        'name': gateway['name'],
        'roleArn': gateway['roleArn'],
        'protocolType': gateway['protocolType'],
        'authorizerType': gateway['authorizerType'],
        'authorizerConfiguration': gateway.get('authorizerConfiguration', {}),
        'interceptorConfigurations': [{
            'interceptor': {
                'lambda': {
                    'arn': lambda_arn
                }
            },
            'interceptionPoints': ['REQUEST'],
            'inputConfiguration': {
                'passRequestHeaders': True
            }
        }]
    }
    
    # Preserve policy engine config if exists
    if 'policyEngineConfiguration' in gateway and gateway['policyEngineConfiguration']:
        update_params['policyEngineConfiguration'] = gateway['policyEngineConfiguration']
    
    client.update_gateway(**update_params)
    
    # Wait for gateway to be ready
    print("  Waiting for Gateway to be ready...")
    while True:
        status = client.get_gateway(gatewayIdentifier=GATEWAY_ID)
        if status['status'] == 'READY':
            break
        time.sleep(3)
    
    print("[OK] Gateway updated with interceptor")


def main():
    print("=" * 60)
    print("Gateway Interceptor Deployment")
    print("=" * 60)
    print(f"\nGateway: {GATEWAY_ID}")
    print(f"Region: {REGION}\n")
    
    # Deploy Lambda
    lambda_arn = deploy_interceptor_lambda()
    
    # Update Gateway
    update_gateway_with_interceptor(lambda_arn)
    
    # Save config
    SCRIPT_DIR = Path(__file__).resolve().parent
    ROOT_DIR = SCRIPT_DIR.parent
    
    print("\n" + "=" * 60)
    print("[OK] Deployment Complete!")
    print("=" * 60)
    print(f"""
Configuration:
   Interceptor Lambda: gateway-auth-interceptor
   Interception Points: REQUEST
   Pass Headers: True

The interceptor will now propagate the Authorization header
from incoming requests to all Lambda targets, including
Analytics.

The Lambda can access the token via:
   event.get('headers', {{}}).get('Authorization')
""")


if __name__ == "__main__":
    main()
