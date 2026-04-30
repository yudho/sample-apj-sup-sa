#!/bin/bash
# Package and upload deployment artifacts to S3
# Uses content-hash versioning for Lambda packages
#
# Usage:
#   ./package_and_upload.sh <bucket-name>                              # Full upload (demo mode)
#   ./package_and_upload.sh <bucket-name> --deploy-mode workshop       # Skip UI build and demo-mode Lambdas
#   ./package_and_upload.sh --workshop-studio <workshop-id>            # Upload to Workshop Studio assets
#   ./package_and_upload.sh --workshop-studio <workshop-id> --deploy-mode workshop

set -e

# Parse arguments
WORKSHOP_STUDIO=false
DEPLOY_MODE="demo"
WORKSHOP_ID=""
BUCKET=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --workshop-studio)
            WORKSHOP_STUDIO=true
            WORKSHOP_ID=$2
            shift 2
            ;;
        --deploy-mode)
            DEPLOY_MODE=$2
            shift 2
            ;;
        *)
            BUCKET=$1
            shift
            ;;
    esac
done

if [ "$WORKSHOP_STUDIO" = true ]; then
    if [ -z "$WORKSHOP_ID" ]; then
        echo "Usage: $0 --workshop-studio <workshop-id> [--deploy-mode workshop]"
        exit 1
    fi
    BUCKET="ws-assets-us-east-1"
    REGION="us-east-1"
    PREFIX="$WORKSHOP_ID"
    echo "Workshop Studio mode: uploading to s3://$BUCKET/$PREFIX/"
else
    REGION=${AWS_REGION:-us-west-2}
    PREFIX=""
    if [ -z "$BUCKET" ]; then
        echo "Usage: $0 <bucket-name> [--deploy-mode workshop]"
        echo "       $0 --workshop-studio <workshop-id> [--deploy-mode workshop]"
        exit 1
    fi
    echo "Packaging and uploading to s3://$BUCKET..."
fi

if [ "$DEPLOY_MODE" = "workshop" ]; then
    echo "Deploy mode: workshop (skipping UI build and demo-mode Lambdas)"
else
    echo "Deploy mode: demo (full packaging)"
fi

