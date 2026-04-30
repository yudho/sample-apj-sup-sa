#!/usr/bin/env python3
"""
Generate vector embeddings for database metadata using Amazon Bedrock Titan Embeddings.
Store embeddings in Aurora PostgreSQL with pgvector for semantic search.
"""
import boto3
import json
import os
import sys
import time
from decimal import Decimal

# Configuration
AWS_REGION = os.environ.get('AWS_REGION', 'us-west-2')
STACK_NAME = os.environ.get('AURORA_STACK_NAME', 'agentic-analytics-aurora')
EMBEDDING_MODEL = 'amazon.titan-embed-text-v2:0'
EMBEDDING_DIMENSION = 1024  # Titan v2 uses 1024 dimensions

# Initialize clients
cf_client = boto3.client('cloudformation', region_name=AWS_REGION)
rds_client = boto3.client('rds-data', region_name=AWS_REGION)
bedrock_client = boto3.client('bedrock-runtime', region_name=AWS_REGION)
secrets_client = boto3.client('secretsmanager', region_name=AWS_REGION)

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
METADATA_PATH = os.path.join(SCRIPT_DIR, 'glue-catalog-metadata.json')


def get_stack_outputs():
    """Get CloudFormation stack outputs."""
    try:
        response = cf_client.describe_stacks(StackName=STACK_NAME)
        outputs = {}
        for output in response['Stacks'][0]['Outputs']:
            outputs[output['OutputKey']] = output['OutputValue']
        return outputs
    except Exception as e:
        print(f"Error getting stack outputs: {e}")
        return None


def execute_sql(cluster_arn, secret_arn, database, sql, parameters=None):
    """Execute SQL via RDS Data API."""
    try:
        kwargs = {
            'resourceArn': cluster_arn,
            'secretArn': secret_arn,
            'database': database,
            'sql': sql
        }
        if parameters:
            kwargs['parameters'] = parameters
        
        response = rds_client.execute_statement(**kwargs)
        return response
    except Exception as e:
        print(f"SQL Error: {e}")
        print(f"SQL: {sql[:200]}...")
        raise


def create_embeddings_table(cluster_arn, secret_arn, database):
    """Create the metadata_embeddings table with pgvector - Bedrock Knowledge Base compatible."""
    print("Creating metadata_embeddings table (Bedrock KB compatible)...")
    
    # Drop existing table if exists
    try:
        execute_sql(cluster_arn, secret_arn, database, 
                   "DROP TABLE IF EXISTS metadata_embeddings CASCADE")
    except:
        pass
    
    # Create Bedrock Knowledge Base compatible table
    # Required columns: id (UUID), chunks (TEXT), embedding (VECTOR), metadata (JSON)
    create_table_sql = f"""
    CREATE TABLE metadata_embeddings (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        chunks TEXT NOT NULL,
        embedding vector({EMBEDDING_DIMENSION}),
        metadata JSON,
        custom_metadata JSONB,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """
    execute_sql(cluster_arn, secret_arn, database, create_table_sql)
    print("  [OK] Table created with Bedrock KB compatible schema")
    print(f"    Columns: id (UUID), chunks (TEXT), embedding (vector({EMBEDDING_DIMENSION})), metadata (JSON), custom_metadata (JSONB)")
    
    # Create IVFFlat index for cosine similarity search
    # Note: Index creation requires some data first, so we'll create it after loading
    print("  [OK] Table ready for embeddings")


def create_vector_index(cluster_arn, secret_arn, database):
    """Create IVFFlat index after data is loaded."""
    print("Creating vector index...")
    
    # Check row count to determine lists parameter
    result = execute_sql(cluster_arn, secret_arn, database,
                        "SELECT COUNT(*) FROM metadata_embeddings")
    row_count = result['records'][0][0]['longValue']
    
    # IVFFlat lists should be sqrt(n) for optimal performance
    lists = max(1, int(row_count ** 0.5))
    
    index_sql = f"""
    CREATE INDEX IF NOT EXISTS idx_metadata_embeddings_vector 
    ON metadata_embeddings 
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = {lists})
    """
    execute_sql(cluster_arn, secret_arn, database, index_sql)
    print(f"  [OK] IVFFlat index created with {lists} lists")


