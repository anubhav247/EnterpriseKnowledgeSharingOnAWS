"""Platform monitoring: alarms, an alert topic, log retention, a dashboard.

Uses only metrics AWS publishes (namespace AWS/Bedrock-AgentCore) plus one
metric this stack publishes itself, the result of the latest ingestion job,
because Bedrock publishes no ingestion metric. Nothing here reads query text
or document content.
"""

from __future__ import annotations

from dataclasses import dataclass

from aws_cdk import (
    Aws,
    CfnOutput,
    CfnResource,
    Duration,
    RemovalPolicy,
    TagManager,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
)
from constructs import Construct

AGENTCORE = "AWS/Bedrock-AgentCore"
CUSTOM_NAMESPACE = "KbMcp"
FIVE_MIN = Duration.minutes(5)


@dataclass(frozen=True)
class MonitoringSettings:
    # False: no alarms, alert topic, custom metric or dashboard (saves their
    # fixed monthly cost). Log retention and usage logging stay.
    enabled: bool = True
    alert_email: str = ""
    log_retention_days: int = 90
    # Refused sign-ins (401 + 403) per 5 minutes before alerting. Normal use
    # produces a few: a client's first call before it has a token is a 401.
    auth_failure_threshold: int = 50
    # Alert when no ingestion has succeeded for this many days. 0 = off,
    # because content that rarely changes is normal for many domains.
    stale_after_days: int = 0


_INGESTION_CHECK = '''
import os, datetime, boto3

agent = boto3.client("bedrock-agent")
cw = boto3.client("cloudwatch")
KB, DS = os.environ["KB_ID"], os.environ["DS_ID"]

def handler(event, context):
    jobs = agent.list_ingestion_jobs(
        knowledgeBaseId=KB, dataSourceId=DS, maxResults=20,
        sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
    ).get("ingestionJobSummaries", [])
    finished = [j for j in jobs if j["status"] in ("COMPLETE", "FAILED", "STOPPED")]
    latest = finished[0] if finished else None
    failed = 0
    if latest:
        docs_failed = (latest.get("statistics") or {}).get("numberOfDocumentsFailed", 0)
        failed = int(latest["status"] != "COMPLETE" or docs_failed > 0)
    ok = [j for j in finished if j["status"] == "COMPLETE"
          and not (j.get("statistics") or {}).get("numberOfDocumentsFailed")]
    now = datetime.datetime.now(datetime.timezone.utc)
    hours = (now - ok[0]["updatedAt"]).total_seconds() / 3600 if ok else 0
    dims = [{"Name": "KnowledgeBaseId", "Value": KB}]
    cw.put_metric_data(Namespace="%s", MetricData=[
        {"MetricName": "LatestIngestionFailed", "Dimensions": dims, "Value": failed},
        {"MetricName": "HoursSinceSuccessfulIngestion", "Dimensions": dims, "Value": hours},
    ])
    return {"failed": failed, "hours_since_success": round(hours, 1),
            "latest": latest and latest["ingestionJobId"]}
''' % CUSTOM_NAMESPACE


