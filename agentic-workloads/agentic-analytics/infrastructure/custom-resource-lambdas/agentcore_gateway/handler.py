"""
AgentCore Gateway Custom Resource Lambda
Deploys Lambda function and creates AgentCore MCP Gateway.

Can be invoked:
1. Via CloudFormation Custom Resource (automatic deployment)
2. Via direct Lambda invoke (workshop mode)

In 'full' deployment mode, also deploys:
- Policy Engine with Cedar policies (from deploy_policy.py)
- Gateway Interceptor for auth token propagation (from deploy_interceptor.py)

Note: This wraps the existing deploy_agentcore_gateway.py logic and calls
Vincent's deploy_policy.py and deploy_interceptor.py for RBAC features.
"""
import boto3
import json
import os
import time
import zipfile
import tempfile
import urllib.request

REGION = os.environ.get('AWS_REGION', 'us-west-2')

lambda_client = boto3.client('lambda', region_name=REGION)
iam_client = boto3.client('iam', region_name=REGION)
s3_client = boto3.client('s3', region_name=REGION)


def create_lambda_role(function_name, aurora_secret_arn):
    """Create IAM role for Lambda function."""
    role_name = f'{function_name}-role'
    
    assume_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    }
    
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": "arn:aws:logs:*:*:*"
            },
            {
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue"],
                "Resource": aurora_secret_arn
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
            },
            {
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock-agent-runtime:Retrieve"],
                "Resource": "*"
            }
        ]
    }
    
    try:
        response = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(assume_policy),
            Description='Role for AgenticAnalytics Lambda'
        )
        iam_client.attach_role_policy(
            RoleName=role_name,
            PolicyArn='arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole'
        )
        print(f"[OK] Created IAM role: {role_name}")
        time.sleep(10)
    except iam_client.exceptions.EntityAlreadyExistsException:
        response = iam_client.get_role(RoleName=role_name)
        print(f"[OK] Using existing IAM role: {role_name}")
    
    # Always update the policy to ensure correct permissions
    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName='LambdaPolicy',
        PolicyDocument=json.dumps(policy)
    )
    print(f"[OK] Updated IAM role policy with current config")
    time.sleep(15)  # Wait for policy propagation
    return response['Role']['Arn']


def get_psycopg2_layer():
    """Get or create psycopg2 Lambda layer."""
    layer_name = 'psycopg2-py312'
    
    try:
        response = lambda_client.list_layer_versions(LayerName=layer_name, MaxItems=1)
        if response.get('LayerVersions'):
            layer_arn = response['LayerVersions'][0]['LayerVersionArn']
            print(f"[OK] Using existing psycopg2 layer: {layer_arn}")
            return layer_arn
    except:
        pass
    
    raise Exception("psycopg2-py312 layer not found. Please create it first.")


def deploy_lambda(function_name, role_arn, bucket, code_key, vpc_config, env_vars, layer_arn):
    """Deploy Lambda function."""
    print(f"Deploying Lambda: {function_name}")
    
    # Download code from S3
    with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as tmp:
        s3_client.download_file(bucket, code_key, tmp.name)
        with open(tmp.name, 'rb') as f:
            zip_content = f.read()
    
    try:
        response = lambda_client.create_function(
            FunctionName=function_name,
            Runtime='python3.12',
            Role=role_arn,
            Handler='datafoundation_lambda.lambda_handler',
            Code={'ZipFile': zip_content},
            Description='AgenticAnalytics MCP tools Lambda',
            Timeout=120,
            MemorySize=256,
            VpcConfig=vpc_config,
            Environment={'Variables': env_vars},
            Layers=[layer_arn]
        )
        print(f"[OK] Created Lambda: {function_name}")
        
        waiter = lambda_client.get_waiter('function_active')
        waiter.wait(FunctionName=function_name)
        return response['FunctionArn']
        
    except lambda_client.exceptions.ResourceConflictException:
        lambda_client.update_function_code(FunctionName=function_name, ZipFile=zip_content)
        time.sleep(5)
        lambda_client.update_function_configuration(
            FunctionName=function_name,
            Timeout=120,
            VpcConfig=vpc_config,
            Environment={'Variables': env_vars},
            Layers=[layer_arn]
        )
        response = lambda_client.get_function(FunctionName=function_name)
        print(f"[OK] Updated Lambda: {function_name}")
        return response['Configuration']['FunctionArn']


