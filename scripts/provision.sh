#!/usr/bin/env bash
# Creates the DynamoDB fix table (+ family GSI) and the S3 logs bucket. Idempotent.
# Usage: AWS_PROFILE=hackathon AWS_REGION=ap-south-1 ./scripts/provision.sh [bucket-name]
set -euo pipefail
REGION="${AWS_REGION:-ap-south-1}"
TABLE="${FIXES_TABLE:-deployment_fixes}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="${1:-nomeshops-logs-${ACCOUNT}-${REGION}}"

echo "account=$ACCOUNT region=$REGION table=$TABLE bucket=$BUCKET"

if aws dynamodb describe-table --table-name "$TABLE" --region "$REGION" >/dev/null 2>&1; then
  echo "table $TABLE exists"
else
  aws dynamodb create-table --region "$REGION" --table-name "$TABLE" \
    --attribute-definitions AttributeName=error_signature,AttributeType=S AttributeName=error_family,AttributeType=S \
    --key-schema AttributeName=error_signature,KeyType=HASH \
    --global-secondary-indexes 'IndexName=family-index,KeySchema=[{AttributeName=error_family,KeyType=HASH}],Projection={ProjectionType=ALL}' \
    --billing-mode PAY_PER_REQUEST --tags Key=project,Value=nomeshops >/dev/null
  aws dynamodb wait table-exists --table-name "$TABLE" --region "$REGION"
  echo "created table $TABLE"
fi

if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "bucket $BUCKET exists"
else
  if [ "$REGION" = "us-east-1" ]; then
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  fi
  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  echo "created bucket $BUCKET"
fi

sed "s/LOGS_BUCKET_PLACEHOLDER/$BUCKET/g" scripts/iam-task-policy.json > scripts/iam-task-policy.resolved.json
echo
echo "Add to .env:"
echo "AWS_REGION=$REGION"
echo "FIXES_TABLE=$TABLE"
echo "LOGS_BUCKET=$BUCKET"
echo
echo "Task-role policy with the bucket filled in: scripts/iam-task-policy.resolved.json"