class Monitoring(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        prefix: str,
        settings: MonitoringSettings,
        knowledge_base_id: str,
        data_source_id: str,
        kb_arn: str,
        runtimes: dict[str, tuple[str, str, str]],  # name -> (arn, id, runtime name)
        gateway_arn: str | None,
    ) -> None:
        super().__init__(scope, construct_id)

        retention = _retention_enum(settings.log_retention_days)

        # The service creates each runtime log group on first start;
        # LogRetention sets retention whether or not it exists yet. Usage
        # events live there, so this stays when monitoring is off.
        for name, (_arn, runtime_id, _rn) in runtimes.items():
            logs.LogRetention(
                self, f"RuntimeLogRetention{name.title()}",
                log_group_name=f"/aws/bedrock-agentcore/runtimes/{runtime_id}-DEFAULT",
                retention=retention)
        if not settings.enabled:
            self.topic = None
            self.ingestion_check = None
            return

        # ------------------------------------------------------------- alerts
        topic = sns.Topic(self, "Alerts", display_name=f"{prefix} knowledge base alerts",
                          enforce_ssl=True)
        if settings.alert_email:
            topic.add_subscription(subs.EmailSubscription(settings.alert_email))
        action = cw_actions.SnsAction(topic)
        alarms: list[cw.Alarm] = []

        def alarm(id_: str, metric: cw.IMetric, threshold: float, description: str,
                  periods: int = 1, missing=cw.TreatMissingData.NOT_BREACHING) -> cw.Alarm:
            a = cw.Alarm(
                self, id_, alarm_name=f"{prefix}-{id_}", alarm_description=description,
                metric=metric, threshold=threshold, evaluation_periods=periods,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=missing)
            a.add_alarm_action(action)
            a.add_ok_action(action)
            alarms.append(a)
            return a

        # ----------------------------------------------------------- runtimes
        runtime_widgets: list[cw.IWidget] = []
        for name, (arn, _runtime_id, runtime_name) in runtimes.items():
            dims = {"Resource": arn, "Operation": "InvokeAgentRuntime",
                    "Name": f"{runtime_name}::DEFAULT"}

            def m(metric_name: str, stat: str = "Sum", dims=dims) -> cw.Metric:
                return cw.Metric(namespace=AGENTCORE, metric_name=metric_name,
                                 dimensions_map=dims, statistic=stat, period=FIVE_MIN)

            alarm(f"runtime-{name}-system-errors", m("SystemErrors"), 5,
                  f"MCP server '{name}' returned 5 or more server errors in 5 minutes. "
                  "Check its CloudWatch log group.")
            alarm(f"runtime-{name}-throttles", m("Throttles"), 10,
                  f"MCP server '{name}' was throttled 10 or more times in 5 minutes.")
            runtime_widgets += [
                cw.GraphWidget(title=f"Server '{name}': invocations and errors",
                               left=[m("Invocations"), m("SystemErrors"), m("UserErrors"),
                                     m("Throttles")], width=12),
                cw.GraphWidget(title=f"Server '{name}': latency (ms)",
                               left=[m("Latency", "Average"), m("Latency", "p90")], width=12),
            ]

        # ------------------------------------------------------------ gateway
        gateway_widgets: list[cw.IWidget] = []
        if gateway_arn:
            gdims = {"Resource": gateway_arn, "Operation": "InvokeGateway", "Protocol": "MCP"}

            def g(metric_name: str, stat: str = "Sum") -> cw.Metric:
                return cw.Metric(namespace=AGENTCORE, metric_name=metric_name,
                                 dimensions_map=gdims, statistic=stat, period=FIVE_MIN)

            def auth(exception: str) -> cw.Metric:
                return cw.Metric(namespace=AGENTCORE, metric_name="InboundAuthorizationFailure",
                                 dimensions_map={"ResourceId": gateway_arn,
                                                 "ExceptionType": exception},
                                 statistic="Sum", period=FIVE_MIN)

            refused = cw.MathExpression(
                expression="FILL(m401, 0) + FILL(m403, 0)", label="Refused sign-ins",
                using_metrics={"m401": auth("UnauthorizedInboundTokenException"),
                               "m403": auth("ForbiddenAccessInboundTokenException")},
                period=FIVE_MIN)
            accepted = cw.Metric(namespace=AGENTCORE, metric_name="InboundAuthorizationSuccess",
                                 dimensions_map={"ResourceId": gateway_arn},
                                 statistic="Sum", period=FIVE_MIN, label="Accepted tokens")

            alarm("gateway-system-errors", g("SystemErrors"), 5,
                  "The MCP gateway returned 5 or more server errors in 5 minutes.")
            alarm("gateway-throttles", g("Throttles"), 10,
                  "The MCP gateway throttled 10 or more requests in 5 minutes.")
            alarm("gateway-refused-sign-ins", refused, settings.auth_failure_threshold,
                  f"The gateway refused {settings.auth_failure_threshold} or more tokens in "
                  "5 minutes (missing, expired, wrong tenant or app, or tampered). A spike "
                  "can mean a misconfigured client or someone probing the endpoint.")
            gateway_widgets = [
                cw.GraphWidget(title="Gateway: requests and errors",
                               left=[g("Invocations"), g("SystemErrors"), g("UserErrors"),
                                     g("Throttles")], width=12),
                cw.GraphWidget(title="Gateway: sign-ins", left=[accepted, refused], width=12),
                cw.GraphWidget(title="Gateway: latency (ms)",
                               left=[g("Latency", "Average"), g("Latency", "p90")], width=12),
            ]

        # ---------------------------------------------------------- ingestion
        check = lambda_.Function(
            self, "IngestionCheck",
            description="Publishes the latest knowledge base ingestion result as a metric",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="index.handler",
            code=lambda_.Code.from_inline(_INGESTION_CHECK),
            timeout=Duration.seconds(30),
            memory_size=128,
            environment={"KB_ID": knowledge_base_id, "DS_ID": data_source_id},
            # Its log group: explicit_lambda_log_groups (app.py).
        )
        check.add_to_role_policy(iam.PolicyStatement(
            sid="ReadIngestionJobs", actions=["bedrock:ListIngestionJobs"],
            resources=[kb_arn, f"{kb_arn}/*"]))
        check.add_to_role_policy(iam.PolicyStatement(
            sid="PublishOwnMetricsOnly", actions=["cloudwatch:PutMetricData"], resources=["*"],
            conditions={"StringEquals": {"cloudwatch:namespace": CUSTOM_NAMESPACE}}))
        events.Rule(self, "IngestionCheckSchedule",
                    description="Check the latest ingestion job every 15 minutes",
                    schedule=events.Schedule.rate(Duration.minutes(15)),
                    targets=[targets.LambdaFunction(check)])

        kb_dims = {"KnowledgeBaseId": knowledge_base_id}
        ingestion_failed = cw.Metric(namespace=CUSTOM_NAMESPACE,
                                     metric_name="LatestIngestionFailed",
                                     dimensions_map=kb_dims, statistic="Maximum",
                                     period=Duration.minutes(15))
        hours_since = cw.Metric(namespace=CUSTOM_NAMESPACE,
                                metric_name="HoursSinceSuccessfulIngestion",
                                dimensions_map=kb_dims, statistic="Maximum",
                                period=Duration.hours(1))
        alarm("ingestion-failed", ingestion_failed, 1,
              "The latest knowledge base ingestion failed or skipped documents. Search may "
              "be serving old content. Check the job in the Bedrock console and the sync "
              "pipeline's verify step.")
        alarm("ingestion-check-broken", check.metric_errors(period=Duration.minutes(15)), 1,
              "The ingestion check Lambda is failing, so ingestion alarms cannot fire.")
        if settings.stale_after_days > 0:
            alarm("content-stale", hours_since, settings.stale_after_days * 24,
                  f"No ingestion has succeeded for {settings.stale_after_days} days.")

        # ---------------------------------------------------------- dashboard
        dashboard = cw.Dashboard(self, "Dashboard", dashboard_name=f"{prefix}-knowledge-base")
        dashboard.add_widgets(cw.AlarmStatusWidget(title="Alarms", alarms=alarms, width=24))
        if gateway_widgets:
            dashboard.add_widgets(*gateway_widgets)
        dashboard.add_widgets(*runtime_widgets)
        dashboard.add_widgets(
            cw.GraphWidget(title="Latest ingestion failed (1 = yes)", left=[ingestion_failed],
                           width=12),
            cw.GraphWidget(title="Hours since last successful ingestion", left=[hours_since],
                           width=12),
        )

        CfnOutput(scope, "AlertTopicArn", value=topic.topic_arn)
        CfnOutput(scope, "DashboardUrl",
                  value=f"https://{Aws.REGION}.console.aws.amazon.com/cloudwatch/home?"
                        f"region={Aws.REGION}#dashboards/dashboard/{prefix}-knowledge-base")
        self.topic = topic
        self.ingestion_check = check