def create_gateway(gateway_name, lambda_arn, existing_cognito=None):
    """Create AgentCore MCP Gateway using boto3 directly.
    
    Args:
        gateway_name: Name for the gateway
        lambda_arn: ARN of the Lambda function to register as target
        existing_cognito: Optional dict with existing Cognito config:
            - user_pool_id: Cognito User Pool ID
            - client_id: Cognito App Client ID
    """
    print(f"Creating AgentCore Gateway: {gateway_name}")
    
    client = boto3.client("bedrock-agentcore-control", region_name=REGION)
    
    # Check for existing gateway
    gateways = client.list_gateways()
    for gw in gateways.get('gateways', []):
        if gw.get('name') == gateway_name:
            print(f"[OK] Gateway already exists: {gw['gatewayId']}")
            return {
                'gateway_id': gw['gatewayId'],
                'gateway_url': gw.get('gatewayUrl', '')
            }
    
    # Build authorizer config from existing Cognito
    if not (existing_cognito and existing_cognito.get('user_pool_id') and existing_cognito.get('client_id')):
        raise Exception("Cognito User Pool ID and Client ID are required")
    
    discovery_url = f"https://cognito-idp.{REGION}.amazonaws.com/{existing_cognito['user_pool_id']}/.well-known/openid-configuration"
    authorizer_config = {
        'customJWTAuthorizer': {
            'discoveryUrl': discovery_url,
            'allowedClients': [existing_cognito['client_id']]
        }
    }
    
    # Create gateway
    gateway = client.create_gateway(
        name=gateway_name,
        protocolType='MCP',
        authorizerConfiguration=authorizer_config
    )
    gateway_id = gateway['gatewayId']
    print(f"[OK] Created Gateway: {gateway_id}")
    
    # Wait for gateway to be ready
    print("Waiting for gateway to be ready...")
    for _ in range(30):
        gw = client.get_gateway(gatewayIdentifier=gateway_id)
        if gw.get('status') == 'READY':
            break
        time.sleep(5)
    
    gateway_url = gw.get('gatewayUrl', '')
    
    # Create Lambda target with tool schema
    tool_schema = get_tool_schema()
    target = client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name="DataTools",
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
    print(f"[OK] Created Lambda target: {target['targetId']}")
    
    return {
        'gateway_id': gateway_id,
        'gateway_url': gateway_url,
        'target_id': target['targetId'],
        'cognito_user_pool_id': existing_cognito['user_pool_id'],
        'cognito_client_id': existing_cognito['client_id']
    }


def get_tool_schema():
    """Return tool schema for the Lambda target."""
    return [
        {"name": "check_db_status_tool", "description": "Check database status", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "get_accounts_tool", "description": "Get rental business accounts", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "get_unicorns_tool", "description": "Get unicorns", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "available_only": {"type": "boolean"}}}},
        {"name": "get_customers_tool", "description": "Get customers", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "limit": {"type": "integer"}}}},
        {"name": "get_bookings_tool", "description": "Get bookings", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "start_date": {"type": "string"}, "end_date": {"type": "string"}}}},
        {"name": "get_transactions_tool", "description": "Get transactions", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "limit": {"type": "integer"}}}},
        {"name": "semantic_search_tool", "description": "Search business context using natural language", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}}, "required": ["query"]}},
        {"name": "get_daily_bookings_summary_tool", "description": "Get daily bookings summary", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
        {"name": "get_monthly_revenue_summary_tool", "description": "Get monthly revenue", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "get_current_unicorn_availability_tool", "description": "Get unicorn availability", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}}}},
        {"name": "get_top_revenue_customers_tool", "description": "Get top customers by revenue", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
        {"name": "get_customer_segmentation_tool", "description": "Get customer segmentation", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}}}},
    ]


