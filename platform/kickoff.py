"""Custom resource of platform/eks-platform.yaml (inlined by platform/render.py).

Create / Update: put setup.html in the console bucket. CloudFront shows it
for any missing object, so an early visit reads "being set up", not S3 XML.
With a ReleaseUrl (quick-create link, or platform/install.sh when the release
is published) also start the runner's "platform" build: it fetches and checks
the release, publishes the console and invites the first admin, so the stack
finishes the console even if CloudShell closes. Without one (a test bundle
that is not published) install.sh uploads the bundle and starts that build.
Delete: refuse while teams exist (their stacks would be left with no console
to remove them), else empty both buckets so CloudFormation can delete them.
"""

import json
import urllib.request

import boto3

SETUP_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="30"><title>EKS console: being set up</title></head>
<body style="font-family:sans-serif;max-width:40em;margin:4em auto">
<h1>The EKS console is being set up</h1>
<p>This takes about 10 minutes after the install starts. This page reloads by itself.</p>
<p>Still here after 20 minutes? Run the install command in AWS CloudShell again.</p>
</body></html>
"""


def send(event, ok, reason=""):
    body = {
        "Status": "SUCCESS" if ok else "FAILED",
        "Reason": (reason or "see the function's log")[:900],
        "PhysicalResourceId": event.get("PhysicalResourceId") or "eks-platform-kickoff",
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
    }
    req = urllib.request.Request(  # noqa: S310 - the presigned CloudFormation URL
        event["ResponseURL"],
        data=json.dumps(body).encode(),
        method="PUT",
        headers={"content-type": ""},
    )
    urllib.request.urlopen(req, timeout=30)  # noqa: S310 - the presigned CloudFormation URL


def teams(bucket):
    keys = [o.key for o in boto3.resource("s3").Bucket(bucket).objects.filter(Prefix="teams/")]
    return sorted({k.split("/")[1] for k in keys if k.endswith("/team.json")})


def handler(event, _context):
    print(json.dumps({k: event.get(k) for k in ("RequestType", "LogicalResourceId")}))
    props = event["ResourceProperties"]
    try:
        if event["RequestType"] == "Delete":
            left = teams(props["Bucket"])
            if left:
                return send(
                    event, False, "Delete these teams in the EKS console first: " + ", ".join(left)
                )
            for name in (props["Bucket"], props["ConsoleBucket"]):
                boto3.resource("s3").Bucket(name).object_versions.delete()
            return send(event, True)
        boto3.client("s3").put_object(
            Bucket=props["ConsoleBucket"],
            Key="setup.html",
            Body=SETUP_PAGE.encode(),
            ContentType="text/html",
            CacheControl="no-cache",
        )
        if props.get("ReleaseUrl"):
            boto3.client("codebuild").start_build(
                projectName=props["Project"],
                environmentVariablesOverride=[
                    {"name": "ACTION", "value": "platform", "type": "PLAINTEXT"}
                ],
            )
        return send(event, True)
    except Exception as exc:  # noqa: BLE001 - any failure must reach CloudFormation
        print(repr(exc))
        return send(event, False, str(exc))
