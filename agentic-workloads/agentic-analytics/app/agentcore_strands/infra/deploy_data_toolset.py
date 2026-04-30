#!/usr/bin/env python3
"""
Deploy AgenticAnalytics Lambda function and create AgentCore Gateway
Using the official AgentCore Starter Toolkit
Based on: https://aws.github.io/bedrock-agentcore-starter-toolkit/user-guide/gateway/quickstart.html

Updated to use Aurora PostgreSQL instead of DynamoDB
"""

import boto3
import json
import time
import zipfile
import os
from bedrock_agentcore_starter_toolkit.operations.gateway.client import GatewayClient
from dotenv import load_dotenv
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
env_path = ROOT_DIR / 'config.env'
load_dotenv(dotenv_path=env_path, override=True)

# Configuration
REGION = os.getenv("AWS_REGION", "us-east-1")
LAMBDA_FUNCTION_NAME = os.getenv("LAMBDA_FUNCTION_NAME", "prebaked_sql_toolset_lambda")

# Tool schema for DataTools target (27 analytics tools)
TOOL_SCHEMA = [
                        {"name": "check_db_status_tool", "description": "Check Aurora PostgreSQL database status", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "list_tables_tool", "description": "List all database tables", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "get_accounts_tool", "description": "Get unicorn rental business accounts", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "get_unicorns_tool", "description": "Get unicorns with optional filters", "inputSchema": {"type": "object", "properties": {"available_only": {"type": "boolean"}}}},
                        {"name": "get_customers_tool", "description": "Get customers", "inputSchema": {"type": "object", "properties": {"customer_type": {"type": "string"}, "limit": {"type": "integer"}}}},
                        {"name": "get_bookings_tool", "description": "Get bookings with date range", "inputSchema": {"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}, "limit": {"type": "integer"}}}},
                        {"name": "get_transactions_tool", "description": "Get transactions", "inputSchema": {"type": "object", "properties": {"transaction_type": {"type": "string"}, "limit": {"type": "integer"}}}},
                        {"name": "get_unicorn_availability_tool", "description": "Get unicorn availability history", "inputSchema": {"type": "object", "properties": {"unicorn_id": {"type": "string"}}, "required": ["unicorn_id"]}},
                        {"name": "get_users_tool", "description": "Get system users", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "get_subscription_plans_tool", "description": "Get subscription plans", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "search_unicorns_tool", "description": "Search unicorns by name/breed/color", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
                        {"name": "search_customers_tool", "description": "Search customers", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
                        {"name": "get_booking_summary_tool", "description": "Get booking analytics summary", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "get_revenue_summary_tool", "description": "Get monthly revenue summary", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "get_daily_bookings_summary_tool", "description": "Get daily bookings with customer/unicorn details", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
                        {"name": "get_monthly_revenue_summary_tool", "description": "Get monthly revenue breakdown", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "get_current_unicorn_availability_tool", "description": "Get real-time unicorn availability status", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "get_calendar_bookings_tool", "description": "Get calendar-friendly booking view", "inputSchema": {"type": "object", "properties": {"start_date": {"type": "string"}, "end_date": {"type": "string"}}}},
                        {"name": "get_customer_retention_metrics_tool", "description": "Get customer retention segments", "inputSchema": {"type": "object", "properties": {"segment": {"type": "string"}}}},
                        {"name": "get_top_revenue_breeds_tool", "description": "Get top unicorn breeds by revenue", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "get_top_revenue_customers_tool", "description": "Get top customers by revenue", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
                        {"name": "get_revenue_by_time_and_day_tool", "description": "Get revenue patterns by hour/day", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "get_unicorns_due_maintenance_tool", "description": "Get unicorns due for maintenance", "inputSchema": {"type": "object", "properties": {"urgency": {"type": "string"}}}},
                        {"name": "get_account_subscription_status_tool", "description": "Get account subscription status", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "get_my_account_info_tool", "description": "Get the current tenant's account details (name, plan, storage, user count). No arguments needed.", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "get_customer_lifetime_value_tool", "description": "Get customer lifetime value metrics", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
                        {"name": "get_seasonal_trends_tool", "description": "Get seasonal booking trends", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "get_customer_segmentation_tool", "description": "Get customer segmentation by revenue", "inputSchema": {"type": "object", "properties": {}}},
                        {"name": "semantic_search_tool", "description": "Search database metadata using natural language", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}}, "required": ["query"]}},
]

# Aurora PostgreSQL Configuration
AURORA_ENDPOINT = os.getenv("AURORA_ENDPOINT")
AURORA_SECRET_ARN = os.getenv("AURORA_SECRET_ARN")
# Prefer app_user secret (non-owner role, RLS enforced) over postgres secret
APP_AURORA_SECRET_ARN = os.getenv("APP_AURORA_SECRET_ARN") or AURORA_SECRET_ARN
AURORA_DATABASE = os.getenv("AURORA_DATABASE", "timely_unicorn")
AURORA_USERNAME = os.getenv("AURORA_USERNAME", "postgres")
VPC_ID = os.getenv("VPC_ID")
SECURITY_GROUP_ID = os.getenv("SECURITY_GROUP_ID")
SUBNET_IDS = os.getenv("SUBNET_IDS", "").split(",")

def create_lambda_zip():
    """Create ZIP file for Lambda deployment"""
    TOOLS_DIR = ROOT_DIR / 'tools'
    zip_path = 'prebaked_sql_toolset_lambda.zip'
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        zipf.write(str(TOOLS_DIR / 'prebaked_sql_toolset_lambda.py'), 'prebaked_sql_toolset_lambda.py')
    
    print(f"[OK] Created Lambda ZIP: {zip_path}")
    return zip_path

def create_psycopg2_layer():
    """Create or get psycopg2 Lambda layer for PostgreSQL connectivity"""
    lambda_client = boto3.client('lambda', region_name=REGION)
    layer_name = 'psycopg2-py312'
    
    # Check if layer already exists
    try:
        response = lambda_client.list_layer_versions(LayerName=layer_name, MaxItems=1)
        if response.get('LayerVersions'):
            layer_arn = response['LayerVersions'][0]['LayerVersionArn']
            print(f"[OK] Using existing psycopg2 layer: {layer_arn}")
            return layer_arn
    except:
        pass
    
    # Download and create layer
    print("Creating psycopg2 Lambda layer...")
    import subprocess
    import tempfile
    
    with tempfile.TemporaryDirectory() as tmpdir:
        layer_dir = os.path.join(tmpdir, 'python')
        os.makedirs(layer_dir)
        
        # Download psycopg2-binary for Linux
        subprocess.run([
            'pip3', 'download', 'psycopg2-binary',
            '--platform', 'manylinux2014_x86_64',
            '--only-binary=:all:',
            '--python-version', '312',
            '-d', os.path.join(tmpdir, 'downloads')
        ], capture_output=True)
        
        # Extract wheel
        import glob
        wheel_files = glob.glob(os.path.join(tmpdir, 'downloads', '*.whl'))
        if wheel_files:
            subprocess.run(['unzip', '-q', wheel_files[0], '-d', layer_dir])
        
        # Create zip
        layer_zip = os.path.join(tmpdir, 'psycopg2_layer.zip')
        subprocess.run(['zip', '-r', layer_zip, 'python'], cwd=tmpdir, capture_output=True)
        
        # Publish layer
        with open(layer_zip, 'rb') as f:
            response = lambda_client.publish_layer_version(
                LayerName=layer_name,
                Description='psycopg2-binary for Python 3.12',
                Content={'ZipFile': f.read()},
                CompatibleRuntimes=['python3.12']
            )
        
        layer_arn = response['LayerVersionArn']
        print(f"[OK] Created psycopg2 layer: {layer_arn}")
        return layer_arn

def create_lambda_role():
    """Create IAM role for Lambda function with Aurora and Secrets Manager access"""
    iam = boto3.client('iam', region_name=REGION)
    
    role_name = f'{LAMBDA_FUNCTION_NAME}-role'
    
    assume_role_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }
        ]
    }
    
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents"
                ],
                "Resource": "arn:aws:logs:*:*:*"
            },
            {
                "Effect": "Allow",
                "Action": [
                    "secretsmanager:GetSecretValue"
                ],
                "Resource": [AURORA_SECRET_ARN, APP_AURORA_SECRET_ARN]
            },
            {
                "Effect": "Allow",
                "Action": [
                    "ec2:CreateNetworkInterface",
                    "ec2:DescribeNetworkInterfaces",
                    "ec2:DeleteNetworkInterface",
                    "ec2:AssignPrivateIpAddresses",
                    "ec2:UnassignPrivateIpAddresses"
                ],
                "Resource": "*"
            }
        ]
    }
    
    try:
        role_response = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_role_policy),
            Description='IAM role for AgenticAnalytics MCP Lambda function (Aurora PostgreSQL)'
        )
        
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName='AgenticAnalyticsLambdaPolicy',
            PolicyDocument=json.dumps(policy_document)
        )
        
        # Attach VPC execution role
        iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn='arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole'
        )
        
        print(f"[OK] Created IAM role: {role_name}")
        return role_response['Role']['Arn']
        
    except iam.exceptions.EntityAlreadyExistsException:
        # Update existing role policies
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName='AgenticAnalyticsLambdaPolicy',
            PolicyDocument=json.dumps(policy_document)
        )
        try:
            iam.attach_role_policy(
                RoleName=role_name,
                PolicyArn='arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole'
            )
        except:
            pass
        
        role_response = iam.get_role(RoleName=role_name)
        print(f"[OK] Using existing IAM role: {role_name}")
        return role_response['Role']['Arn']

