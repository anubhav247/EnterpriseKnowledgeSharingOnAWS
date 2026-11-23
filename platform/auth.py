"""One-time AWS IAM sign-in links for the EKS console.

POST /auth/mint is an API Gateway HTTP API route with AWS_IAM auth. It stores
one random code for five minutes. POST /auth/exchange consumes that code and
returns Cognito tokens accepted by the console API's existing JWT authorizer.
"""

import hashlib
import json
import os
import re
import secrets
import time

import boto3
from botocore.exceptions import ClientError

cognito = boto3.client("cognito-idp")
dynamodb = boto3.client("dynamodb")
TABLE = os.environ["TABLE"]
USER_POOL_ID = os.environ["USER_POOL_ID"]
CLIENT_ID = os.environ["CLIENT_ID"]
CONSOLE_URL = os.environ["CONSOLE_URL"].rstrip("/") + "/"
ALLOWED = {p for p in os.environ.get("CONSOLE_ADMIN_PRINCIPALS", "").split(",") if p}
ASSUMED_ROLE = re.compile(r"^arn:([^:]+):sts::([0-9]{12}):assumed-role/(.+)/[^/]+$")
CODE = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
TTL_SECONDS = 300


def reply(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body),
    }


def log(action, principal, outcome):
    print(
        json.dumps(
            {"action": action, "principal": principal, "time": int(time.time()), "outcome": outcome}
        )
    )


def caller(event):
    arn = event.get("requestContext", {}).get("authorizer", {}).get("iam", {}).get("userArn", "")
    match = ASSUMED_ROLE.fullmatch(arn)
    if match:
        return f"arn:{match[1]}:iam::{match[2]}:role/{match[3]}"
    return arn


def mint(event):
    principal = caller(event)
    if not principal:
        log("mint", "unknown", "refused")
        return reply(403, {"error": "AWS IAM identity missing"})
    if ALLOWED and principal not in ALLOWED:
        log("mint", principal, "refused")
        return reply(403, {"error": "AWS IAM identity is not a console admin"})
    code = secrets.token_urlsafe(32)
    expires = int(time.time()) + TTL_SECONDS
    dynamodb.put_item(
        TableName=TABLE,
        Item={
            "code": {"S": code},
            "principal": {"S": principal},
            "expires": {"N": str(expires)},
        },
        ConditionExpression="attribute_not_exists(#code)",
        ExpressionAttributeNames={"#code": "code"},
    )
    log("mint", principal, "ok")
    return reply(200, {"url": f"{CONSOLE_URL}#code={code}", "expiresIn": TTL_SECONDS})


def consume(code):
    now = int(time.time())
    try:
        result = dynamodb.delete_item(
            TableName=TABLE,
            Key={"code": {"S": code}},
            ConditionExpression="attribute_exists(#code) AND #expires >= :now",
            ExpressionAttributeNames={"#code": "code", "#expires": "expires"},
            ExpressionAttributeValues={":now": {"N": str(now)}},
            ReturnValues="ALL_OLD",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    item = result.get("Attributes") or {}
    return item.get("principal", {}).get("S")


def tokens(principal):
    digest = hashlib.sha256(principal.encode()).hexdigest()[:32]
    username = f"iam-{digest}@console.invalid"
    password = "Aa1!" + secrets.token_urlsafe(24)
    attrs = [
        {"Name": "email", "Value": username},
        {"Name": "email_verified", "Value": "true"},
        {"Name": "name", "Value": principal},
    ]
    try:
        cognito.admin_create_user(
            UserPoolId=USER_POOL_ID,
            Username=username,
            UserAttributes=attrs,
            MessageAction="SUPPRESS",
        )
    except cognito.exceptions.UsernameExistsException:
        cognito.admin_update_user_attributes(
            UserPoolId=USER_POOL_ID, Username=username, UserAttributes=attrs
        )
    cognito.admin_set_user_password(
        UserPoolId=USER_POOL_ID, Username=username, Password=password, Permanent=True
    )
    result = cognito.admin_initiate_auth(
        UserPoolId=USER_POOL_ID,
        ClientId=CLIENT_ID,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )["AuthenticationResult"]
    return {
        "idToken": result["IdToken"],
        "accessToken": result["AccessToken"],
        "expiresIn": result.get("ExpiresIn", 3600),
        "principal": principal,
    }


def exchange(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except (TypeError, ValueError):
        body = {}
    code = body.get("code", "") if isinstance(body, dict) else ""
    if not isinstance(code, str) or not CODE.fullmatch(code):
        log("exchange", "unknown", "refused")
        return reply(400, {"error": "invalid or expired code"})
    principal = consume(code)
    if not principal:
        log("exchange", "unknown", "refused")
        return reply(400, {"error": "invalid or expired code"})
    result = tokens(principal)
    log("exchange", principal, "ok")
    return reply(200, result)


def handler(event, _context):
    if event.get("routeKey") == "POST /auth/mint":
        return mint(event)
    if event.get("routeKey") == "POST /auth/exchange":
        return exchange(event)
    return reply(404, {"error": "no such route"})
