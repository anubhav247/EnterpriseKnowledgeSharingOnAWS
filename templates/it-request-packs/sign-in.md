
## Sign-in: OIDC app and groups for team {{team}}

Any OpenID Connect provider works (Entra ID, Okta, Google, Keycloak, Ping,
...). This sheet says what we need, not where to click.

### Ticket text

Please create one OIDC application and two groups in our identity provider,
for the EKS knowledge base server of team {{team}}.

Application:

- Name: `{{appName}}`
- Type: public client (native / desktop app): no client secret,
  authorization code flow with PKCE (S256)
- Redirect URIs (loopback; one per MCP client: Kiro, Claude Code, VS Code).
  Any port on `localhost` and `127.0.0.1` should be accepted where the
  provider allows it:
{{redirectUris}}
- One API scope: `{{scope}}` ("Read the knowledge base"). Users may get it
  without a consent prompt.
- Access tokens: JWT, signed (RS256 or ES256), with `iss` = the issuer,
  `aud` = an audience for this app (often the client id), and the scope.
- Only assigned users and groups may sign in (assignment required).
- Owners: {{owner}}

Groups, both assigned to the application:

- `{{readers}}`: may ask the server. Members: {{readerMembers}}
- `{{publishers}}`: may also publish documents. Members: {{publisherMembers}}
- Owners of both groups: {{owner}}

### Values to send back

| Value | Example | Where to find it |
|---|---|---|
| issuer URL | `https://login.example.com/oauth2/default` | the `issuer` in the provider's `/.well-known/openid-configuration` |
| client id | `0oa1b2c3d4` | the application's client id (public, not a secret) |
| audience | often the client id | the `aud` the access token carries |
| scope value | `{{scope}}`, or the provider's full form | the string the token's `scope` / `scp` claim carries |

Never send a client secret: the app has none.

### How to verify

The team admin runs, from a checkout of the EKS repo:

```bash
scripts/add-team.sh --check-oidc --issuer <issuer URL> --client-id <client id> \
  --audience <audience> --scope <scope value>
```

It reads the discovery document and the signing keys (JWKS) and prints what
the gateway will enforce. With a test access token on stdin
(`--token -`) it also checks the token's `iss`, `aud` and scope. The token is
read, not stored or printed.