# Helper to build S3 path
s3_path() {
    local key=$1
    if [ -n "$PREFIX" ]; then
        echo "$PREFIX/$key"
    else
        echo "$key"
    fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(dirname "$SCRIPT_DIR")"
PROJECT_ROOT="$(dirname "$INFRA_DIR")"
TEMP_DIR=$(mktemp -d)
trap "rm -rf $TEMP_DIR" EXIT

echo "Packaging and uploading to s3://$BUCKET..."

# Function to package Lambda with hash-based versioning
package_lambda() {
    local name=$1
    local source_dir=$2
    shift 2
    local source_files=("$@")
    
    # Default to handler.py if no files specified
    if [ ${#source_files[@]} -eq 0 ]; then
        source_files=("handler.py")
    fi
    
    cd "$source_dir"
    zip -j "$TEMP_DIR/${name}.zip" "${source_files[@]}" > /dev/null
    
    # Compute hash
    if command -v sha256sum &> /dev/null; then
        HASH=$(sha256sum "$TEMP_DIR/${name}.zip" | cut -c1-8)
    else
        HASH=$(shasum -a 256 "$TEMP_DIR/${name}.zip" | cut -c1-8)
    fi
    
    local s3_key=$(s3_path "lambdas/${name}-${HASH}.zip")
    aws s3 cp "$TEMP_DIR/${name}.zip" "s3://$BUCKET/$s3_key" --region $REGION > /dev/null
    # Return just the relative key (without prefix) for CloudFormation params
    echo "lambdas/${name}-${HASH}.zip"
}

echo "Packaging Lambdas..."
DB_INIT_KEY=$(package_lambda "database_init" "$INFRA_DIR/custom-resource-lambdas/database_init")
echo "  database_init: $DB_INIT_KEY"

GLUE_KEY=$(package_lambda "glue_crawler_trigger" "$INFRA_DIR/custom-resource-lambdas/glue_crawler_trigger")
echo "  glue_crawler_trigger: $GLUE_KEY"

BEDROCK_KEY=$(package_lambda "bedrock_kb_ingestion" "$INFRA_DIR/custom-resource-lambdas/bedrock_kb_ingestion")
echo "  bedrock_kb_ingestion: $BEDROCK_KEY"

OBSERVABILITY_KEY=$(package_lambda "observability_setup" "$INFRA_DIR/custom-resource-lambdas/observability_setup")
echo "  observability_setup: $OBSERVABILITY_KEY"

# Package agentcore_gateway (demo mode only)
if [ "$DEPLOY_MODE" != "workshop" ]; then
    echo "Packaging agentcore_gateway (with policy/interceptor scripts)..."
    AGENTCORE_DIR="$INFRA_DIR/custom-resource-lambdas/agentcore_gateway"
    cp "$PROJECT_ROOT/app/agentcore_strands/deploy_policy.py" "$AGENTCORE_DIR/"
    cp "$PROJECT_ROOT/app/agentcore_strands/deploy_interceptor.py" "$AGENTCORE_DIR/"
    AGENTCORE_KEY=$(package_lambda "agentcore_gateway" "$AGENTCORE_DIR" handler.py deploy_policy.py deploy_interceptor.py)
    rm -f "$AGENTCORE_DIR/deploy_policy.py" "$AGENTCORE_DIR/deploy_interceptor.py"
    echo "  agentcore_gateway: $AGENTCORE_KEY"

    # Package datafoundation Lambda
    echo "Packaging datafoundation Lambda..."
    cd "$PROJECT_ROOT/app/agentcore_strands"
    zip -j "$TEMP_DIR/datafoundation_lambda.zip" datafoundation_lambda.py > /dev/null
    aws s3 cp "$TEMP_DIR/datafoundation_lambda.zip" "s3://$BUCKET/$(s3_path "lambda/datafoundation_lambda.zip")" --region $REGION > /dev/null
    echo "  datafoundation: lambda/datafoundation_lambda.zip"

    # Package amplify_hosting Lambda
    echo "Packaging amplify_hosting (with common/ utilities)..."
    AMPLIFY_DIR="$INFRA_DIR/custom-resource-lambdas/amplify_hosting"
    mkdir -p "$TEMP_DIR/amplify_pkg/common"
    cp "$AMPLIFY_DIR/handler.py" "$TEMP_DIR/amplify_pkg/"
    cp "$PROJECT_ROOT/common/amplify_utils.py" "$TEMP_DIR/amplify_pkg/common/"
    cp "$PROJECT_ROOT/common/__init__.py" "$TEMP_DIR/amplify_pkg/common/"
    cd "$TEMP_DIR/amplify_pkg"
    zip -r "$TEMP_DIR/amplify_hosting.zip" . > /dev/null
    if command -v sha256sum &> /dev/null; then
        HASH=$(sha256sum "$TEMP_DIR/amplify_hosting.zip" | cut -c1-8)
    else
        HASH=$(shasum -a 256 "$TEMP_DIR/amplify_hosting.zip" | cut -c1-8)
    fi
    AMPLIFY_KEY="lambdas/amplify_hosting-${HASH}.zip"
    aws s3 cp "$TEMP_DIR/amplify_hosting.zip" "s3://$BUCKET/$(s3_path "$AMPLIFY_KEY")" --region $REGION > /dev/null
    echo "  amplify_hosting: $AMPLIFY_KEY"
else
    echo "Skipping agentcore_gateway, datafoundation, amplify_hosting (workshop-only mode)"
    AGENTCORE_KEY="N/A"
    AMPLIFY_KEY="N/A"
fi

echo "Uploading templates..."
cd "$INFRA_DIR/stacks"
aws s3 cp main-stack.yaml "s3://$BUCKET/$(s3_path "templates/main-stack.yaml")" --region $REGION > /dev/null
aws s3 cp aurora-stack.yaml "s3://$BUCKET/$(s3_path "templates/aurora-stack.yaml")" --region $REGION > /dev/null
aws s3 cp database-init-stack.yaml "s3://$BUCKET/$(s3_path "templates/database-init-stack.yaml")" --region $REGION > /dev/null
aws s3 cp glue-stack.yaml "s3://$BUCKET/$(s3_path "templates/glue-stack.yaml")" --region $REGION > /dev/null
aws s3 cp bedrock-kb-stack.yaml "s3://$BUCKET/$(s3_path "templates/bedrock-kb-stack.yaml")" --region $REGION > /dev/null
aws s3 cp agentcore-stack.yaml "s3://$BUCKET/$(s3_path "templates/agentcore-stack.yaml")" --region $REGION > /dev/null
aws s3 cp amplify-stack.yaml "s3://$BUCKET/$(s3_path "templates/amplify-stack.yaml")" --region $REGION > /dev/null
aws s3 cp cognito-stack.yaml "s3://$BUCKET/$(s3_path "templates/cognito-stack.yaml")" --region $REGION > /dev/null
aws s3 cp code-editor-stack.yaml "s3://$BUCKET/$(s3_path "templates/code-editor-stack.yaml")" --region $REGION > /dev/null
aws s3 cp observability-stack.yaml "s3://$BUCKET/$(s3_path "templates/observability-stack.yaml")" --region $REGION > /dev/null

echo "Uploading schema and data..."
aws s3 cp "$PROJECT_ROOT/dataset/schema/schema.sql" "s3://$BUCKET/$(s3_path "schema/schema.sql")" --region $REGION > /dev/null
aws s3 sync "$PROJECT_ROOT/dataset/data/" "s3://$BUCKET/$(s3_path "data")/" --region $REGION > /dev/null
aws s3 cp "$PROJECT_ROOT/dataset/docs/business-context.md" "s3://$BUCKET/$(s3_path "docs/business-context.md")" --region $REGION > /dev/null

echo "Uploading SOP file..."
aws s3 cp "$PROJECT_ROOT/app/agentcore_strands/unicorn_rental_analytics.sop.md" "s3://$BUCKET/$(s3_path "sops/unicorn_rental_analytics.sop.md")" --region $REGION > /dev/null

echo "Uploading JDBC driver..."
# Download PostgreSQL JDBC driver if not present
DRIVER_PATH="$TEMP_DIR/postgresql-42.7.3.jar"
if [ ! -f "$DRIVER_PATH" ]; then
    curl -sL https://jdbc.postgresql.org/download/postgresql-42.7.3.jar -o "$DRIVER_PATH"
fi
aws s3 cp "$DRIVER_PATH" "s3://$BUCKET/$(s3_path "drivers/postgresql-42.7.3.jar")" --region $REGION > /dev/null

if [ "$DEPLOY_MODE" != "workshop" ]; then
    echo "Building and uploading UI..."
    UI_DIR="$PROJECT_ROOT/app/ui"
    if [ -d "$UI_DIR" ]; then
        cd "$UI_DIR"
        npm install --silent
        npm run build --silent
        
        # Create ZIP of build directory
        cd build
        zip -r "$TEMP_DIR/ui-build.zip" . > /dev/null
        aws s3 cp "$TEMP_DIR/ui-build.zip" "s3://$BUCKET/$(s3_path "ui/build.zip")" --region $REGION > /dev/null
        echo "  Uploaded UI build"
    else
        echo "  Warning: UI directory not found at $UI_DIR, skipping UI build"
    fi
else
    echo "Skipping UI build (workshop-only mode)"
fi

echo "Uploading project repo..."
cd "$PROJECT_ROOT"
# Create ZIP of entire repo (excluding .git, node_modules, etc.)
zip -r "$TEMP_DIR/repo.zip" . \
    -x "*.git*" \
    -x "*node_modules*" \
    -x "*.venv*" \
    -x "*__pycache__*" \
    -x "*.pyc" \
    -x "*app/ui/build*" \
    > /dev/null
aws s3 cp "$TEMP_DIR/repo.zip" "s3://$BUCKET/$(s3_path "repo/agentic-analytics.zip")" --region $REGION > /dev/null
echo "  Uploaded repo"

echo ""
echo "[OK] Upload complete"
echo ""
echo "Lambda S3 Keys:"
echo "  DatabaseInitLambdaKey=$DB_INIT_KEY"
echo "  GlueCrawlerLambdaKey=$GLUE_KEY"
echo "  BedrockKBLambdaKey=$BEDROCK_KEY"
echo "  AgentCoreLambdaKey=$AGENTCORE_KEY"
echo "  AmplifyLambdaKey=$AMPLIFY_KEY"
echo "  ObservabilityLambdaKey=$OBSERVABILITY_KEY"
echo ""

if [ "$WORKSHOP_STUDIO" = true ]; then
    ARTIFACTS_BUCKET="ws-assets-us-east-1/$WORKSHOP_ID"
    TEMPLATE_URL="https://ws-assets-us-east-1.s3.us-east-1.amazonaws.com/$WORKSHOP_ID/templates/main-stack.yaml"
    DEPLOY_REGION="us-east-1"
else
    ARTIFACTS_BUCKET="$BUCKET"
    TEMPLATE_URL="https://$BUCKET.s3.$REGION.amazonaws.com/templates/main-stack.yaml"
    DEPLOY_REGION="$REGION"
fi

echo "Deploy command:"
echo "aws cloudformation create-stack \\"
echo "  --stack-name agentic-analytics-workshop \\"
echo "  --template-url $TEMPLATE_URL \\"
echo "  --parameters \\"
echo "      ParameterKey=ArtifactsBucket,ParameterValue=$ARTIFACTS_BUCKET \\"
echo "      ParameterKey=DeployMode,ParameterValue=workshop \\"
echo "      ParameterKey=DatabaseInitLambdaKey,ParameterValue=$DB_INIT_KEY \\"
echo "      ParameterKey=GlueCrawlerLambdaKey,ParameterValue=$GLUE_KEY \\"
echo "      ParameterKey=BedrockKBLambdaKey,ParameterValue=$BEDROCK_KEY \\"
echo "      ParameterKey=AgentCoreLambdaKey,ParameterValue=$AGENTCORE_KEY \\"
echo "      ParameterKey=AmplifyLambdaKey,ParameterValue=$AMPLIFY_KEY \\"
echo "      ParameterKey=ObservabilityLambdaKey,ParameterValue=$OBSERVABILITY_KEY \\"
echo "  --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND \\"
echo "  --region $DEPLOY_REGION"
