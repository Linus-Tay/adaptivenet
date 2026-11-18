"""credential_process for profile adaptivenet-ml: IPv4-only SSO refresh + role creds.

This Mac's IPv6 path currently black-holes AWS endpoints, which makes the AWS CLI's
own SSO refresh hang. Reads/writes only the user's own ~/.aws/sso/cache token.
Never prints anything except the credential JSON the AWS CLI expects.
"""
import datetime as dt, hashlib, json, os, socket, sys
_orig = socket.getaddrinfo
socket.getaddrinfo = lambda h, p, family=0, *a, **k: _orig(h, p, socket.AF_INET, *a, **k)
import boto3
from botocore.config import Config
SESSION, REGION, ACCOUNT, ROLE = "adaptivenet", "ap-southeast-1", "468354413374", "AdaptiveNetSetupAdmin"
path = os.path.expanduser("~/.aws/sso/cache/" + hashlib.sha1(SESSION.encode()).hexdigest() + ".json")
tok = json.load(open(path, encoding="utf-8"))
cfg = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 3})
exp = dt.datetime.fromisoformat(tok["expiresAt"].replace("Z", "+00:00"))
if exp - dt.datetime.now(dt.timezone.utc) < dt.timedelta(minutes=20):
    r = boto3.client("sso-oidc", region_name=REGION, config=cfg).create_token(
        clientId=tok["clientId"], clientSecret=tok["clientSecret"], grantType="refresh_token",
        refreshToken=tok["refreshToken"])
    tok["accessToken"] = r["accessToken"]
    tok["refreshToken"] = r.get("refreshToken", tok["refreshToken"])
    tok["expiresAt"] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=r["expiresIn"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(tok, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
rc = boto3.client("sso", region_name=REGION, config=cfg).get_role_credentials(
    roleName=ROLE, accountId=ACCOUNT, accessToken=tok["accessToken"])["roleCredentials"]
print(json.dumps({"Version": 1, "AccessKeyId": rc["accessKeyId"], "SecretAccessKey": rc["secretAccessKey"],
                  "SessionToken": rc["sessionToken"],
                  "Expiration": dt.datetime.fromtimestamp(rc["expiration"] / 1000, dt.timezone.utc).isoformat()}))