def generate_embedding(text, max_retries=3):
    """Generate embedding for text using Bedrock Titan."""
    for attempt in range(max_retries):
        try:
            response = bedrock_client.invoke_model(
                modelId=EMBEDDING_MODEL,
                contentType='application/json',
                accept='application/json',
                body=json.dumps({
                    'inputText': text,
                    'dimensions': EMBEDDING_DIMENSION,
                    'normalize': True
                })
            )
            result = json.loads(response['body'].read())
            return result['embedding']
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                print(f"  Retry {attempt + 1}/{max_retries} after {wait_time}s: {e}")
                time.sleep(wait_time)
            else:
                raise


def store_embedding(cluster_arn, secret_arn, database, entity_type, entity_name, 
                   parent_entity, description, metadata, embedding):
    """Store an embedding in the database (Bedrock KB compatible format)."""
    # Convert embedding to PostgreSQL array format
    embedding_str = '[' + ','.join(str(x) for x in embedding) + ']'
    
    # Build the chunks text (searchable content)
    if parent_entity:
        chunks = f"{entity_type}: {entity_name} in {parent_entity}. {description}"
    else:
        chunks = f"{entity_type}: {entity_name}. {description}"
    
    # Build metadata JSON (for source attribution)
    meta = {
        "entity_type": entity_type,
        "entity_name": entity_name,
        "parent_entity": parent_entity,
        "source": "glue_catalog"
    }
    
    # Build custom_metadata JSONB (additional details)
    custom_meta = metadata if metadata else {}
    
    sql = """
    INSERT INTO metadata_embeddings 
    (chunks, embedding, metadata, custom_metadata)
    VALUES (:chunks, :embedding::vector, :metadata::json, :custom_metadata::jsonb)
    """
    
    parameters = [
        {'name': 'chunks', 'value': {'stringValue': chunks}},
        {'name': 'embedding', 'value': {'stringValue': embedding_str}},
        {'name': 'metadata', 'value': {'stringValue': json.dumps(meta)}},
        {'name': 'custom_metadata', 'value': {'stringValue': json.dumps(custom_meta)}},
    ]
    
    execute_sql(cluster_arn, secret_arn, database, sql, parameters)


def load_metadata():
    """Load metadata from JSON file."""
    with open(METADATA_PATH, 'r') as f:
        return json.load(f)


def generate_table_text(table):
    """Generate searchable text for a table."""
    columns_text = ', '.join([
        f"{col['name']} ({col['type']})" 
        for col in table['columns'][:10]  # Limit to first 10 columns
    ])
    
    fk_text = ""
    if table.get('foreign_keys'):
        fk_text = " Related to: " + ', '.join([
            f"{fk['references']['table']}" 
            for fk in table['foreign_keys']
        ])
    
    return f"Table: {table['name']}. {table['description']}. Columns: {columns_text}.{fk_text}"


def generate_column_text(table_name, column):
    """Generate searchable text for a column."""
    text = f"Column {column['name']} in table {table_name}. Type: {column['type']}. {column.get('comment', '')}"
    
    if column.get('enum_values'):
        text += f" Valid values: {', '.join(column['enum_values'])}."
    
    return text


def vectorize_metadata(cluster_arn, secret_arn, database, metadata):
    """Generate and store embeddings for all metadata."""
    print("\nGenerating embeddings for metadata...")
    
    total_embeddings = 0
    
    for table in metadata['tables']:
        table_name = table['name']
        print(f"\n  Processing table: {table_name}")
        
        # Generate embedding for table
        table_text = generate_table_text(table)
        print(f"    Generating table embedding...")
        embedding = generate_embedding(table_text)
        
        table_metadata = {
            'columns': [col['name'] for col in table['columns']],
            'foreign_keys': table.get('foreign_keys', []),
            'primary_key': table.get('primary_key')
        }
        
        store_embedding(
            cluster_arn, secret_arn, database,
            entity_type='table',
            entity_name=table_name,
            parent_entity=None,
            description=table['description'],
            metadata=table_metadata,
            embedding=embedding
        )
        total_embeddings += 1
        
        # Generate embeddings for columns
        for column in table['columns']:
            column_text = generate_column_text(table_name, column)
            embedding = generate_embedding(column_text)
            
            column_metadata = {
                'type': column['type'],
                'nullable': column.get('nullable', True),
                'enum_values': column.get('enum_values')
            }
            
            store_embedding(
                cluster_arn, secret_arn, database,
                entity_type='column',
                entity_name=column['name'],
                parent_entity=table_name,
                description=column.get('comment', ''),
                metadata=column_metadata,
                embedding=embedding
            )
            total_embeddings += 1
        
        print(f"    [OK] {len(table['columns']) + 1} embeddings stored")
    
    return total_embeddings


