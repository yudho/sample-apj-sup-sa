"""
Amplify Hosting Custom Resource Lambda
Deploys React UI to AWS Amplify Hosting.

Uses shared utilities from common/amplify_utils.py (packaged together)
"""
import json
import os
import urllib.request

from common.amplify_utils import (
    create_amplify_app,
    create_branch,
    deploy_from_s3,
    wait_for_deployment,
    delete_amplify_app,
    update_app_env_vars,
)

REGION = os.environ.get('AWS_REGION', 'us-west-2')


def send_cfn_response(event, context, status, data=None, reason=None):
    """Send response to CloudFormation."""
    response_body = {
        'Status': status,
        'Reason': reason or f'See CloudWatch Log Stream: {context.log_stream_name}',
        'PhysicalResourceId': data.get('AppId', context.log_stream_name) if data else context.log_stream_name,
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
    """
    CloudFormation Custom Resource handler.
    
    Properties:
        AppName: Amplify app name
        BranchName: Branch name (default: main)
        ArtifactsBucket: S3 bucket with UI build ZIP
        ArtifactsKey: S3 key for UI build ZIP
        EnvironmentVariables: Dict of env vars for the app
    """
    print(f"Event: {json.dumps(event)}")
    
    request_type = event.get('RequestType', 'Create')
    props = event.get('ResourceProperties', {})
    
    app_name = props.get('AppName', 'agentic-analytics-ui')
    branch_name = props.get('BranchName', 'main')
    bucket = props.get('ArtifactsBucket')
    key = props.get('ArtifactsKey', 'ui/build.zip')
    env_vars = props.get('EnvironmentVariables', {})
    
    try:
        if request_type == 'Delete':
            app_id = event.get('PhysicalResourceId', '')
            if app_id and app_id.startswith('d'):  # Amplify app IDs start with 'd'
                delete_amplify_app(app_id, REGION)
            
            send_cfn_response(event, context, 'SUCCESS', {'AppId': app_id})
            return
        
        # Create or Update
        app_id, default_domain = create_amplify_app(app_name, REGION)
        create_branch(app_id, branch_name, REGION)
        
        # Set environment variables
        if env_vars:
            update_app_env_vars(app_id, env_vars, REGION)
        
        # Deploy if artifacts provided
        if bucket:
            job_id = deploy_from_s3(app_id, branch_name, bucket, key, REGION)
            wait_for_deployment(app_id, branch_name, job_id, region=REGION)
        
        app_url = f"https://{branch_name}.{default_domain}"
        
        send_cfn_response(event, context, 'SUCCESS', {
            'AppId': app_id,
            'AppUrl': app_url,
            'DefaultDomain': default_domain
        })
        
    except Exception as e:
        print(f"Error: {e}")
        send_cfn_response(event, context, 'FAILED', reason=str(e))
        raise
