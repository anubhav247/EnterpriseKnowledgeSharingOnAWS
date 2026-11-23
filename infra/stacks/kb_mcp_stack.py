"""Knowledge base + MCP servers + signed-in gateway, as one CDK stack.

Layout:

    S3 docs bucket            whole documents (knowledge-base/<source>/...)
                              and section chunks (chunks/<source>/...)
    S3 Vectors index          embeddings, written by the knowledge base
    Bedrock knowledge base    one data source over chunks/, chunkingStrategy NONE
    AgentCore Runtime x N     the MCP server, one per configured scope, IAM-only
    AgentCore Gateway         optional; validates OIDC tokens from any provider
                              (generic settings, identity/oidc.py) or AWS IAM
                              SigV4, and calls the runtimes with its own IAM role
    Cognito user pool         optional, for tests: makes the OIDC provider

With OIDC, developers hold no AWS credentials: they reach the gateway with a
token from their provider, and only the gateway's role may invoke the runtimes.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
from typing import cast
from dataclasses import dataclass

import datetime
import json

import jsii
from aws_cdk import (
    Aws,
    BundlingOptions,
    CfnOutput,
    CustomResource,
    Duration,
    Fn,
    ILocalBundling,
    RemovalPolicy,
    Stack,
    aws_bedrock as bedrock,
    aws_bedrockagentcore as agentcore,
    aws_ecr_assets as ecr_assets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_s3_assets as s3_assets,
    aws_s3vectors as s3vectors,
    aws_secretsmanager as secretsmanager,
    custom_resources as cr,
)
from constructs import Construct

from identity.oidc import NATIVE_CLIENT_CLAIM, OidcSettings

from .cognito_identity import CognitoTestIdentity
from .monitoring import Monitoring, MonitoringSettings

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

DOCS_PREFIX = "knowledge-base/"
CHUNK_PREFIX = "chunks/"
MCP_PREFIX = "mcp/"
# Caller id header the runtime passes on to the MCP server (usage events).
CALLER_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Caller-Id"


def runtime_log_group(runtime: agentcore.CfnRuntime) -> str:
    """Log group AgentCore creates for a runtime's DEFAULT endpoint."""
    return f"/aws/bedrock-agentcore/runtimes/{runtime.attr_agent_runtime_id}-DEFAULT"


# Gateway instructions used until a content repository publishes its own
# mcp/instructions.md. Domain teams replace this through their pipeline.
DEFAULT_GATEWAY_INSTRUCTIONS = (
    "This knowledge base is a search index. Use kb___search_knowledge_base "
    "first, then kb___retrieve_document on relevant results and answer only "
    "from the retrieved text. Cite the document_id behind each claim, copy "
    "figures, code and definitions exactly, and say so when the documents do "
    "not cover a question."
)


def _claim_equals(name: str, value: str) -> agentcore.CfnGateway.CustomClaimValidationTypeProperty:
    return agentcore.CfnGateway.CustomClaimValidationTypeProperty(
        inbound_token_claim_name=name,
        inbound_token_claim_value_type="STRING",
        authorizing_claim_match_value=agentcore.CfnGateway.AuthorizingClaimMatchValueTypeProperty(
            claim_match_operator="EQUALS",
            claim_match_value=agentcore.CfnGateway.ClaimMatchValueTypeProperty(
                match_value_string=value
            ),
        ),
    )


def jwt_authorizer(oidc: OidcSettings) -> agentcore.CfnGateway.AuthorizerConfigurationProperty:
    """CUSTOM_JWT authorizer from the generic settings only (no provider names).

    Audiences go to allowedAudience and scopes to allowedScopes: together they
    say the token was issued for THIS server, so any MCP client may call it.
    Clients are an optional pin: allowedClients when the provider puts the app
    in client_id (checked natively); otherwise the client claim (azp, cid, ...)
    is pinned with an EQUALS custom claim. Required claims are EQUALS custom
    claims too. Entra sets no scopes: see identity/entra/preset.py.
    """
    native = oidc.client_claim == NATIVE_CLIENT_CLAIM
    claims = [] if native else [_claim_equals(oidc.client_claim, c) for c in oidc.allowed_clients]
    claims += [_claim_equals(name, value) for name, value in oidc.required_claims]
    return agentcore.CfnGateway.AuthorizerConfigurationProperty(
        custom_jwt_authorizer=agentcore.CfnGateway.CustomJWTAuthorizerConfigurationProperty(
            discovery_url=oidc.discovery_url,
            allowed_audience=list(oidc.allowed_audiences) or None,
            allowed_clients=list(oidc.allowed_clients) if native and oidc.allowed_clients else None,
            allowed_scopes=list(oidc.allowed_scopes) or None,
            custom_claims=claims or None,
        )
    )