def explicit_lambda_log_groups(stack: Construct, days: int, tags: dict[str, str]) -> int:
    """Give every Lambda function in the stack its own log group, made by the stack.

    Lambda otherwise makes /aws/lambda/<name> itself on first run: no
    retention, no tags, and left behind when the stack goes. That includes
    the functions CDK adds (custom resource providers, S3 auto-delete, log
    retention). Run it once the stack is built, before synth: the app's tags
    then reach these groups too. CDK's raw provider role and function (S3
    auto-delete) take no tags from Tags.of: they get `tags` here.
    Returns how many groups it made.
    """
    made = 0
    for node in list(stack.node.find_all()):
        if not isinstance(node, CfnResource):
            continue
        kind = node.cfn_resource_type
        if kind in ("AWS::Lambda::Function", "AWS::IAM::Role") and not TagManager.is_taggable(node):
            node.add_property_override("Tags", [{"Key": k, "Value": v} for k, v in tags.items()])
        if kind != "AWS::Lambda::Function":
            continue
        cfg = node.logging_config if isinstance(node, lambda_.CfnFunction) else None
        if cfg is not None and getattr(cfg, "log_group", None):
            continue
        scope, name = node.node.scope, f"{node.node.id}LogGroup"
        if not isinstance(scope, Construct) or scope.node.try_find_child(name):
            continue
        group = logs.LogGroup(scope, name,
                              retention=_retention_enum(days), removal_policy=RemovalPolicy.DESTROY)
        node.add_property_override("LoggingConfig.LogGroup", group.log_group_name)
        # The same encryption Lambda's own group had: CloudWatch's default.
        group.node.default_child.add_metadata(  # type: ignore[union-attr]
            "checkov",
            {"skip": [{"id": "CKV_AWS_158",
                       "comment": "function logs, CloudWatch default encryption"}]})
        made += 1
    return made


def _retention_enum(days: int) -> logs.RetentionDays:
    """CloudWatch accepts only specific retention values; round up to one."""
    R = logs.RetentionDays
    allowed = [(1, R.ONE_DAY), (3, R.THREE_DAYS), (5, R.FIVE_DAYS), (7, R.ONE_WEEK),
               (14, R.TWO_WEEKS), (30, R.ONE_MONTH), (60, R.TWO_MONTHS),
               (90, R.THREE_MONTHS), (120, R.FOUR_MONTHS), (150, R.FIVE_MONTHS),
               (180, R.SIX_MONTHS), (365, R.ONE_YEAR), (400, R.THIRTEEN_MONTHS),
               (545, R.EIGHTEEN_MONTHS), (731, R.TWO_YEARS), (1827, R.FIVE_YEARS),
               (3653, R.TEN_YEARS)]
    return next((r for d, r in allowed if d >= days), allowed[-1][1])