def ensure_vpc_endpoint():
    """Ensure Secrets Manager VPC endpoint exists"""
    ec2 = boto3.client('ec2', region_name=REGION)
    
    # Check for existing endpoint
    response = ec2.describe_vpc_endpoints(
        Filters=[
            {'Name': 'vpc-id', 'Values': [VPC_ID]},
            {'Name': 'service-name', 'Values': [f'com.amazonaws.{REGION}.secretsmanager']}
        ]
    )
    
    if response['VpcEndpoints']:
        print(f"[OK] Secrets Manager VPC endpoint exists")
        return response['VpcEndpoints'][0]['VpcEndpointId']
    
    # Create endpoint
    print("Creating Secrets Manager VPC endpoint...")
    response = ec2.create_vpc_endpoint(
        VpcId=VPC_ID,
        ServiceName=f'com.amazonaws.{REGION}.secretsmanager',
        VpcEndpointType='Interface',
        SubnetIds=SUBNET_IDS[:2],
        SecurityGroupIds=[SECURITY_GROUP_ID],
        PrivateDnsEnabled=True
    )
    
    endpoint_id = response['VpcEndpoint']['VpcEndpointId']
    print(f"[OK] Created VPC endpoint: {endpoint_id}")
    
    # Wait for endpoint to be available
    print("[WAIT] Waiting for VPC endpoint to be available...")
    waiter = ec2.get_waiter('vpc_endpoint_available')
    waiter.wait(VpcEndpointIds=[endpoint_id])
    
    return endpoint_id

