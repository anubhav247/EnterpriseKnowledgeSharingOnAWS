"""Custom resource of platform/eks-platform.yaml (inlined by platform/render.py).

Create / Update with a ReleaseUrl (quick-create link): start the runner's
"platform" build, which fetches and checks the release and publishes the
console. From CloudShell (platform/install.sh) ReleaseUrl is empty: the
script uploads the bundle and starts that build itself.
Delete: refuse while teams exist (their stacks would be left with no console
to remove them), else empty both buckets so CloudFormation can delete them.
"""

import json
import urllib.request

import boto3


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
        elif props.get("ReleaseUrl"):
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
