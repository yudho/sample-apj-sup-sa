#!/usr/bin/env python3
"""Deploy text-to-SQL Lambda and register to existing Gateway"""

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
LAMBDA_NAME = "custom-sql-toolset-lambda"
AURORA_ENDPOINT = os.getenv("AURORA_ENDPOINT")
AURORA_SECRET_ARN = os.getenv("AURORA_SECRET_ARN")
# Prefer app_user secret (non-owner role, RLS enforced) over postgres secret
APP_AURORA_SECRET_ARN = os.getenv("APP_AURORA_SECRET_ARN") or AURORA_SECRET_ARN
SECURITY_GROUP_ID = os.getenv("SECURITY_GROUP_ID")
SUBNET_IDS = os.getenv("SUBNET_IDS", "").split(",")
ROOT_DIR = SCRIPT_DIR.parent
# Gateway config from config.env
pass

GATEWAY_ID = os.getenv('GATEWAY_ID')

def create_lambda_zip():
    with zipfile.ZipFile('custom_sql_toolset_lambda.zip', 'w', zipfile.ZIP_DEFLATED) as zipf:
        zipf.write(str(ROOT_DIR / 'tools' / 'custom_sql_toolset_lambda.py'), 'custom_sql_toolset_lambda.py')
    print(f"[OK] Created ZIP")
    return 'custom_sql_toolset_lambda.zip'

def get_or_create_role():
    iam = boto3.client('iam', region_name=REGION)
    role_name = f'{LAMBDA_NAME}-role'
    
    assume_policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "arn:aws:logs:*:*:*"},
        {"Effect": "Allow", "Action": ["glue:GetTables", "glue:GetTable", "glue:GetDatabase"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["bedrock:Retrieve", "bedrock:InvokeModel"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": [AURORA_SECRET_ARN, APP_AURORA_SECRET_ARN]},
        {"Effect": "Allow", "Action": ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface", "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"], "Resource": "*"},
    ]}
    
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

def deploy_lambda(role_arn, zip_path):
    client = boto3.client('lambda', region_name=REGION)
    layer_arn = get_psycopg2_layer()
    
    with open(zip_path, 'rb') as f:
        zip_content = f.read()
    
    env = {'Variables': {
        'GLUE_DATABASE': os.getenv('GLUE_DATABASE', 'timely_unicorn'),
        'BEDROCK_KB_ID': os.getenv('KNOWLEDGE_BASE_ID', ''),
        'AURORA_ENDPOINT': AURORA_ENDPOINT,
        'AURORA_SECRET_ARN': APP_AURORA_SECRET_ARN,
        'AURORA_DATABASE': 'timely_unicorn',
    }}
    vpc = {'SubnetIds': SUBNET_IDS, 'SecurityGroupIds': [SECURITY_GROUP_ID]}
    
    try:
        client.create_function(FunctionName=LAMBDA_NAME, Runtime='python3.12', Role=role_arn,
            Handler='custom_sql_toolset_lambda.lambda_handler', Code={'ZipFile': zip_content},
            Timeout=60, MemorySize=256, Environment=env, VpcConfig=vpc, Layers=[layer_arn])
        print(f"[OK] Created Lambda: {LAMBDA_NAME}")
        client.get_waiter('function_active').wait(FunctionName=LAMBDA_NAME)
    except client.exceptions.ResourceConflictException:
        client.update_function_code(FunctionName=LAMBDA_NAME, ZipFile=zip_content)
        client.get_waiter('function_updated').wait(FunctionName=LAMBDA_NAME)
        client.update_function_configuration(FunctionName=LAMBDA_NAME, Timeout=60, Environment=env, VpcConfig=vpc, Layers=[layer_arn])
        print(f"[OK] Updated Lambda: {LAMBDA_NAME}")
        client.get_waiter('function_updated').wait(FunctionName=LAMBDA_NAME)
    
    return client.get_function(FunctionName=LAMBDA_NAME)['Configuration']['FunctionArn']

def add_gateway_permission(lambda_arn):
    client = boto3.client('lambda', region_name=REGION)
    try:
        client.add_permission(FunctionName=LAMBDA_NAME, StatementId='AllowBedrockAgentCore',
            Action='lambda:InvokeFunction', Principal='bedrock-agentcore.amazonaws.com')
        print("[OK] Added gateway permission")
    except client.exceptions.ResourceConflictException:
        print("[OK] Permission exists")

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
    print("\nRegistering to Gateway...")
    client = boto3.client('bedrock-agentcore-control', region_name=REGION)
    
    tool_schema = [
        {
            "name": "text_to_sql_tool",
            "description": "Convert natural language question to SQL context. Use when user asks analytics questions not covered by existing tools. Returns schema, business context, and a suggested SQL query for user approval.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "Natural language analytics question"},
                },
                "required": ["question"]
            }
        },
        {
            "name": "get_schema_context_tool",
            "description": "Get database table schemas from Glue catalog. Use to understand available tables and columns.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tables": {"type": "array", "items": {"type": "string"}, "description": "List of table names (empty for all)"}
                }
            }
        },
        {
            "name": "execute_sql_tool",
            "description": "Execute an approved SQL query against the database. Only use after user has approved the SQL query. Only SELECT queries are allowed.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "The SQL query to execute (must be SELECT only)"},
                },
                "required": ["sql"]
            }
        }
    ]
    
    # Delete existing target first
    _delete_existing_target(client, GATEWAY_ID, 'CustomSQL')
    
    response = client.create_gateway_target(
        gatewayIdentifier=GATEWAY_ID, name="CustomSQL",
        targetConfiguration={'mcp': {'lambda': {'lambdaArn': lambda_arn, 'toolSchema': {'inlinePayload': tool_schema}}}},
        credentialProviderConfigurations=[{'credentialProviderType': 'GATEWAY_IAM_ROLE'}]
    )
    print(f"[OK] Created target: {response['targetId']}")
    return response['targetId']

def test_lambda(lambda_arn):
    print("\nTesting Lambda...")
    client = boto3.client('lambda', region_name=REGION)
    
    resp = client.invoke(FunctionName=LAMBDA_NAME, Payload=json.dumps({
        'name': 'text_to_sql_tool',
        'arguments': {'question': 'What is the total revenue by customer?'}
    }))
    
    result = json.loads(resp['Payload'].read())
    if result.get('statusCode') == 200:
        body = json.loads(result['body'])
        print(f"[OK] Test passed! Tables: {body['tables_count']}, RAG sources: {body['rag_sources']}")
        return True
    print(f"❌ Test failed: {result}")
    return False

def main():
    print("Deploying text-to-SQL Lambda")
    print("=" * 50)
    
    zip_path = create_lambda_zip()
    role_arn = get_or_create_role()
    lambda_arn = deploy_lambda(role_arn, zip_path)
    add_gateway_permission(lambda_arn)
    
    target_id = register_gateway_target(lambda_arn)
    
    print("\n" + "=" * 50)
    print("[OK] Deployment complete!")
    print(f"   Lambda: {lambda_arn}")
    print(f"   Gateway Target: {target_id}")

if __name__ == '__main__':
    main()