def semantic_search(cluster_arn, secret_arn, database, query, top_k=10):
    """Search metadata by semantic similarity."""
    print(f"\nSearching for: '{query}'")
    
    # Generate embedding for query
    query_embedding = generate_embedding(query)
    embedding_str = '[' + ','.join(str(x) for x in query_embedding) + ']'
    
    # Search using cosine distance (smaller = more similar)
    # Convert to similarity: 1 - distance
    sql = f"""
    SELECT 
        id,
        chunks,
        metadata,
        custom_metadata,
        (1 - (embedding <=> :query_embedding::vector)) as similarity
    FROM metadata_embeddings
    ORDER BY embedding <=> :query_embedding::vector
    LIMIT :top_k
    """
    
    parameters = [
        {'name': 'query_embedding', 'value': {'stringValue': embedding_str}},
        {'name': 'top_k', 'value': {'longValue': top_k}},
    ]
    
    result = execute_sql(cluster_arn, secret_arn, database, sql, parameters)
    
    results = []
    for record in result.get('records', []):
        # Handle different response formats
        similarity_val = record[4]
        if 'stringValue' in similarity_val:
            sim = float(similarity_val['stringValue'])
        elif 'doubleValue' in similarity_val:
            sim = similarity_val['doubleValue']
        else:
            sim = 0.0
        
        meta = json.loads(record[2].get('stringValue', '{}'))
        custom_meta = json.loads(record[3].get('stringValue', '{}'))
            
        results.append({
            'id': record[0].get('stringValue'),
            'chunks': record[1].get('stringValue'),
            'entity_type': meta.get('entity_type'),
            'entity_name': meta.get('entity_name'),
            'parent_entity': meta.get('parent_entity'),
            'metadata': meta,
            'custom_metadata': custom_meta,
            'similarity': sim
        })
    
    return results


def test_semantic_search(cluster_arn, secret_arn, database):
    """Test semantic search with sample queries."""
    print("\n" + "=" * 60)
    print("Testing Semantic Search")
    print("=" * 60)
    
    test_queries = [
        "customer information and contact details",
        "revenue and financial transactions",
        "unicorn availability and maintenance",
        "booking history and reservations",
        "subscription plans and pricing"
    ]
    
    for query in test_queries:
        results = semantic_search(cluster_arn, secret_arn, database, query, top_k=5)
        print(f"\nQuery: '{query}'")
        print("-" * 40)
        for i, r in enumerate(results, 1):
            parent = f" (in {r['parent_entity']})" if r['parent_entity'] else ""
            print(f"  {i}. [{r['entity_type']}] {r['entity_name']}{parent} - {r['similarity']:.3f}")


def main():
    """Main function to generate and store embeddings."""
    print("=" * 60)
    print("Vector Embeddings Generation")
    print("=" * 60)
    print(f"Region: {AWS_REGION}")
    print(f"Model: {EMBEDDING_MODEL}")
    print(f"Dimensions: {EMBEDDING_DIMENSION}")
    print()
    
    # Get stack outputs
    outputs = get_stack_outputs()
    if not outputs:
        print("Failed to get CloudFormation outputs")
        sys.exit(1)
    
    cluster_arn = outputs.get('AuroraClusterArn') or outputs.get('AuroraResourceArn')
    secret_arn = outputs.get('DatabaseSecretArn')
    database = outputs.get('DatabaseName', 'timely_unicorn')
    
    print(f"Database: {database}")
    print(f"Cluster ARN: {cluster_arn}")
    print()
    
    # Load metadata
    print("Loading metadata from Glue catalog export...")
    metadata = load_metadata()
    print(f"  Found {len(metadata['tables'])} tables")
    print()
    
    # Create embeddings table
    create_embeddings_table(cluster_arn, secret_arn, database)
    print()
    
    # Generate and store embeddings
    total = vectorize_metadata(cluster_arn, secret_arn, database, metadata)
    print(f"\n  [OK] Total embeddings generated: {total}")
    
    # Create vector index
    print()
    create_vector_index(cluster_arn, secret_arn, database)
    
    # Test semantic search
    test_semantic_search(cluster_arn, secret_arn, database)
    
    print("\n" + "=" * 60)
    print("Embeddings generation complete!")
    print("=" * 60)


if __name__ == '__main__':
    main()
