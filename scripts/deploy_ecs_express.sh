#!/usr/bin/env bash
# Build the orchestrator image, push to ECR, and create (or update) an ECS Express Mode service:
# one API call gives a load-balanced public HTTPS endpoint. Flags verified against AWS CLI 2.36.
# Usage: AWS_PROFILE=default AWS_REGION=ap-south-1 LOGS_BUCKET=... ./scripts/deploy_ecs_express.sh
set -euo pipefail
REGION="${AWS_REGION:-ap-south-1}"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REPO=nomeshops
IMAGE="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO:latest"
TASK_ROLE=nomeshops-task-role
EXEC_ROLE=nomeshops-exec-role
INFRA_ROLE=nomeshops-ecs-infra-role
SERVICE=nomeshops
LOG_GROUP=/ecs/nomeshops
: "${LOGS_BUCKET:?set LOGS_BUCKET (from scripts/provision.sh)}"

# 1. ECR: build + push
aws ecr describe-repositories --repository-names "$REPO" --region "$REGION" >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "$REPO" --region "$REGION" --image-scanning-configuration scanOnPush=true >/dev/null
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com" >/dev/null
docker build --platform linux/amd64 -t "$IMAGE" .
docker push "$IMAGE" | tail -1
DIGEST=$(aws ecr describe-images --repository-name "$REPO" --image-ids imageTag=latest --region "$REGION" --query 'imageDetails[0].imageDigest' --output text)
IMAGE_PINNED="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO@$DIGEST"
echo "image: $IMAGE_PINNED"

# 2. Roles
TASKS_TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
ECS_TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
ensure_role() { # name trust
  aws iam get-role --role-name "$1" >/dev/null 2>&1 || { aws iam create-role --role-name "$1" --assume-role-policy-document "$2" >/dev/null; echo "created role $1"; NEW_ROLE=1; }
}
NEW_ROLE=0
ensure_role "$EXEC_ROLE" "$TASKS_TRUST"
aws iam attach-role-policy --role-name "$EXEC_ROLE" --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
ensure_role "$INFRA_ROLE" "$ECS_TRUST"
aws iam attach-role-policy --role-name "$INFRA_ROLE" --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSInfrastructureRoleforExpressGatewayServices
ensure_role "$TASK_ROLE" "$TASKS_TRUST"
sed "s/LOGS_BUCKET_PLACEHOLDER/$LOGS_BUCKET/g" scripts/iam-task-policy.json > scripts/iam-task-policy.resolved.json
aws iam put-role-policy --role-name "$TASK_ROLE" --policy-name nomeshops-task --policy-document file://scripts/iam-task-policy.resolved.json
[ "$NEW_ROLE" = "1" ] && { echo "waiting for IAM propagation"; sleep 12; }
aws logs create-log-group --log-group-name "$LOG_GROUP" --region "$REGION" 2>/dev/null || true

# DEMO_INSTANCE_* map the hosted demo's target picker to real EC2 instance ids (from
# launch_targets.sh). Under EXECUTOR=ssm, app/web.py only lists a target once its instance value
# looks like a real id (i-...) — the other four entries in DEMO_TARGETS are container names with
# no EC2 counterpart on this deployment, so they stay hidden rather than failing when clicked.
DEMO_ENV=""
[ -n "${DEMO_INSTANCE_AWS_UBUNTU:-}" ] && DEMO_ENV="$DEMO_ENV,{\"name\":\"DEMO_INSTANCE_AWS_UBUNTU\",\"value\":\"$DEMO_INSTANCE_AWS_UBUNTU\"}"
[ -n "${DEMO_INSTANCE_AWS_AL2023:-}" ] && DEMO_ENV="$DEMO_ENV,{\"name\":\"DEMO_INSTANCE_AWS_AL2023\",\"value\":\"$DEMO_INSTANCE_AWS_AL2023\"}"

CONTAINER=$(cat <<JSON
{"image":"$IMAGE_PINNED","containerPort":8080,
 "awsLogsConfiguration":{"logGroup":"$LOG_GROUP","logStreamPrefix":"ecs"},
 "environment":[{"name":"AWS_REGION","value":"$REGION"},{"name":"LOGS_BUCKET","value":"$LOGS_BUCKET"},
                {"name":"FIXES_TABLE","value":"${FIXES_TABLE:-deployment_fixes}"},
                {"name":"BEDROCK_MODEL_ID","value":"${BEDROCK_MODEL_ID:-global.anthropic.claude-sonnet-4-6}"}$DEMO_ENV]}
JSON
)

# 3. Create or update the Express service
EXISTING=$(aws ecs list-services --region "$REGION" --query "serviceArns[?contains(@, '/$SERVICE')]|[0]" --output text 2>/dev/null || true)
if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
  echo "updating existing service $EXISTING"
  ARN=$(aws ecs update-express-gateway-service --region "$REGION" --service-arn "$EXISTING" \
    --primary-container "$CONTAINER" --health-check-path /health --cpu 512 --memory 1024 \
    --query 'service.serviceArn' --output text)
else
  ARN=$(aws ecs create-express-gateway-service --region "$REGION" \
    --service-name "$SERVICE" \
    --execution-role-arn "arn:aws:iam::$ACCOUNT:role/$EXEC_ROLE" \
    --infrastructure-role-arn "arn:aws:iam::$ACCOUNT:role/$INFRA_ROLE" \
    --task-role-arn "arn:aws:iam::$ACCOUNT:role/$TASK_ROLE" \
    --primary-container "$CONTAINER" \
    --health-check-path /health --cpu 512 --memory 1024 \
    --scaling-target minTaskCount=1,maxTaskCount=1 \
    --tags key=project,value=nomeshops \
    --query 'service.serviceArn' --output text)
fi
echo "service: $ARN"
echo "waiting for the service to become ACTIVE (ALB + target group provisioning, ~3-5 min)..."
for i in $(seq 1 60); do
  OUT=$(aws ecs describe-express-gateway-service --region "$REGION" --service-arn "$ARN" --output json)
  STATUS=$(printf '%s' "$OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin)["service"]; print(d.get("status",{}).get("statusCode",""))')
  URL=$(printf '%s' "$OUT" | python3 -c '
import json,sys
def walk(o):
    if isinstance(o, dict):
        for k,v in o.items():
            if k=="endpoint" and isinstance(v,str): yield v
            yield from walk(v)
    elif isinstance(o, list):
        for i in o: yield from walk(i)
u=list(walk(json.load(sys.stdin))); print(u[0] if u else "")')
  echo "  $STATUS ${URL:+-> $URL}"
  [ "$STATUS" = "ACTIVE" ] && [ -n "$URL" ] && break
  sleep 15
done
echo
echo "NOMESHOPS_URL=$URL"
echo "try: curl -s $URL/health"