@jsii.implements(ILocalBundling)
class PipBundling:
    """Bundle pure-Python code without Docker: pip install the arm64 wheels, copy files.

    Returns False (CDK then falls back to Docker) when pip fails, for example
    when a requirement has no linux arm64 wheel.
    """

    def __init__(self, src: pathlib.Path, files: list[str], python_version: str) -> None:
        self.src = src
        self.files = files
        self.python_version = python_version

    def try_bundle(self, output_dir: str, options: object = None) -> bool:
        # --no-compile: no build-machine bytecode in the zip (AgentCore advice).
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            "--disable-pip-version-check",
            "--no-compile",
            "-r",
            str(self.src / "requirements.txt"),
            "--target",
            output_dir,
            "--platform",
            "manylinux2014_aarch64",
            "--implementation",
            "cp",
            "--python-version",
            self.python_version,
            "--only-binary=:all:",
        ]
        # Fixed argv, no shell; paths come from this repo, not from input.
        if subprocess.run(cmd, check=False).returncode != 0:  # noqa: S603
            return False
        for name in self.files:
            shutil.copy2(self.src / name, output_dir)
        return True


def pip_bundling(src: pathlib.Path, files: list[str], runtime: lambda_.Runtime) -> BundlingOptions:
    """Local pip bundling first; the Docker command below only runs if it fails."""
    version = runtime.name.removeprefix("python")
    return BundlingOptions(
        image=runtime.bundling_image,
        platform="linux/arm64",
        local=cast(ILocalBundling, PipBundling(src, files, version)),  # jsii.implements
        command=[
            "bash",
            "-c",
            "pip install -q -r requirements.txt -t /asset-output "
            f"&& cp {' '.join(files)} /asset-output/",
        ],
    )


# Every module of the MCP server goes into the code zip (as the Dockerfile COPYs them).
MCP_SERVER_FILES = sorted(p.name for p in (REPO_ROOT / "mcp_server").glob("*.py"))


@dataclass(frozen=True)
class McpServerSpec:
    """One MCP server: a name for clients and an optional source namespace."""

    name: str  # e.g. "general", "analytics"; used in resource and tool names
    scope: str = ""  # SOURCE_SCOPE; empty spans every source


@dataclass(frozen=True)
class AzureDevOpsSync:
    """Lets one Azure DevOps service connection sync one content source.

    The pipeline gets an OIDC token from Azure DevOps whose subject names the
    service connection (sc://<org>/<project>/<connection>). The token says
    nothing about the branch or the YAML, so "main only" and "approved template
    only" must be enforced by checks on that service connection.
    """

    org_id: str  # organization GUID, part of the issuer URL
    org_name: str
    project: str
    service_connection: str
    source_id: str  # namespace this pipeline may write

    @property
    def issuer_host(self) -> str:
        return f"vstoken.dev.azure.com/{self.org_id}"

    @property
    def subject(self) -> str:
        return f"sc://{self.org_name}/{self.project}/{self.service_connection}"


class KbMcpStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        prefix: str,
        servers: list[McpServerSpec],
        source_id: str = "",
        ado_sync: AzureDevOpsSync | None = None,
        embedding_model_id: str = "amazon.titan-embed-text-v2:0",
        embedding_dimensions: int = 1024,
        retain_data: bool = False,
        monitoring: MonitoringSettings | None = None,
        gateway_debug_errors: bool = False,
        gateway_auth: str = "none",
        oidc: OidcSettings | None = None,
        cognito_create: bool = False,
        runtime_artifact: str = "image",
        ci_trust_role_arn: str = "",
        ci_content_role_name: str = "",
        gateway_user_trust_account: str = "",
        usage_log_queries: bool = True,
        usage_no_answer_score: float = 0.7,
        server_name: str = "",
        extra_source_ids: list[str] | None = None,
        proposals: dict[str, dict[str, str]] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        removal = RemovalPolicy.RETAIN if retain_data else RemovalPolicy.DESTROY
        # Domain-owned instructions and prompts, compiled by the content pipeline.
        published_key = f"{MCP_PREFIX}{source_id or 'default'}/published.json"
        name_token = prefix.replace("-", "_")

        # ------------------------------------------------------------ storage
        # Server access logs record every request to the docs bucket (who wrote or
        # deleted which document, and when) without needing a CloudTrail trail.
        access_logs = s3.Bucket(
            self,
            "DocsAccessLogs",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            lifecycle_rules=[
                s3.LifecycleRule(
                    expiration=Duration.days(
                        (monitoring or MonitoringSettings()).log_retention_days
                    )
                )
            ],
            removal_policy=removal,
            auto_delete_objects=not retain_data,
        )

        docs_bucket = s3.Bucket(
            self,
            "DocsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=removal,
            auto_delete_objects=not retain_data,
            server_access_logs_bucket=access_logs,
            server_access_logs_prefix="docs-bucket/",
        )

        vector_bucket = s3vectors.CfnVectorBucket(
            self,
            "VectorBucket",
            vector_bucket_name=f"{prefix}-vectors-{Aws.ACCOUNT_ID}",
        )
        vector_bucket.apply_removal_policy(removal)

        vector_index = s3vectors.CfnIndex(
            self,
            "VectorIndex",
            vector_bucket_arn=vector_bucket.attr_vector_bucket_arn,
            index_name=f"{prefix}-index",
            data_type="float32",
            dimension=embedding_dimensions,
            distance_metric="cosine",
            # The knowledge base stores the chunk text and its own metadata on
            # each vector. Both exceed the filterable-metadata size limit.
            metadata_configuration=s3vectors.CfnIndex.MetadataConfigurationProperty(
                non_filterable_metadata_keys=[
                    "AMAZON_BEDROCK_TEXT",
                    "AMAZON_BEDROCK_METADATA",
                ]
            ),
        )
        vector_index.apply_removal_policy(removal)

        # ----------------------------------------------------- knowledge base
        embedding_model_arn = (
            f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}::foundation-model/{embedding_model_id}"
        )

        kb_role = iam.Role(
            self,
            "KnowledgeBaseRole",
            description="Bedrock knowledge base: embed chunks and manage vectors",
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}:"
                        f"{Aws.ACCOUNT_ID}:knowledge-base/*"
                    },
                },
            ),
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="EmbedWithOneModel",
                actions=["bedrock:InvokeModel"],
                resources=[embedding_model_arn],
            )
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadChunksOnly",
                actions=["s3:GetObject"],
                resources=[docs_bucket.arn_for_objects(f"{CHUNK_PREFIX}*")],
            )
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="ListChunksOnly",
                actions=["s3:ListBucket"],
                resources=[docs_bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": [f"{CHUNK_PREFIX}*"]}},
            )
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="ManageVectorsInOneIndex",
                actions=[
                    "s3vectors:GetIndex",
                    "s3vectors:QueryVectors",
                    "s3vectors:PutVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:DeleteVectors",
                    "s3vectors:ListVectors",
                ],
                resources=[vector_index.attr_index_arn],
            )
        )

        knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name=f"{prefix}-kb",
            description="Enterprise markdown knowledge base served over MCP",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=embedding_model_arn,
                    embedding_model_configuration=bedrock.CfnKnowledgeBase.EmbeddingModelConfigurationProperty(
                        bedrock_embedding_model_configuration=bedrock.CfnKnowledgeBase.BedrockEmbeddingModelConfigurationProperty(
                            dimensions=embedding_dimensions,
                            embedding_data_type="FLOAT32",
                        )
                    ),
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    index_arn=vector_index.attr_index_arn,
                ),
            ),
        )
        # The role's policy must exist before the KB validates access to it.
        knowledge_base.node.add_dependency(kb_role)

        data_source = bedrock.CfnDataSource(
            self,
            "ChunkDataSource",
            knowledge_base_id=knowledge_base.attr_knowledge_base_id,
            name="ast-chunks",
            description="One S3 object per document section, embedded as written",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=docs_bucket.bucket_arn,
                    inclusion_prefixes=[CHUNK_PREFIX],
                ),
            ),
            vector_ingestion_configuration=bedrock.CfnDataSource.VectorIngestionConfigurationProperty(
                chunking_configuration=bedrock.CfnDataSource.ChunkingConfigurationProperty(
                    chunking_strategy="NONE"
                )
            ),
            # RETAIN: deleting the data source never empties the vector store
            # itself. With DELETE, Bedrock could fail that step and leave the
            # stack DELETE_FAILED (platform-test, 2026-11-23); the vector index
            # and bucket go with the stack anyway (or stay with retainData).
            data_deletion_policy="RETAIN",
        )

        kb_arn = (
            f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
            f"knowledge-base/{knowledge_base.attr_knowledge_base_id}"
        )

        # ------------------------------------------------- MCP server artifact
        # "image": container built by Docker. "code": a zip built by pip with no
        # Docker (AgentCore direct code deployment), for runners without Docker.
        if runtime_artifact == "code":
            code = s3_assets.Asset(
                self,
                "McpServerCode",
                path=str(REPO_ROOT / "mcp_server"),
                bundling=pip_bundling(
                    REPO_ROOT / "mcp_server", MCP_SERVER_FILES, lambda_.Runtime.PYTHON_3_12
                ),
            )
            artifact = agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                code_configuration=agentcore.CfnRuntime.CodeConfigurationProperty(
                    code=agentcore.CfnRuntime.CodeProperty(
                        s3=agentcore.CfnRuntime.S3LocationProperty(
                            bucket=code.s3_bucket_name, prefix=code.s3_object_key
                        )
                    ),
                    runtime="PYTHON_3_12",
                    entry_point=["agentcore_app.py"],
                )
            )
        else:
            image = ecr_assets.DockerImageAsset(
                self,
                "McpServerImage",
                directory=str(REPO_ROOT / "mcp_server"),
                # AgentCore Runtime runs linux/arm64 only.
                platform=ecr_assets.Platform.LINUX_ARM64,
                exclude=["__pycache__", "*.pyc", "lambda_handler.py"],
            )
            artifact = agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=image.image_uri,
                )
            )

        # ------------------------------------------------------ runtime role
        runtime_role = iam.Role(
            self,
            "McpRuntimeRole",
            description="MCP server runtime: read-only retrieval, no model access",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:{Aws.PARTITION}:bedrock-agentcore:"
                        f"{Aws.REGION}:{Aws.ACCOUNT_ID}:*"
                    },
                },
            ),
        )
        if runtime_artifact == "code":
            code.grant_read(runtime_role)
        else:
            image.repository.grant_pull(runtime_role)
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrAuth",
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="Logs",
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                    "logs:DescribeLogGroups",
                ],
                resources=[
                    f"arn:{Aws.PARTITION}:logs:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
                    "log-group:/aws/bedrock-agentcore/*"
                ],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="Telemetry",
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "cloudwatch:PutMetricData",
                ],
                resources=["*"],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="RetrieveFromOneKnowledgeBase",
                actions=["bedrock:Retrieve"],
                resources=[kb_arn],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadSyncStatus",
                actions=[
                    "bedrock:ListDataSources",
                    "bedrock:ListIngestionJobs",
                    "bedrock:GetIngestionJob",
                    "bedrock:GetDataSource",
                    "bedrock:GetKnowledgeBase",
                ],
                resources=[kb_arn, f"{kb_arn}/*"],
            )
        )
        # Deliberately no bedrock:InvokeModel: the server retrieves, the caller
        # reasons. Read whole documents only; chunks are reached via Retrieve.
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadDocuments",
                actions=["s3:GetObject"],
                resources=[docs_bucket.arn_for_objects(f"{DOCS_PREFIX}*")],
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="ReadPublishedMcpContent",
                actions=["s3:GetObject"],
                resources=[docs_bucket.arn_for_objects(published_key)],
            )
        )
        # Without ListBucket a missing object reads as AccessDenied, not NoSuchKey.
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="ListPublishedMcpContent",
                actions=["s3:ListBucket"],
                resources=[docs_bucket.bucket_arn],
                conditions={"StringEquals": {"s3:prefix": published_key}},
            )
        )
        runtime_role.add_to_policy(
            iam.PolicyStatement(
                sid="ListDocuments",
                actions=["s3:ListBucket"],
                resources=[docs_bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": [f"{DOCS_PREFIX}*"]}},
            )
        )

        # propose_change (mcp_server/proposals.py): on the draft server only. The
        # bot token that opens PRs/MRs lives in this secret; the team sets its
        # value (docs/propose-change.md). The runtime may only read it.
        proposals_secret = None
        if proposals:
            proposals_secret = secretsmanager.Secret(
                self,
                "ProposalsToken",
                description=f"{prefix}: bot token for propose_change pull requests",
                removal_policy=removal,
            )
            proposals_secret.grant_read(runtime_role)
            CfnOutput(self, "ProposalsTokenSecretArn", value=proposals_secret.secret_arn)

        # ---------------------------------------------------------- runtimes
        runtimes: dict[str, agentcore.CfnRuntime] = {}
        for spec in servers:
            env = {
                "KB_ID": knowledge_base.attr_knowledge_base_id,
                "DOCS_BUCKET": docs_bucket.bucket_name,
                "DOCS_PREFIX": DOCS_PREFIX,
                "MCP_PUBLISHED_KEY": published_key,
                # serverName in deployment.json: the team's own name (legal-kb).
                "MCP_SERVER_NAME": server_name or f"{prefix}-{spec.name}",
                "LOG_LEVEL": "INFO",
                # Usage events (mcp_server/usage_log.py, docs/usage-analytics.md).
                "USAGE_LOG": "1",
                "USAGE_LOG_QUERIES": "1" if usage_log_queries else "0",
                "USAGE_NO_ANSWER_SCORE": str(usage_no_answer_score),
            }
            if spec.scope:
                env["SOURCE_SCOPE"] = spec.scope
            if proposals_secret is not None:
                env["PROPOSALS"] = json.dumps(proposals, sort_keys=True)
                env["PROPOSALS_TOKEN_SECRET"] = proposals_secret.secret_arn

            runtime = agentcore.CfnRuntime(
                self,
                f"McpRuntime{spec.name.title().replace('-', '')}",
                agent_runtime_name=f"{name_token}_{spec.name.replace('-', '_')}",
                description=f"MCP server over the knowledge base ({spec.scope or 'all sources'})",
                role_arn=runtime_role.role_arn,
                agent_runtime_artifact=artifact,
                network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                    network_mode="PUBLIC"
                ),
                protocol_configuration="MCP",
                environment_variables=env,
                # Opaque caller id for usage events (mcp_server/agentcore_app.py).
                request_header_configuration=agentcore.CfnRuntime.RequestHeaderConfigurationProperty(
                    request_header_allowlist=[CALLER_HEADER]
                ),
                # No authorizer_configuration: IAM SigV4 only. Developers come in
                # through the gateway, never directly.
            )
            runtime.node.add_dependency(runtime_role)
            runtimes[spec.name] = runtime

            CfnOutput(
                self,
                f"RuntimeArn{spec.name.title().replace('-', '')}",
                value=runtime.attr_agent_runtime_arn,
                description=f"AgentCore Runtime ARN, server '{spec.name}'",
            )
            CfnOutput(
                self,
                f"RuntimeLogGroup{spec.name.title().replace('-', '')}",
                value=runtime_log_group(runtime),
                description=f"CloudWatch log group (usage events), server '{spec.name}'",
            )

        # ----------------------------------------------------------- gateway
        gateway = None
        publisher = None
        if gateway_auth not in ("none", "iam", "oidc"):
            raise ValueError(f"gateway_auth must be none, iam or oidc, not {gateway_auth!r}")
        cognito_test = None
        if gateway_auth == "oidc" and cognito_create:
            cognito_test = CognitoTestIdentity(self, "CognitoTest", prefix=prefix, removal=removal)
            oidc = cognito_test.oidc
        if gateway_auth == "oidc" and oidc is None:
            raise ValueError("gateway_auth oidc needs oidc settings or cognito_create")
        if gateway_auth != "none":
            gateway, publisher = self._add_gateway(
                prefix,
                oidc if gateway_auth == "oidc" else None,
                runtimes,
                gateway_debug_errors,
                docs_bucket,
                published_key,
            )

        # -------------------------------------------------------- monitoring
        Monitoring(
            self,
            "Monitoring",
            prefix=prefix,
            settings=monitoring or MonitoringSettings(),
            knowledge_base_id=knowledge_base.attr_knowledge_base_id,
            data_source_id=data_source.attr_data_source_id,
            kb_arn=kb_arn,
            runtimes={
                name: (rt.attr_agent_runtime_arn, rt.attr_agent_runtime_id, rt.agent_runtime_name)
                for name, rt in runtimes.items()
            },
            gateway_arn=gateway.attr_gateway_arn if gateway is not None else None,
        )

        # ------------------------------------------------- domain-owner sync
        if ado_sync is not None:
            self._add_azure_devops_sync(ado_sync, docs_bucket, kb_arn, publisher)
        if ci_trust_role_arn:
            ci_role = self._add_ci_content_role(
                ci_content_role_name or f"{prefix}-ci-content",
                ci_trust_role_arn,
                source_id or "default",
                docs_bucket,
                kb_arn,
                publisher,
                runtimes,
            )
            if proposals_secret is not None:
                # The deploy workflow sets the token and proves propose_change
                # through the gateway (infra/propose_smoke.py).
                proposals_secret.grant_write(ci_role)
                if gateway is not None:
                    ci_role.add_to_policy(
                        iam.PolicyStatement(
                            sid="ProposeSmokeInvokeOwnGateway",
                            actions=["bedrock-agentcore:InvokeGateway"],
                            resources=[gateway.attr_gateway_arn],
                        )
                    )
            for src in extra_source_ids or []:
                # One more repo behind the same server: its own role, its own
                # namespace only (no smoke-test or usage rights).
                src_role = iam.Role(
                    self,
                    f"CiSourceRole{src.title().replace('-', '')}",
                    role_name=f"{ci_content_role_name or prefix + '-ci-content'}-{src}",
                    description=f"CI content load for source '{src}' only",
                    max_session_duration=Duration.hours(1),
                    assumed_by=iam.ArnPrincipal(ci_trust_role_arn).with_session_tags(),
                )
                self._grant_source_sync(src_role, src, docs_bucket, kb_arn, None)
            if cognito_test is not None:
                # The sign-in smoke test reads the test clients' secrets.
                ci_role.add_to_policy(
                    iam.PolicyStatement(
                        sid="SignInSmokeReadTestClients",
                        actions=["cognito-idp:DescribeUserPoolClient"],
                        resources=[cognito_test.pool.user_pool_arn],
                    )
                )
                # The Kiro sign-in test creates the demo user and a CI user,
                # sets their passwords and signs both in (infra/kiro_signin_test.py).
                ci_role.add_to_policy(
                    iam.PolicyStatement(
                        sid="KiroSignInTestUsers",
                        actions=[
                            "cognito-idp:AdminGetUser",
                            "cognito-idp:AdminCreateUser",
                            "cognito-idp:AdminSetUserPassword",
                            "cognito-idp:AdminInitiateAuth",
                        ],
                        resources=[cognito_test.pool.user_pool_arn],
                    )
                )
                cognito_test.demo_secret.grant_read(ci_role)
        if gateway_user_trust_account and gateway is not None:
            # Lets people who sign in to another account (e.g. tooling) call
            # this gateway from Kiro: a role profile with source_profile.
            user_role = iam.Role(
                self,
                "GatewayUserRole",
                role_name=f"{prefix}-gateway-user",
                description="Call this stack's MCP gateway (SigV4) and nothing else",
                max_session_duration=Duration.hours(1),
                assumed_by=iam.AccountPrincipal(gateway_user_trust_account).with_session_tags(),
            )
            user_role.add_to_policy(
                iam.PolicyStatement(
                    sid="InvokeOwnGateway",
                    actions=["bedrock-agentcore:InvokeGateway"],
                    resources=[gateway.attr_gateway_arn],
                )
            )
            CfnOutput(self, "GatewayUserRoleArn", value=user_role.role_arn)

        # ----------------------------------------------------------- outputs
        CfnOutput(self, "DocsBucketName", value=docs_bucket.bucket_name)
        CfnOutput(self, "KnowledgeBaseId", value=knowledge_base.attr_knowledge_base_id)
        CfnOutput(self, "DataSourceId", value=data_source.attr_data_source_id)
        CfnOutput(self, "DocsPrefix", value=DOCS_PREFIX)
        CfnOutput(self, "ChunkPrefix", value=CHUNK_PREFIX)

    # ------------------------------------------------------------------ helpers

    def _add_azure_devops_sync(
        self,
        ado: AzureDevOpsSync,
        docs_bucket: s3.Bucket,
        kb_arn: str,
        publisher: lambda_.Function | None = None,
    ) -> None:
        """OIDC trust for one Azure DevOps service connection, write-only to one source."""
        provider = iam.CfnOIDCProvider(
            self,
            "AzureDevOpsOidcProvider",
            url=f"https://{ado.issuer_host}",
            # Fixed audience for Azure DevOps workload identity tokens.
            client_id_list=["api://AzureADTokenExchange"],
        )

        role = iam.Role(
            self,
            "AzureDevOpsSyncRole",
            description=f"Sync source '{ado.source_id}' from {ado.subject}",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.FederatedPrincipal(
                provider.attr_arn,
                conditions={
                    "StringEquals": {
                        f"{ado.issuer_host}:aud": "api://AzureADTokenExchange",
                        f"{ado.issuer_host}:sub": ado.subject,
                    }
                },
                assume_role_action="sts:AssumeRoleWithWebIdentity",
            ),
        )

        self._grant_source_sync(role, ado.source_id, docs_bucket, kb_arn, publisher)
        CfnOutput(self, "AzureDevOpsSyncRoleArn", value=role.role_arn)
        CfnOutput(self, "AzureDevOpsSubject", value=ado.subject)

    def _add_ci_content_role(
        self,
        role_name: str,
        trust_role_arn: str,
        source_id: str,
        docs_bucket: s3.Bucket,
        kb_arn: str,
        publisher: lambda_.Function | None,
        runtimes: dict[str, agentcore.CfnRuntime],
    ) -> iam.Role:
        """Role the deploy workflow assumes after `cdk deploy`: load content, smoke test.

        Trusts one role (the GitHub OIDC deploy role in the tooling account).
        Same rights as a content pipeline for one source, plus invoking this
        stack's own runtimes for the smoke test.
        """
        role = iam.Role(
            self,
            "CiContentRole",
            role_name=role_name,
            description=f"CI content load and smoke test for source '{source_id}'",
            max_session_duration=Duration.hours(1),
            assumed_by=iam.ArnPrincipal(trust_role_arn).with_session_tags(),
        )
        self._grant_source_sync(role, source_id, docs_bucket, kb_arn, publisher)
        role.add_to_policy(
            iam.PolicyStatement(
                sid="SmokeTestOwnRuntimes",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[
                    a
                    for rt in runtimes.values()
                    for a in (
                        rt.attr_agent_runtime_arn,
                        f"{rt.attr_agent_runtime_arn}/runtime-endpoint/*",
                    )
                ],
            )
        )
        # Usage report (python -m reports.usage) after the smoke test.
        groups = [
            f"arn:{Aws.PARTITION}:logs:{Aws.REGION}:{Aws.ACCOUNT_ID}:"
            f"log-group:{runtime_log_group(rt)}"
            for rt in runtimes.values()
        ]
        role.add_to_policy(
            iam.PolicyStatement(
                sid="UsageReportReadRuntimeLogs",
                actions=["logs:StartQuery", "logs:FilterLogEvents"],
                resources=[a for g in groups for a in (g, f"{g}:*")],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="UsageReportQueryResults",
                # GetQueryResults takes a query id, not a resource: "*" only.
                actions=["logs:GetQueryResults"],
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="UsageReportReadDocs",
                actions=["s3:GetObject"],
                resources=[docs_bucket.arn_for_objects(f"{DOCS_PREFIX}*")],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="UsageReportListDocs",
                actions=["s3:ListBucket"],
                resources=[docs_bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": [f"{DOCS_PREFIX}*"]}},
            )
        )
        CfnOutput(self, "CiContentRoleArn", value=role.role_arn)
        return role

    def _grant_source_sync(
        self,
        role: iam.Role,
        src: str,
        docs_bucket: s3.Bucket,
        kb_arn: str,
        publisher: lambda_.Function | None,
    ) -> None:
        """Write one source's documents and chunks, and ingest them. Nothing else."""
        docs = f"{DOCS_PREFIX}{src}/"
        chunks = f"{CHUNK_PREFIX}{src}/"
        manifest = f"manifests/{src}-chunk-manifest.json"
        mcp_published = f"{MCP_PREFIX}{src}/published.json"
        role.add_to_policy(
            iam.PolicyStatement(
                sid="WriteOwnSourceOnly",
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                resources=[
                    docs_bucket.arn_for_objects(f"{docs}*"),
                    docs_bucket.arn_for_objects(f"{chunks}*"),
                    docs_bucket.arn_for_objects(manifest),
                    docs_bucket.arn_for_objects(mcp_published),
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="ListOwnSourceOnly",
                actions=["s3:ListBucket"],
                resources=[docs_bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": [f"{docs}*", f"{chunks}*", manifest]}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                sid="IngestOneKnowledgeBase",
                actions=[
                    "bedrock:StartIngestionJob",
                    "bedrock:GetIngestionJob",
                    "bedrock:ListIngestionJobs",
                ],
                resources=[kb_arn, f"{kb_arn}/*"],
            )
        )
        if publisher is not None:
            # The pipeline may ask the publisher to run, nothing more: it cannot
            # call the gateway API, so it cannot change who may sign in.
            publisher.grant_invoke(role)
        # No bedrock:InvokeModel, no Retrieve, nothing outside this source.

    def _add_gateway(
        self,
        prefix: str,
        oidc: OidcSettings | None,
        runtimes: dict[str, agentcore.CfnRuntime],
        debug_errors: bool,
        docs_bucket: s3.Bucket,
        published_key: str,
    ) -> tuple[agentcore.CfnGateway, lambda_.Function]:
        """MCP gateway in front of the IAM-only runtimes: OIDC JWT, or AWS IAM without oidc."""
        gateway_role = iam.Role(
            self,
            "GatewayRole",
            description="MCP gateway: invoke the knowledge base MCP runtimes only",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:{Aws.PARTITION}:bedrock-agentcore:"
                        f"{Aws.REGION}:{Aws.ACCOUNT_ID}:gateway/*"
                    },
                },
            ),
        )
        runtime_arns = [rt.attr_agent_runtime_arn for rt in runtimes.values()]
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeKnowledgeBaseRuntimes",
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=runtime_arns + [f"{arn}/*" for arn in runtime_arns],
            )
        )

        if oidc is not None:
            authorizer_type = "CUSTOM_JWT"
            description = f"{oidc.label} protected MCP endpoint for the knowledge base"
            authorizer_configuration = jwt_authorizer(oidc)
        else:
            # AWS_IAM: callers sign with SigV4 and need bedrock-agentcore:InvokeGateway.
            # No authorizer configuration for this type.
            authorizer_type = "AWS_IAM"
            description = "MCP endpoint for the knowledge base"
            authorizer_configuration = None

        gateway = agentcore.CfnGateway(
            self,
            "McpGateway",
            name=f"{prefix}-gateway",
            description=description,
            role_arn=gateway_role.role_arn,
            protocol_type="MCP",
            protocol_configuration=agentcore.CfnGateway.GatewayProtocolConfigurationProperty(
                mcp=agentcore.CfnGateway.MCPGatewayConfigurationProperty(
                    supported_versions=["2025-06-18", "2025-03-26"],
                    # No instructions here on purpose: domain teams own them.
                    # The publisher function below sets them from the content
                    # repository, and re-applies them after every deploy.
                )
            ),
            authorizer_type=authorizer_type,
            authorizer_configuration=authorizer_configuration,
            # DEBUG returns internal error detail to callers. Useful while setting
            # up; leave off once clients connect.
            exception_level="DEBUG" if debug_errors else None,
        )
        gateway.node.add_dependency(gateway_role)

        targets: list[agentcore.CfnGatewayTarget] = []
        for name, runtime in runtimes.items():
            # Runtime invocation URL: the ARN is URL-encoded into the path.
            escaped_arn = Fn.join(
                "",
                [
                    f"arn%3A{Aws.PARTITION}%3Abedrock-agentcore%3A",
                    Aws.REGION,
                    "%3A",
                    Aws.ACCOUNT_ID,
                    "%3Aruntime%2F",
                    runtime.attr_agent_runtime_id,
                ],
            )
            endpoint = Fn.join(
                "",
                [
                    f"https://bedrock-agentcore.{Aws.REGION}.amazonaws.com/runtimes/",
                    escaped_arn,
                    "/invocations?qualifier=DEFAULT",
                ],
            )
            target = agentcore.CfnGatewayTarget(
                self,
                f"GatewayTarget{name.title().replace('-', '')}",
                gateway_identifier=gateway.attr_gateway_identifier,
                name=name,
                description=f"Knowledge base MCP server '{name}'",
                target_configuration=agentcore.CfnGatewayTarget.TargetConfigurationProperty(
                    mcp=agentcore.CfnGatewayTarget.McpTargetConfigurationProperty(
                        mcp_server=agentcore.CfnGatewayTarget.McpServerTargetConfigurationProperty(
                            endpoint=endpoint,
                        )
                    )
                ),
                credential_provider_configurations=[
                    agentcore.CfnGatewayTarget.CredentialProviderConfigurationProperty(
                        credential_provider_type="GATEWAY_IAM_ROLE",
                        credential_provider=agentcore.CfnGatewayTarget.CredentialProviderProperty(
                            iam_credential_provider=agentcore.CfnGatewayTarget.IamCredentialProviderProperty(
                                service="bedrock-agentcore",
                                region=Aws.REGION,
                            )
                        ),
                    )
                ],
            )
            target.node.add_dependency(runtime)
            targets.append(target)

        publisher = self._add_publisher(gateway, gateway_role, docs_bucket, published_key, targets)

        CfnOutput(self, "GatewayUrl", value=gateway.attr_gateway_url)
        CfnOutput(self, "GatewayId", value=gateway.attr_gateway_identifier)
        return gateway, publisher

    def _add_publisher(
        self,
        gateway: agentcore.CfnGateway,
        gateway_role: iam.Role,
        docs_bucket: s3.Bucket,
        published_key: str,
        targets: list[agentcore.CfnGatewayTarget],
    ) -> lambda_.Function:
        """Platform-owned function that applies domain instructions to the gateway."""
        fn = lambda_.Function(
            self,
            "McpPublisher",
            description="Apply domain-published MCP instructions to the gateway and sync targets",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                str(REPO_ROOT / "infra" / "publisher"),
                bundling=pip_bundling(
                    REPO_ROOT / "infra" / "publisher", ["handler.py"], lambda_.Runtime.PYTHON_3_13
                ),
            ),
            timeout=Duration.minutes(10),
            memory_size=256,
            environment={
                "GATEWAY_ID": gateway.attr_gateway_identifier,
                "DOCS_BUCKET": docs_bucket.bucket_name,
                "MCP_PUBLISHED_KEY": published_key,
                "DEFAULT_INSTRUCTIONS": DEFAULT_GATEWAY_INSTRUCTIONS,
            },
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ManageOneGateway",
                actions=[
                    "bedrock-agentcore:GetGateway",
                    "bedrock-agentcore:UpdateGateway",
                    "bedrock-agentcore:ListGatewayTargets",
                    "bedrock-agentcore:GetGatewayTarget",
                    "bedrock-agentcore:SynchronizeGatewayTargets",
                ],
                resources=[gateway.attr_gateway_arn, f"{gateway.attr_gateway_arn}/*"],
            )
        )
        # UpdateGateway re-submits the gateway's existing role.
        gateway_role.grant_pass_role(fn.role)
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadPublishedInstructions",
                actions=["s3:GetObject"],
                resources=[docs_bucket.arn_for_objects(published_key)],
            )
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ListPublishedInstructions",
                actions=["s3:ListBucket"],
                resources=[docs_bucket.bucket_arn],
                conditions={"StringEquals": {"s3:prefix": published_key}},
            )
        )

        # Re-apply on every deploy: if CloudFormation updates the gateway, it
        # sends the template's configuration, which has no instructions.
        provider = cr.Provider(self, "McpPublisherProvider", on_event_handler=fn)
        invoke = CustomResource(
            self,
            "McpPublishOnDeploy",
            service_token=provider.service_token,
            # A new value each synth makes CloudFormation run it on every deploy.
            properties={"DeployedAt": datetime.datetime.now(datetime.timezone.utc).isoformat()},
        )
        for target in targets:
            invoke.node.add_dependency(target)

        CfnOutput(self, "McpPublisherFunctionName", value=fn.function_name)
        return fn
