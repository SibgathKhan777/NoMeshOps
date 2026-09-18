#!/usr/bin/env bash
# Launches 3 small SSM-managed EC2 targets with different OS / arch / Python, tagged nomeshops=target.
#   ubuntu22  : Ubuntu 22.04 x86_64, Python 3.10 (no pip/venv preinstalled -> deterministic rules path)
#   al2023    : Amazon Linux 2023 x86_64, Python 3.9 (no git preinstalled)
#   al2023arm : Amazon Linux 2023 aarch64 on Graviton
# Usage: AWS_PROFILE=hackathon AWS_REGION=ap-south-1 ./scripts/launch_targets.sh
# Tear down: ./scripts/launch_targets.sh --terminate
set -euo pipefail
REGION="${AWS_REGION:-ap-south-1}"
ROLE=nomeshops-ssm-target

if [ "${1:-}" = "--terminate" ]; then
  IDS=$(aws ec2 describe-instances --region "$REGION" --filters Name=tag:nomeshops,Values=target Name=instance-state-name,Values=pending,running,stopped \
        --query 'Reservations[].Instances[].InstanceId' --output text)
  [ -n "$IDS" ] && aws ec2 terminate-instances --region "$REGION" --instance-ids $IDS >/dev/null && echo "terminating: $IDS" || echo "nothing to terminate"
  exit 0
fi

if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$ROLE" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
  aws iam create-instance-profile --instance-profile-name "$ROLE" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$ROLE" --role-name "$ROLE"
  echo "created role + instance profile $ROLE (waiting for propagation)"; sleep 12
fi
# let the SSM agent upload full command output to the logs bucket (optional but nice for the audit trail)
if [ -n "${LOGS_BUCKET:-}" ]; then
  sed "s/LOGS_BUCKET_PLACEHOLDER/$LOGS_BUCKET/" scripts/iam-target-s3-output.json > /tmp/nomeshops-target-s3.json
  aws iam put-role-policy --role-name "$ROLE" --policy-name nomeshops-ssm-output-to-s3 --policy-document file:///tmp/nomeshops-target-s3.json
fi

ami() { aws ssm get-parameter --region "$REGION" --name "$1" --query Parameter.Value --output text; }
UBUNTU22=$(ami /aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id)
AL2023=$(ami /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64)
AL2023ARM=$(ami /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64)

launch() { # name ami type
  aws ec2 run-instances --region "$REGION" --image-id "$2" --instance-type "$3" --count 1 \
    --iam-instance-profile Name="$ROLE" \
    --metadata-options HttpTokens=required \
    --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":16,"VolumeType":"gp3"}},{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":16,"VolumeType":"gp3"}}]' \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=nomeshops-$1},{Key=nomeshops,Value=target}]" \
    --query 'Instances[0].InstanceId' --output text 2>/dev/null \
  || aws ec2 run-instances --region "$REGION" --image-id "$2" --instance-type "$3" --count 1 \
    --iam-instance-profile Name="$ROLE" --metadata-options HttpTokens=required \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=nomeshops-$1},{Key=nomeshops,Value=target}]" \
    --query 'Instances[0].InstanceId' --output text
}

I1=$(launch ubuntu22 "$UBUNTU22" t3.small)
I2=$(launch al2023 "$AL2023" t3.small)
I3=$(launch al2023arm "$AL2023ARM" t4g.small)
echo "ubuntu22  $I1"
echo "al2023    $I2"
echo "al2023arm $I3"
echo "waiting for SSM registration (1-3 min)..."
for i in $(seq 1 40); do
  N=$(aws ssm describe-instance-information --region "$REGION" --filters "Key=InstanceIds,Values=$I1,$I2,$I3" --query 'length(InstanceInformationList)' --output text)
  [ "$N" = "3" ] && break; sleep 10
done
aws ssm describe-instance-information --region "$REGION" --filters "Key=InstanceIds,Values=$I1,$I2,$I3" \
  --query 'InstanceInformationList[].[InstanceId,PlatformName,PlatformVersion,PingStatus]' --output table