def deploy_policy_and_interceptor(gateway_config, create_test_users=False, artifacts_bucket=None):
    """
    Deploy Policy Engine and Interceptor using Vincent's code.
    
    Args:
        gateway_config: Dict with gateway_id, gateway_arn, cognito_user_pool_id
        create_test_users: Whether to create test users (skip in workshop mode)
        artifacts_bucket: S3 bucket containing users.csv (optional, for CSV-based user creation)
    
    Returns:
        Dict with policy_engine_id, interceptor_lambda_arn
    """
    print("Deploying Policy Engine and Interceptor...")
    
    # Import Vincent's modules (bundled in Lambda package)
    try:
        from deploy_policy import (
            create_policy_engine, create_policies, setup_cognito,
            deploy_pre_token_lambda, update_gateway_authorizer,
            create_test_users as create_users_hardcoded, attach_policy_engine,
            create_users_from_csv
        )
        from deploy_interceptor import (
            deploy_interceptor_lambda, update_gateway_with_interceptor
        )
    except ImportError as e:
        print(f"Warning: Could not import policy/interceptor modules: {e}")
        return {}
    
    # Set module-level variables that Vincent's code expects
    import deploy_policy
    import deploy_interceptor
    
    deploy_policy.REGION = REGION
    deploy_policy.GATEWAY_ID = gateway_config['gateway_id']
    deploy_policy.GATEWAY_ARN = gateway_config['gateway_arn']
    deploy_policy.USER_POOL_ID = gateway_config['cognito_user_pool_id']
    deploy_policy.gateway_config = gateway_config
    
    deploy_interceptor.REGION = REGION
    deploy_interceptor.GATEWAY_ID = gateway_config['gateway_id']
    deploy_interceptor.gateway_config = gateway_config
    
    result = {}
    
    try:
        # 1. Create Policy Engine
        policy_engine = create_policy_engine()
        result['policy_engine_id'] = policy_engine['policyEngineId']
        result['policy_engine_arn'] = policy_engine['policyEngineArn']
        
        # 2. Create Cedar Policies
        create_policies(policy_engine['policyEngineId'])
        
        # 3. Configure Cognito (custom:role attribute, user login client)
        setup_cognito()
        
        # 4. Deploy Pre-Token Generation Lambda
        deploy_pre_token_lambda()
        
        # 5. Update Gateway authorizer
        update_gateway_authorizer()
        
        # 6. Create users (from CSV if bucket provided, else hardcoded)
        if create_test_users:
            if artifacts_bucket:
                create_users_from_csv(artifacts_bucket)
            else:
                create_users_hardcoded()
        
        # 7. Attach Policy Engine to Gateway (LOG_ONLY mode)
        attach_policy_engine(policy_engine['policyEngineArn'], mode="LOG_ONLY")
        
        # 8. Deploy Interceptor Lambda
        interceptor_arn = deploy_interceptor_lambda()
        result['interceptor_lambda_arn'] = interceptor_arn
        
        # 9. Update Gateway with Interceptor
        update_gateway_with_interceptor(interceptor_arn)
        
        print("[OK] Policy Engine and Interceptor deployed successfully")
        
    except Exception as e:
        print(f"Warning: Policy/Interceptor deployment failed: {e}")
        # Non-fatal - gateway still works without RBAC
    
    return result


def send_cfn_response(event, context, status, data=None, reason=None):
    """Send response to CloudFormation."""
    response_body = {
        'Status': status,
        'Reason': reason or f'See CloudWatch Log Stream: {context.log_stream_name}',
        'PhysicalResourceId': event.get('PhysicalResourceId', context.log_stream_name),
        'StackId': event['StackId'],
        'RequestId': event['RequestId'],
        'LogicalResourceId': event['LogicalResourceId'],
        'Data': data or {}
    }
    
    req = urllib.request.Request(
        event['ResponseURL'],
        data=json.dumps(response_body).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='PUT'
    )
    urllib.request.urlopen(req)


