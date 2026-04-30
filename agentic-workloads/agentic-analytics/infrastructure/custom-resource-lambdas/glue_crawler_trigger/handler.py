"""
Glue Crawler Trigger Custom Resource Lambda
Triggers Glue crawler and waits for completion.

Can be invoked:
1. Via CloudFormation Custom Resource (automatic deployment)
2. Via direct Lambda invoke (workshop mode)
"""
import boto3
import json
import os
import time
import urllib.request

REGION = os.environ.get('AWS_REGION', 'us-west-2')
glue_client = boto3.client('glue', region_name=REGION)
ec2_client = boto3.client('ec2', region_name=REGION)


def cleanup_glue_enis(security_group_id, timeout=120):
    """Delete ENIs created by Glue for the given security group."""
    if not security_group_id:
        print("No security group ID provided, skipping ENI cleanup")
        return
    
    print(f"Cleaning up Glue ENIs for security group: {security_group_id}")
    
    # Find ENIs attached to this security group
    response = ec2_client.describe_network_interfaces(
        Filters=[{'Name': 'group-id', 'Values': [security_group_id]}]
    )
    
    enis = response.get('NetworkInterfaces', [])
    if not enis:
        print("No ENIs found")
        return
    
    print(f"Found {len(enis)} ENI(s) to clean up")
    
    for eni in enis:
        eni_id = eni['NetworkInterfaceId']
        status = eni['Status']
        
        try:
            # Detach if attached
            if status == 'in-use' and eni.get('Attachment'):
                print(f"  Detaching {eni_id}...")
                ec2_client.detach_network_interface(
                    AttachmentId=eni['Attachment']['AttachmentId'],
                    Force=True
                )
                # Wait for detachment
                for _ in range(20):
                    time.sleep(3)
                    resp = ec2_client.describe_network_interfaces(NetworkInterfaceIds=[eni_id])
                    if resp['NetworkInterfaces'][0]['Status'] == 'available':
                        break
            
            # Delete ENI
            print(f"  Deleting {eni_id}...")
            ec2_client.delete_network_interface(NetworkInterfaceId=eni_id)
            print(f"  [OK] Deleted {eni_id}")
        except Exception as e:
            print(f"  Warning: Could not delete {eni_id}: {e}")


def start_crawler(crawler_name):
    """Start the Glue crawler."""
    print(f"Starting crawler: {crawler_name}")
    try:
        glue_client.start_crawler(Name=crawler_name)
        print(f"[OK] Crawler started")
        return True
    except glue_client.exceptions.CrawlerRunningException:
        print(f"Crawler already running")
        return True
    except Exception as e:
        print(f"Error starting crawler: {e}")
        raise


def wait_for_crawler(crawler_name, timeout=600):
    """Wait for crawler to complete."""
    print(f"Waiting for crawler to complete (timeout: {timeout}s)...")
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        response = glue_client.get_crawler(Name=crawler_name)
        state = response['Crawler']['State']
        
        if state == 'READY':
            last_crawl = response['Crawler'].get('LastCrawl', {})
            status = last_crawl.get('Status', 'UNKNOWN')
            print(f"[OK] Crawler completed with status: {status}")
            
            if status == 'SUCCEEDED':
                tables_created = last_crawl.get('TablesCreated', 0)
                tables_updated = last_crawl.get('TablesUpdated', 0)
                print(f"  Tables created: {tables_created}, updated: {tables_updated}")
                return True
            elif status == 'FAILED':
                error = last_crawl.get('ErrorMessage', 'Unknown error')
                print(f"  Error: {error}")
                raise Exception(f"Crawler failed: {error}")
            return True
        
        print(f"  Crawler state: {state}")
        time.sleep(15)
    
    raise Exception(f"Crawler timed out after {timeout}s")


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
                # Clean up Glue ENIs before stack deletion
                security_group_id = props.get('GlueSecurityGroupId')
                cleanup_glue_enis(security_group_id)
                send_cfn_response(event, context, 'SUCCESS')
                return {'status': 'success', 'message': 'Delete - ENIs cleaned up'}
            
            crawler_name = props['CrawlerName']
            wait_for_completion = props.get('WaitForCompletion', 'true').lower() == 'true'
        else:
            crawler_name = event.get('CrawlerName') or os.environ.get('CRAWLER_NAME')
            wait_for_completion = event.get('WaitForCompletion', True)
        
        start_crawler(crawler_name)
        
        if wait_for_completion:
            wait_for_crawler(crawler_name)
        
        if is_cfn:
            send_cfn_response(event, context, 'SUCCESS', {'CrawlerTriggered': 'true'})
        
        return {'status': 'success', 'message': 'Crawler completed successfully'}
        
    except Exception as e:
        print(f"Error: {str(e)}")
        if is_cfn:
            send_cfn_response(event, context, 'FAILED', reason=str(e))
        raise
