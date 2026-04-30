#!/usr/bin/env python3
"""
Deploy create_booking Lambda and register to existing Gateway
"""

import boto3
import json
import time
import zipfile
import os
from pathlib import Path
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
load_dotenv(ROOT_DIR / 'config.env')

REGION = os.getenv("AWS_REGION", "us-east-1")
LAMBDA_NAME = "api-integration-toolset-lambda"
AURORA_ENDPOINT = os.getenv("AURORA_ENDPOINT")
AURORA_SECRET_ARN = os.getenv("AURORA_SECRET_ARN")
# Prefer app_user secret (non-owner role, RLS enforced) over postgres secret
APP_AURORA_SECRET_ARN = os.getenv("APP_AURORA_SECRET_ARN") or AURORA_SECRET_ARN
VPC_ID = os.getenv("VPC_ID")
SECURITY_GROUP_ID = os.getenv("SECURITY_GROUP_ID")
SUBNET_IDS = os.getenv("SUBNET_IDS", "").split(",")

# Load existing gateway config
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
# Gateway config from config.env
GATEWAY_ID_FROM_ENV = True

GATEWAY_ID = os.getenv('GATEWAY_ID')

def create_lambda_zip():
    zip_path = 'api_integration_toolset_lambda.zip'
    tools_dir = ROOT_DIR / 'tools'
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        zipf.write(str(tools_dir / 'api_integration_toolset_lambda.py'), 'api_integration_toolset_lambda.py')
    print(f"[OK] Created ZIP: {zip_path}")
    return zip_path

def get_or_create_role():
    iam = boto3.client('iam', region_name=REGION)
    role_name = f'{LAMBDA_NAME}-role'
    
    assume_policy = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]
    }
    
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "arn:aws:logs:*:*:*"},
            {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": [AURORA_SECRET_ARN, APP_AURORA_SECRET_ARN]},
            {"Effect": "Allow", "Action": ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"], "Resource": "*"}
        ]
    }
    
    try:
        iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps(assume_policy))
        iam.attach_role_policy(RoleName=role_name, PolicyArn='arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole')
        print(f"[OK] Created role: {role_name}")
        time.sleep(10)
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"[OK] Using existing role: {role_name}")
    
    iam.put_role_policy(RoleName=role_name, PolicyName='LambdaPolicy', PolicyDocument=json.dumps(policy))
    return iam.get_role(RoleName=role_name)['Role']['Arn']

def get_psycopg2_layer():
    lambda_client = boto3.client('lambda', region_name=REGION)
    response = lambda_client.list_layer_versions(LayerName='psycopg2-py312', MaxItems=1)
    if response.get('LayerVersions'):
        return response['LayerVersions'][0]['LayerVersionArn']
    raise Exception("psycopg2-py312 layer not found")

def deploy_lambda(role_arn, zip_path, layer_arn):
    lambda_client = boto3.client('lambda', region_name=REGION)
    
    with open(zip_path, 'rb') as f:
        zip_content = f.read()
    
    env = {'Variables': {'AURORA_ENDPOINT': AURORA_ENDPOINT, 'AURORA_SECRET_ARN': APP_AURORA_SECRET_ARN, 'AURORA_DATABASE': 'timely_unicorn'}}
    vpc = {'SubnetIds': SUBNET_IDS, 'SecurityGroupIds': [SECURITY_GROUP_ID]}
    
    try:
        response = lambda_client.create_function(
            FunctionName=LAMBDA_NAME, Runtime='python3.12', Role=role_arn,
            Handler='api_integration_toolset_lambda.lambda_handler', Code={'ZipFile': zip_content},
            Timeout=60, MemorySize=256, VpcConfig=vpc, Environment=env, Layers=[layer_arn]
        )
        print(f"[OK] Created Lambda: {LAMBDA_NAME}")
        waiter = lambda_client.get_waiter('function_active')
        waiter.wait(FunctionName=LAMBDA_NAME)
    except lambda_client.exceptions.ResourceConflictException:
        lambda_client.update_function_code(FunctionName=LAMBDA_NAME, ZipFile=zip_content)
        time.sleep(5)
        lambda_client.update_function_configuration(FunctionName=LAMBDA_NAME, Timeout=60, VpcConfig=vpc, Environment=env, Layers=[layer_arn])
        print(f"[OK] Updated Lambda: {LAMBDA_NAME}")
        waiter = lambda_client.get_waiter('function_updated')
        waiter.wait(FunctionName=LAMBDA_NAME)
    
    return lambda_client.get_function(FunctionName=LAMBDA_NAME)['Configuration']['FunctionArn']

def add_gateway_permission(lambda_arn):
    lambda_client = boto3.client('lambda', region_name=REGION)
    try:
        lambda_client.add_permission(
            FunctionName=LAMBDA_NAME, StatementId='AllowBedrockAgentCore', Action='lambda:InvokeFunction',
            Principal='bedrock-agentcore.amazonaws.com'
        )
        print("[OK] Added gateway invoke permission")
    except lambda_client.exceptions.ResourceConflictException:
        print("[OK] Gateway permission exists")

