#!/usr/bin/env python3
"""Configure observability for AgentCore Gateway and Memory.

Sets up CloudWatch vended log delivery and tracing delivery for:
  - Gateway: APPLICATION_LOGS + TRACES
  - Memory: APPLICATION_LOGS + TRACES

Run AFTER deploy_gateway.py and deploy_memory.py.
Transaction Search must already be enabled (done by CFN observability-stack).
"""
import os, sys, json, boto3
from pathlib import Path
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
load_dotenv(ROOT_DIR / 'config.env')

REGION = os.getenv('AWS_REGION', 'us-east-1')
ACCOUNT_ID = boto3.client('sts', region_name=REGION).get_caller_identity()['Account']


def enable_observability(resource_arn, resource_id, resource_type):
    """Enable log delivery + tracing for an AgentCore resource."""
    logs = boto3.client('logs', region_name=REGION)
    # Use short prefix for delivery names (60 char limit)
    import hashlib
    short_id = hashlib.md5(resource_id.encode()).hexdigest()[:12]
    prefix = f"{resource_type}-{short_id}"
    log_group = f'/aws/vendedlogs/bedrock-agentcore/{resource_type}/APPLICATION_LOGS/{resource_id}'

    # Create log group (ignore if exists)
    try:
        logs.create_log_group(logGroupName=log_group)
        print(f"  [OK] Created log group: {log_group}")
    except logs.exceptions.ResourceAlreadyExistsException:
        print(f"  [OK] Log group exists: {log_group}")

    log_group_arn = f'arn:aws:logs:{REGION}:{ACCOUNT_ID}:log-group:{log_group}'

    # Logs delivery source
    src_name = f"{prefix}-logs-src"
    try:
        logs.put_delivery_source(name=src_name, logType='APPLICATION_LOGS', resourceArn=resource_arn)
        print(f"  [OK] Logs delivery source: {src_name}")
    except Exception as e:
        if 'already exists' in str(e).lower() or 'conflict' in str(e).lower():
            print(f"  [OK] Logs delivery source exists: {src_name}")
        else:
            raise

    # Logs delivery destination
    dst_name = f"{prefix}-logs-dst"
    try:
        dst_resp = logs.put_delivery_destination(
            name=dst_name, deliveryDestinationType='CWL',
            deliveryDestinationConfiguration={'destinationResourceArn': log_group_arn})
        dst_arn = dst_resp['deliveryDestination']['arn']
        print(f"  [OK] Logs delivery destination: {dst_name}")
    except Exception as e:
        if 'already exists' in str(e).lower() or 'conflict' in str(e).lower():
            # Fetch existing
            dst_arn = f'arn:aws:logs:{REGION}:{ACCOUNT_ID}:delivery-destination:{dst_name}'
            print(f"  [OK] Logs delivery destination exists: {dst_name}")
        else:
            raise

    # Logs delivery
    try:
        logs.create_delivery(deliverySourceName=src_name, deliveryDestinationArn=dst_arn)
        print(f"  [OK] Logs delivery created")
    except Exception as e:
        if 'already exists' in str(e).lower() or 'conflict' in str(e).lower():
            print(f"  [OK] Logs delivery already exists")
        else:
            raise

    # Traces delivery source
    trace_src = f"{prefix}-traces-src"
    try:
        logs.put_delivery_source(name=trace_src, logType='TRACES', resourceArn=resource_arn)
        print(f"  [OK] Traces delivery source: {trace_src}")
    except Exception as e:
        if 'already exists' in str(e).lower() or 'conflict' in str(e).lower():
            print(f"  [OK] Traces delivery source exists: {trace_src}")
        else:
            raise

    # Traces delivery destination (X-Ray)
    trace_dst = f"{prefix}-traces-dst"
    try:
        trace_dst_resp = logs.put_delivery_destination(name=trace_dst, deliveryDestinationType='XRAY')
        trace_dst_arn = trace_dst_resp['deliveryDestination']['arn']
        print(f"  [OK] Traces delivery destination: {trace_dst}")
    except Exception as e:
        if 'already exists' in str(e).lower() or 'conflict' in str(e).lower():
            trace_dst_arn = f'arn:aws:logs:{REGION}:{ACCOUNT_ID}:delivery-destination:{trace_dst}'
            print(f"  [OK] Traces delivery destination exists: {trace_dst}")
        else:
            raise

    # Traces delivery
    try:
        logs.create_delivery(deliverySourceName=trace_src, deliveryDestinationArn=trace_dst_arn)
        print(f"  [OK] Traces delivery created")
    except Exception as e:
        if 'already exists' in str(e).lower() or 'conflict' in str(e).lower():
            print(f"  [OK] Traces delivery already exists")
        else:
            raise


def main():
    print("Configuring AgentCore Observability")
    print("=" * 70)

    agentcore = boto3.client('bedrock-agentcore-control', region_name=REGION)

    # Gateway
    gateway_id = os.getenv('GATEWAY_ID')
    gateway_arn = os.getenv('GATEWAY_ARN')
    if gateway_id:
        if not gateway_arn:
            gw = agentcore.get_gateway(gatewayIdentifier=gateway_id)
            gateway_arn = gw.get('gatewayArn', '')
        print(f"\nGateway: {gateway_id}")
        enable_observability(gateway_arn, gateway_id, 'gateway')
    else:
        print("\n[SKIP] No GATEWAY_ID in config.env — run deploy_gateway.py first")

    # Memory
    memory_id = os.getenv('MEMORY_ID')
    if memory_id:
        try:
            from bedrock_agentcore.memory import MemoryClient
            mc = MemoryClient(region_name=REGION)
            memories = mc.list_memories()
            memory_arn = None
            for m in memories:
                if m.get('id') == memory_id or memory_id in str(m.get('arn', '')):
                    memory_arn = m.get('arn')
                    break
            if memory_arn:
                print(f"\nMemory: {memory_id}")
                enable_observability(memory_arn, memory_id, 'memory')
            else:
                print(f"\n[SKIP] Memory {memory_id} not found")
        except Exception as e:
            print(f"\n[SKIP] Memory observability failed: {e}")
    else:
        print("\n[SKIP] No MEMORY_ID in config.env — run deploy_memory.py first")

    print("\n[OK] Observability configuration complete")
    print("View traces: CloudWatch → GenAI Observability → AgentCore")
    print("View gateway logs: CloudWatch → Log groups → /aws/vendedlogs/bedrock-agentcore/gateway/")


if __name__ == "__main__":
    main()