def ensure_security_group_rules():
    """Ensure security group allows PostgreSQL and HTTPS traffic"""
    ec2 = boto3.client('ec2', region_name=REGION)
    
    rules_to_add = [
        {'port': 5432, 'description': 'PostgreSQL'},
        {'port': 443, 'description': 'HTTPS for Secrets Manager'}
    ]
    
    for rule in rules_to_add:
        try:
            ec2.authorize_security_group_ingress(
                GroupId=SECURITY_GROUP_ID,
                IpPermissions=[{
                    'IpProtocol': 'tcp',
                    'FromPort': rule['port'],
                    'ToPort': rule['port'],
                    'UserIdGroupPairs': [{'GroupId': SECURITY_GROUP_ID}]
                }]
            )
            print(f"[OK] Added security group rule for {rule['description']} (port {rule['port']})")
        except ec2.exceptions.ClientError as e:
            if 'Duplicate' in str(e):
                print(f"[OK] Security group rule for {rule['description']} already exists")
            else:
                raise

def deploy_lambda_function(role_arn, zip_path, layer_arn):
    """Deploy Lambda function with VPC configuration for Aurora access"""
    lambda_client = boto3.client('lambda', region_name=REGION)
    
    with open(zip_path, 'rb') as zip_file:
        zip_content = zip_file.read()
    
    vpc_config = {
        'SubnetIds': SUBNET_IDS,
        'SecurityGroupIds': [SECURITY_GROUP_ID]
    }
    
    environment = {
        'Variables': {
            'AURORA_ENDPOINT': AURORA_ENDPOINT,
            'AURORA_SECRET_ARN': APP_AURORA_SECRET_ARN,
            'AURORA_DATABASE': 'timely_unicorn',
            'AURORA_USERNAME': 'postgres'
        }
    }
    
    try:
        response = lambda_client.create_function(
            FunctionName=LAMBDA_FUNCTION_NAME,
            Runtime='python3.12',
            Role=role_arn,
            Handler='prebaked_sql_toolset_lambda.lambda_handler',
            Code={'ZipFile': zip_content},
            Description='AgenticAnalytics MCP tools Lambda function (Aurora PostgreSQL)',
            Timeout=120,
            MemorySize=256,
            VpcConfig=vpc_config,
            Environment=environment,
            Layers=[layer_arn]
        )
        
        print(f"[OK] Created Lambda function: {LAMBDA_FUNCTION_NAME}")
        
        # Wait for function to be active
        print("[WAIT] Waiting for Lambda function to be active...")
        waiter = lambda_client.get_waiter('function_active')
        waiter.wait(FunctionName=LAMBDA_FUNCTION_NAME)
        
        return response['FunctionArn']
        
    except lambda_client.exceptions.ResourceConflictException:
        # Update existing function
        lambda_client.update_function_code(
            FunctionName=LAMBDA_FUNCTION_NAME,
            ZipFile=zip_content
        )
        
        # Wait for update to complete
        time.sleep(5)
        
        lambda_client.update_function_configuration(
            FunctionName=LAMBDA_FUNCTION_NAME,
            Timeout=120,
            VpcConfig=vpc_config,
            Environment=environment,
            Layers=[layer_arn]
        )
        
        response = lambda_client.get_function(FunctionName=LAMBDA_FUNCTION_NAME)
        print(f"[OK] Updated existing Lambda function: {LAMBDA_FUNCTION_NAME}")
        return response['Configuration']['FunctionArn']