def _delete_existing_target(client, gateway_id, target_name):
    """Delete existing gateway target and wait for deletion to complete."""
    try:
        targets = client.list_gateway_targets(gatewayIdentifier=gateway_id).get('items', [])
        for t in targets:
            if t.get('name') == target_name:
                client.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=t['targetId'])
                print(f"[OK] Deleted existing {target_name} target: {t['targetId']}")
                for _ in range(30):
                    time.sleep(3)
                    remaining = client.list_gateway_targets(gatewayIdentifier=gateway_id).get('items', [])
                    if not any(r.get('name') == target_name for r in remaining):
                        break
                return
    except Exception as e:
        print(f"Note: {e}")

def register_gateway_target(lambda_arn):
    print("\nRegistering new target to Gateway...")
    client = boto3.client('bedrock-agentcore-control', region_name=REGION)
    
    tool_schema = [{
        "name": "create_booking_tool",
        "description": "Create a new unicorn booking. Validates unicorn availability and calculates cost based on hourly rate and duration.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "string", "description": "The customer ID making the booking"},
                "unicorn_id": {"type": "string", "description": "The unicorn ID to book"},
                "start_datetime": {"type": "string", "description": "Booking start time in ISO format (e.g., 2026-02-01T10:00:00)"},
                "end_datetime": {"type": "string", "description": "Booking end time in ISO format (e.g., 2026-02-01T14:00:00)"},
                "special_requests": {"type": "string", "description": "Optional special requests"},
                "pickup_location": {"type": "string", "description": "Optional pickup location"},
                "dropoff_location": {"type": "string", "description": "Optional dropoff location"}
            },
            "required": ["customer_id", "unicorn_id", "start_datetime", "end_datetime"]
        }
    }]
    
    # Delete existing target if it exists
    _delete_existing_target(client, GATEWAY_ID, 'APIInteg')

    response = client.create_gateway_target(
        gatewayIdentifier=GATEWAY_ID,
        name="APIInteg",
        targetConfiguration={
            'mcp': {
                'lambda': {
                    'lambdaArn': lambda_arn,
                    'toolSchema': {'inlinePayload': tool_schema}
                }
            }
        },
        credentialProviderConfigurations=[
            {'credentialProviderType': 'GATEWAY_IAM_ROLE'}
        ]
    )
    
    target_id = response['targetId']
    print(f"[OK] Created gateway target: {target_id}")
    return target_id

def test_lambda(lambda_arn):
    print("\nTesting Lambda...")
    lambda_client = boto3.client('lambda', region_name=REGION)
    
    # Get test data from existing Lambda
    existing_lambda = 'prebaked_sql_toolset_lambda'
    
    resp = lambda_client.invoke(FunctionName=existing_lambda, Payload=json.dumps({'name': 'get_accounts_tool', 'arguments': {}}))
    accounts = json.loads(json.loads(resp['Payload'].read())['body'])['accounts']
    account_id = str(accounts[0]['account_id'])
    
    resp = lambda_client.invoke(FunctionName=existing_lambda, Payload=json.dumps({'name': 'get_customers_tool', 'arguments': {'account_id': account_id, 'limit': 1}}))
    customer_id = str(json.loads(json.loads(resp['Payload'].read())['body'])['customers'][0]['customer_id'])
    
    resp = lambda_client.invoke(FunctionName=existing_lambda, Payload=json.dumps({'name': 'get_unicorns_tool', 'arguments': {'account_id': account_id, 'available_only': True}}))
    unicorn_id = str(json.loads(json.loads(resp['Payload'].read())['body'])['unicorns'][0]['unicorn_id'])
    
    # Test create_booking
    resp = lambda_client.invoke(FunctionName=LAMBDA_NAME, Payload=json.dumps({
        'name': 'create_booking_tool',
        'arguments': {
            'account_id': account_id, 'customer_id': customer_id, 'unicorn_id': unicorn_id,
            'start_datetime': '2026-02-01T10:00:00', 'end_datetime': '2026-02-01T14:00:00',
            'pickup_location': 'Test Location'
        }
    }))
    
    result = json.loads(resp['Payload'].read())
    if result.get('statusCode') == 200:
        body = json.loads(result['body'])
        print(f"[OK] Test passed! Booking: {body['booking']['booking_reference']}, Cost: ${body['booking']['total_cost']}")
        return True
    print(f"❌ Test failed: {result}")
    return False

def main():
    print("Deploying create_booking Lambda and registering to Gateway")
    print("=" * 60)
    
    zip_path = create_lambda_zip()
    role_arn = get_or_create_role()
    layer_arn = get_psycopg2_layer()
    lambda_arn = deploy_lambda(role_arn, zip_path, layer_arn)
    add_gateway_permission(lambda_arn)
    
    print("\n[WAIT] Waiting for Lambda VPC setup...")
    time.sleep(15)
    
    target_id = register_gateway_target(lambda_arn)
    
    print("\n" + "=" * 60)
    print("[OK] Deployment complete!")
    print(f"   Lambda: {lambda_arn}")
    print(f"   Gateway Target: {target_id}")

if __name__ == '__main__':
    main()