def lambda_handler(event, context):
    """Lambda handler for both CFN Custom Resource and direct invocation."""
    print(f"Event: {json.dumps(event)}")
    
    is_cfn = 'RequestType' in event and 'ResponseURL' in event
    
    try:
        if is_cfn:
            request_type = event['RequestType']
            props = event['ResourceProperties']
            
            if request_type == 'Delete':
                # TODO: Implement gateway deletion
                send_cfn_response(event, context, 'SUCCESS')
                return {'status': 'success', 'message': 'Delete - manual cleanup required'}
            
            function_name = props.get('LambdaFunctionName', 'datafoundation_lambda')
            gateway_name = props.get('GatewayName', 'AgenticAnalyticsMCPGateway')
            aurora_secret_arn = props['DatabaseSecretArn']
            aurora_endpoint = props['AuroraEndpoint']
            bucket = props['ArtifactsBucket']
            code_key = props.get('LambdaCodeKey', 'lambda/datafoundation_lambda.zip')
            subnet_ids = props['SubnetIds'].split(',')
            security_group_id = props['SecurityGroupId']
            kb_id = props.get('KnowledgeBaseId', '')
            deployment_mode = props.get('DeploymentMode', 'full')
            cognito_user_pool_id = props.get('CognitoUserPoolId', '')
            cognito_client_id = props.get('CognitoClientId', '')
        else:
            function_name = event.get('LambdaFunctionName', 'datafoundation_lambda')
            gateway_name = event.get('GatewayName', 'AgenticAnalyticsMCPGateway')
            aurora_secret_arn = event.get('DatabaseSecretArn') or os.environ.get('DATABASE_SECRET_ARN')
            aurora_endpoint = event.get('AuroraEndpoint') or os.environ.get('AURORA_ENDPOINT')
            bucket = event.get('ArtifactsBucket') or os.environ.get('ARTIFACTS_BUCKET')
            code_key = event.get('LambdaCodeKey', 'lambda/datafoundation_lambda.zip')
            subnet_ids = event.get('SubnetIds', os.environ.get('SUBNET_IDS', '')).split(',')
            security_group_id = event.get('SecurityGroupId') or os.environ.get('SECURITY_GROUP_ID')
            kb_id = event.get('KnowledgeBaseId', '')
            deployment_mode = event.get('DeploymentMode', 'full')
            cognito_user_pool_id = event.get('CognitoUserPoolId', '')
            cognito_client_id = event.get('CognitoClientId', '')
        
        # Build existing_cognito config if provided
        existing_cognito = None
        if cognito_user_pool_id and cognito_client_id:
            existing_cognito = {
                'user_pool_id': cognito_user_pool_id,
                'client_id': cognito_client_id
            }
            print(f"Workshop mode: Using existing Cognito User Pool {cognito_user_pool_id}")
        
        # Create Lambda role
        role_arn = create_lambda_role(function_name, aurora_secret_arn)
        
        # Get psycopg2 layer
        layer_arn = get_psycopg2_layer()
        
        # VPC config
        vpc_config = {
            'SubnetIds': subnet_ids,
            'SecurityGroupIds': [security_group_id]
        }
        
        # Environment variables
        env_vars = {
            'AURORA_ENDPOINT': aurora_endpoint,
            'AURORA_SECRET_ARN': aurora_secret_arn,
            'AURORA_DATABASE': 'timely_unicorn',
            'KNOWLEDGE_BASE_ID': kb_id
        }
        
        # Deploy Lambda
        lambda_arn = deploy_lambda(function_name, role_arn, bucket, code_key, vpc_config, env_vars, layer_arn)
        
        # Create Gateway
        gateway_result = create_gateway(gateway_name, lambda_arn, existing_cognito=existing_cognito)
        
        result = {
            'LambdaArn': lambda_arn,
            'GatewayId': gateway_result.get('gateway_id', '') if gateway_result else '',
            'GatewayUrl': gateway_result.get('gateway_url', '') if gateway_result else ''
        }
        
        # Deploy Policy Engine and Interceptor in 'full' mode
        if deployment_mode == 'full' and gateway_result:
            print(f"Deployment mode: {deployment_mode} - deploying Policy Engine and Interceptor")
            
            # Build gateway_config for Vincent's code
            gateway_config = {
                'gateway_id': gateway_result.get('gateway_id', ''),
                'gateway_arn': f"arn:aws:bedrock-agentcore:{REGION}:{boto3.client('sts').get_caller_identity()['Account']}:gateway/{gateway_result.get('gateway_id', '')}",
                'cognito_user_pool_id': gateway_result.get('cognito_user_pool_id', ''),
                'cognito_client_id': gateway_result.get('cognito_client_id', ''),
            }
            
            policy_result = deploy_policy_and_interceptor(
                gateway_config,
                create_test_users=True,
                artifacts_bucket=bucket  # Pass bucket for CSV-based user creation
            )
            
            result.update({
                'PolicyEngineId': policy_result.get('policy_engine_id', ''),
                'InterceptorLambdaArn': policy_result.get('interceptor_lambda_arn', '')
            })
        else:
            print(f"Deployment mode: {deployment_mode} - skipping Policy Engine and Interceptor")
        
        if is_cfn:
            send_cfn_response(event, context, 'SUCCESS', result)
        
        return {'status': 'success', **result}
        
    except Exception as e:
        print(f"Error: {str(e)}")
        if is_cfn:
            send_cfn_response(event, context, 'FAILED', reason=str(e))
        raise