def test_lambda_function():
    """Test the Lambda function connectivity to Aurora"""
    lambda_client = boto3.client('lambda', region_name=REGION)
    
    print("Testing Lambda function...")
    
    response = lambda_client.invoke(
        FunctionName=LAMBDA_FUNCTION_NAME,
        InvocationType='RequestResponse',
        Payload=json.dumps({'name': 'check_db_status_tool', 'arguments': {}})
    )
    
    result = json.loads(response['Payload'].read())
    
    if result.get('statusCode') == 200:
        body = json.loads(result['body'])
        if body.get('db_accessible'):
            print(f"[OK] Lambda successfully connected to Aurora PostgreSQL")
            print(f"  Tables found: {', '.join(body.get('existing_tables', []))}")
            return True
    
    print(f"❌ Lambda test failed: {result}")
    return False

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

def main():
    """Main deployment function — deploys DataTools Lambda and adds target to existing Gateway"""
    print("Deploying DataTools Lambda (27 analytics tools)")
    print("=" * 70)
    
    # Load existing gateway config
    GATEWAY_ID = os.getenv("GATEWAY_ID")
    if not GATEWAY_ID:
        print("GATEWAY_ID not found in config.env. Run deploy_gateway.py first.")
        sys.exit(1)
    print(f"Using Gateway: {GATEWAY_ID}")
    
    # Step 1: Create Lambda ZIP
    zip_path = create_lambda_zip()
    
    # Step 2: Create/get psycopg2 layer
    print("\nSetting up psycopg2 Lambda layer...")
    layer_arn = create_psycopg2_layer()
    
    # Step 3: Ensure VPC endpoint for Secrets Manager
    print("\nSetting up VPC connectivity...")
    ensure_vpc_endpoint()
    ensure_security_group_rules()
    
    # Step 4: Create Lambda role and function
    print("\nCreating Lambda function...")
    lambda_role_arn = create_lambda_role()
    time.sleep(10)
    lambda_arn = deploy_lambda_function(lambda_role_arn, zip_path, layer_arn)
    
    # Step 5: Add or update DataTools target on existing Gateway
    print("\nAdding DataTools target to Gateway...")
    boto_client = boto3.client("bedrock-agentcore-control", region_name=REGION)
    
    try:
        from bedrock_agentcore_starter_toolkit.operations.gateway.client import GatewayClient
    except ImportError:
        os.system(f"{sys.executable} -m pip install bedrock-agentcore-starter-toolkit -q")
        from bedrock_agentcore_starter_toolkit.operations.gateway.client import GatewayClient
    
    # Delete existing target if it exists
    _delete_existing_target(boto_client, GATEWAY_ID, 'PrebakedSQL')
    
    client = GatewayClient(region_name=REGION)
    gateway = {"gatewayId": GATEWAY_ID}
    
    lambda_target = client.create_mcp_gateway_target(
        gateway=gateway,
        name="PrebakedSQL",
        target_type="lambda",
        target_payload={
            "lambdaArn": lambda_arn,
            "toolSchema": {
                "inlinePayload": TOOL_SCHEMA
            }
        },
        credentials=None,
    )
    print(f"[OK] Created DataTools target: {lambda_target['targetId']}")
    
    
    os.remove(zip_path)

if __name__ == "__main__":
    main()
