"""EKS console API (Lambda behind API Gateway, JWT sign-in). Inlined into
platform/eks-platform.yaml by platform/render.py; tests: tests/test_platform.py.

Routes: GET /teams, POST /teams, GET /teams/{team}, DELETE /teams/{team},
POST /teams/{team}/pipeline-key (an Azure DevOps team's pipeline key, shown once).
Each call is logged as one JSON line (who, what, which team). A create or
delete starts the CodeBuild runner (platform/runner.sh); this function only
records the request and reads status, so it needs no rights on team stacks.
"""

import json
import os
import re

import boto3

s3 = boto3.client("s3")
cb = boto3.client("codebuild")
sm = boto3.client("secretsmanager")
logs = boto3.client("logs")
BUCKET = os.environ["BUCKET"]
PROJECT = os.environ["PROJECT"]
ADMIN_CLAIM = os.environ.get("ADMIN_CLAIM", "")
TEAM = re.compile(r"^[a-z][a-z0-9-]{0,18}[a-z0-9]$")
RESERVED = ("console", "platform")


def reply(code, body):
    return {
        "statusCode": code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def get(key):
    try:
        return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None


def put(key, value):
    s3.put_object(
        Bucket=BUCKET, Key=key, Body=json.dumps(value).encode(), ContentType="application/json"
    )


def is_admin(claims):
    """ADMIN_CLAIM "name=value": the token must carry it (a group, a subject)."""
    if not ADMIN_CLAIM:
        return True
    name, _, value = ADMIN_CLAIM.partition("=")
    have = str(claims.get(name, ""))
    return have == value or value in have.strip("[]").split()


def start(team, action, who):
    env = [
        {"name": k, "value": v, "type": "PLAINTEXT"}
        for k, v in (("ACTION", action), ("TEAM", team))
    ]
    build = cb.start_build(projectName=PROJECT, environmentVariablesOverride=env)["build"]["id"]
    rec = {"team": team, "action": action, "build": build, "by": who, "status": "IN_PROGRESS"}
    put(f"teams/{team}/team.json", rec)
    return rec


def with_status(recs):
    builds = cb.batch_get_builds(ids=[r["build"] for r in recs])["builds"] if recs else []
    by_id = {b["id"]: b for b in builds}
    for r in recs:
        b = by_id.get(r["build"], {})
        r.update(
            status=b.get("buildStatus"), phase=b.get("currentPhase"), started=b.get("startTime")
        )
    return recs


def detail(rec):
    rec = with_status([rec])[0]
    b = cb.batch_get_builds(ids=[rec["build"]])["builds"][0].get("logs", {})
    if b.get("streamName"):
        ev = logs.get_log_events(
            logGroupName=b["groupName"], logStreamName=b["streamName"], limit=300
        )
        rec["log"] = [e["message"].rstrip() for e in ev["events"]]
    team = rec["team"]
    rec["result"] = get(f"teams/{team}/result.json")
    # The files the team commits (runner.sh repo-files.zip), as a 15-minute link.
    if rec["result"] and rec["result"].get("host") in ("ado", "github", "gitlab"):
        rec["repoFiles"] = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": f"teams/{team}/repo-files.zip"},
            ExpiresIn=900,
        )
    # The filled Azure DevOps setup script (runner.sh), as its own download.
    if rec["result"] and rec["result"].get("adoSetup"):
        rec["adoSetupScript"] = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": BUCKET,
                "Key": f"teams/{team}/ado-setup.sh",
                "ResponseContentDisposition": f'attachment; filename="ado-setup-{team}.sh"',
            },
            ExpiresIn=900,
        )
        rec["pipelineKey"] = {k: v for k, v in (get(f"teams/{team}/key.json") or {}).items()}
    return rec


def pipeline_key(team, who):
    """The pipeline key's secret, once: after that only a new team build makes another."""
    key = get(f"teams/{team}/key.json")
    if not key:
        return reply(404, {"error": f"{team} has no pipeline key (not an Azure DevOps team)"})
    if key.get("shown"):
        return reply(409, {"error": "the key was shown once already", "keyId": key["keyId"]})
    out = (get(f"teams/{team}/outputs.json") or {}).values()
    arn = next(
        (o["AzureDevOpsSyncKeySecretArn"] for o in out if "AzureDevOpsSyncKeySecretArn" in o), ""
    )
    if not arn:
        return reply(404, {"error": f"{team} has no pipeline key secret"})
    secret = sm.get_secret_value(SecretId=arn)["SecretString"]
    put(f"teams/{team}/key.json", {**key, "shown": True, "shownBy": who})
    return reply(200, {"keyId": key["keyId"], "secretAccessKey": secret})


def handler(event, _context):
    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    who = claims.get("email") or claims.get("sub")
    route = event["routeKey"]
    team = (event.get("pathParameters") or {}).get("team", "")
    body = event.get("body") or "{}"
    if route == "POST /teams":
        try:
            req = json.loads(body)
        except ValueError:
            req = {}
        team = str(req.get("team", "")) if isinstance(req, dict) else ""
    admin = is_admin(claims)
    print(json.dumps({"route": route, "team": team, "who": who, "admin": admin}))
    if not admin:
        return reply(403, {"error": "not an EKS admin"})
    if route == "GET /teams":
        keys = s3.list_objects_v2(Bucket=BUCKET, Prefix="teams/").get("Contents", [])
        recs = [get(k["Key"]) for k in keys if k["Key"].endswith("/team.json")]
        return reply(200, with_status([r for r in recs if r]))
    if not TEAM.match(team) or team.startswith(RESERVED):
        return reply(
            400, {"error": "team: 2-20 lowercase letters, digits and -, starts with a letter"}
        )
    rec = get(f"teams/{team}/team.json")
    if rec and with_status([rec])[0]["status"] == "IN_PROGRESS" and route != "GET /teams/{team}":
        return reply(409, {"error": f"a build for {team} is running"})
    if route == "POST /teams":
        if len(body) > 8000:
            return reply(400, {"error": "request too large"})
        put(f"teams/{team}/request.json", req)
        return reply(202, start(team, "create", who))
    if not rec:
        return reply(404, {"error": f"no team {team}"})
    if route == "GET /teams/{team}":
        return reply(200, detail(rec))
    if route == "POST /teams/{team}/pipeline-key":
        return pipeline_key(team, who)
    if route == "DELETE /teams/{team}":
        return reply(202, start(team, "delete", who))
    return reply(404, {"error": "no such route"})
