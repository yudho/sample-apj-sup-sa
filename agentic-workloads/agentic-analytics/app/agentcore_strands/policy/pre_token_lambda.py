def lambda_handler(event, context):
    """Add custom:role and custom:account_id to access token claims for policy evaluation (V2 trigger)"""
    trigger_source = event.get('triggerSource', '')
    user_attrs = event.get('request', {}).get('userAttributes', {})
    role = user_attrs.get('custom:role', 'user')
    account_id = user_attrs.get('custom:account_id', '')
    
    print(f"Trigger: {trigger_source}, User: {user_attrs.get('email', 'unknown')}, Role: {role}, Account: {account_id[:8] if account_id else 'none'}...")
    
    claims = {
        'custom:role': role,
        'custom:account_id': account_id
    }
    
    # V2 trigger - modify access token claims
    if trigger_source == 'TokenGeneration_HostedAuth' or trigger_source == 'TokenGeneration_Authentication':
        event['response'] = {
            'claimsAndScopeOverrideDetails': {
                'accessTokenGeneration': {
                    'claimsToAddOrOverride': claims
                },
                'idTokenGeneration': {
                    'claimsToAddOrOverride': claims
                }
            }
        }
    else:
        # V1 fallback
        event['response'] = {
            'claimsOverrideDetails': {
                'claimsToAddOrOverride': claims
            }
        }
    
    print(f"Added claims to tokens: role={role}, account_id={account_id[:8] if account_id else 'none'}...")
    return event
