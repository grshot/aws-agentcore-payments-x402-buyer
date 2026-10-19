"""Send AgentCore Payments logs and traces to CloudWatch. Run once per account/manager.

    uv run python infra/setup_observability.py --manager-arn arn:aws:bedrock-agentcore:...:payment-manager/...

AgentCore Payments already publishes metrics on its own. Logs and traces need
a delivery set up per payment manager, and traces are only searchable once
CloudWatch Transaction Search is on. This script does both, and is safe to
re-run: anything that already exists is left as it is.

Why bother: these records are written by AWS, not by the agent or by our app.
They're the audit trail the verifier's lesson points to.
"""

from __future__ import annotations

import argparse
import json
import time

import boto3
from botocore.exceptions import ClientError


def enable_transaction_search(region: str, account: str) -> None:
    """Route X-Ray spans to CloudWatch Logs so they can be searched (one-time, per account and region)."""
    xray = boto3.client("xray", region_name=region)
    if xray.get_trace_segment_destination()["Destination"] == "CloudWatchLogs":
        wait_until_active(xray)
        return
    # X-Ray needs permission to write spans into CloudWatch Logs.
    boto3.client("logs", region_name=region).put_resource_policy(
        policyName="TransactionSearchXRayAccess",
        policyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": "TransactionSearchXRayAccess",
                "Effect": "Allow",
                "Principal": {"Service": "xray.amazonaws.com"},
                "Action": "logs:PutLogEvents",
                "Resource": [
                    f"arn:aws:logs:{region}:{account}:log-group:aws/spans:*",
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/application-signals/data:*",
                ],
                "Condition": {
                    "ArnLike": {"aws:SourceArn": f"arn:aws:xray:{region}:{account}:*"},
                    "StringEquals": {"aws:SourceAccount": account},
                },
            }],
        }),
    )
    xray.update_trace_segment_destination(Destination="CloudWatchLogs")
    wait_until_active(xray)


def wait_until_active(xray, timeout_s: int = 600) -> None:
    """Trace delivery can't be set up until the switch has finished (it takes a few minutes)."""
    deadline = time.time() + timeout_s
    while (status := xray.get_trace_segment_destination()["Status"]) != "ACTIVE":
        if time.time() > deadline:
            raise SystemExit(f"Transaction Search still {status}; re-run this script in a few minutes")
        print(f"Transaction Search: {status}, waiting…")
        time.sleep(15)
    print("Transaction Search: on")


def deliver_payment_logs_and_traces(manager_arn: str, region: str, account: str, retention_days: int) -> str:
    """Logs to a CloudWatch log group, traces to X-Ray, for one payment manager."""
    manager_id = manager_arn.split("/")[-1]
    logs = boto3.client("logs", region_name=region)

    # 1. Where the logs go.
    # (The caller also needs the IAM permission bedrock-agentcore:AllowVendedLogDeliveryForResource.
    # It's checked by put_delivery_source below; there is no separate API call for it.)
    log_group = f"/aws/vendedlogs/bedrock-agentcore/payments/{manager_id}"
    try:
        logs.create_log_group(logGroupName=log_group)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    logs.put_retention_policy(logGroupName=log_group, retentionInDays=retention_days)
    log_group_arn = f"arn:aws:logs:{region}:{account}:log-group:{log_group}"

    # 2. Sources (this manager's logs and traces) -> destinations -> deliveries.
    for kind, log_type, destination in [
        ("logs", "APPLICATION_LOGS", {"deliveryDestinationConfiguration": {"destinationResourceArn": log_group_arn}}),
        ("traces", "TRACES", {"deliveryDestinationType": "XRAY"}),
    ]:
        source = logs.put_delivery_source(name=f"{manager_id}-{kind}", resourceArn=manager_arn, logType=log_type)
        dest = logs.put_delivery_destination(name=f"{manager_id}-{kind}-dest", **destination)
        try:
            logs.create_delivery(
                deliverySourceName=source["deliverySource"]["name"],
                deliveryDestinationArn=dest["deliveryDestination"]["arn"],
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConflictException":  # already delivering
                raise
        print(f"{manager_id}: {kind} -> {'X-Ray' if kind == 'traces' else log_group}")
    return log_group


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manager-arn", action="append", required=True, help="Payment manager ARN (repeat for each)")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--retention-days", type=int, default=30)
    args = parser.parse_args()

    account = boto3.client("sts", region_name=args.region).get_caller_identity()["Account"]
    enable_transaction_search(args.region, account)
    for arn in args.manager_arn:
        deliver_payment_logs_and_traces(arn, args.region, account, args.retention_days)


if __name__ == "__main__":
    main()
