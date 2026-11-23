"""Where alarms are sent: one SNS topic with an email subscription.

Every CloudWatch alarm in the stack names this topic as its alarm action. The address comes
from stack config (`alarmEmail`); SNS emails it a confirmation link, and nothing is
delivered until the owner follows it.

The topic is encrypted with its own KMS key. CloudWatch cannot publish to a topic encrypted
with the AWS-managed SNS key, so the key is customer-managed and its policy lets CloudWatch
use it. Only CloudWatch alarms in this account may publish to the topic.
"""

from __future__ import annotations

import json

import pulumi
import pulumi_aws as aws

CLOUDWATCH = "cloudwatch.amazonaws.com"


def _json(document: object) -> str:
    return json.dumps(document, separators=(",", ":"))


def key_policy(account_id: str) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "AccountIamPolicies",
                    "Effect": "Allow",
                    "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
                    "Action": "kms:*",
                    "Resource": "*",
                },
                {
                    # AWS's documented statement for alarms that publish to an encrypted topic.
                    "Sid": "CloudWatchAlarmsPublish",
                    "Effect": "Allow",
                    "Principal": {"Service": CLOUDWATCH},
                    "Action": ["kms:Decrypt", "kms:GenerateDataKey*"],
                    "Resource": "*",
                },
            ],
        }
    )


def topic_policy(*, topic: str, region: str, account_id: str) -> str:
    return _json(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "CloudWatchAlarmsInThisAccountPublish",
                    "Effect": "Allow",
                    "Principal": {"Service": CLOUDWATCH},
                    "Action": "sns:Publish",
                    "Resource": topic,
                    "Condition": {
                        "ArnLike": {
                            "aws:SourceArn": f"arn:aws:cloudwatch:{region}:{account_id}:alarm:*"
                        },
                        "StringEquals": {"aws:SourceAccount": account_id},
                    },
                },
                {
                    "Sid": "DenyInsecureTransport",
                    "Effect": "Deny",
                    "Principal": "*",
                    "Action": "sns:Publish",
                    "Resource": topic,
                    "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                },
            ],
        }
    )


class Alerts(pulumi.ComponentResource):
    """The alarm topic. Pass `topic.arn` as an alarm's `alarm_actions`."""

    def __init__(
        self,
        name: str,
        *,
        email: str,
        region: str,
        account_id: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("assay:observability:Alerts", name, None, opts)
        child = pulumi.ResourceOptions(parent=self)
        account = pulumi.Output.from_input(account_id)

        self.key = aws.kms.Key(
            f"{name}-key",
            description=f"{name}: alarm notifications",
            enable_key_rotation=True,
            rotation_period_in_days=365,
            deletion_window_in_days=30,
            policy=account.apply(key_policy),
            opts=child,
        )
        aws.kms.Alias(
            f"{name}-key-alias",
            name=f"alias/{name}",
            target_key_id=self.key.key_id,
            opts=child,
        )
        self.topic = aws.sns.Topic(
            f"{name}-topic",
            name=name,
            display_name="Assay Insights alarms",
            kms_master_key_id=self.key.key_id,
            opts=child,
        )
        aws.sns.TopicPolicy(
            f"{name}-topic-policy",
            arn=self.topic.arn,
            policy=pulumi.Output.all(topic=self.topic.arn, account_id=account).apply(
                lambda a: topic_policy(region=region, **a)
            ),
            opts=child,
        )
        # Stays "pending confirmation" until the owner follows the link SNS emails.
        self.subscription = aws.sns.TopicSubscription(
            f"{name}-email",
            topic=self.topic.arn,
            protocol="email",
            endpoint=email,
            opts=child,
        )
        self.register_outputs({"topic_arn": self.topic.arn})
