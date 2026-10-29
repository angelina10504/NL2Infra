# Temporary team link

The app can be shared from this Mac through ngrok while keeping the existing
local server and its active runs running. It uses the same membership database,
saved run logs, model credentials, GitOps credential and validation cluster.
This is temporary hosting: the Mac, Docker/validation cluster, app and tunnel
must stay running and connected to the internet.

## Start the shared instance

Use the HTTPS hostname assigned by ngrok. Replace `YOUR-NGROK-HOST` in both
commands below with that hostname. In the project directory, run:

```bash
NL2INFRA_AUTH_BASE_URL=https://YOUR-NGROK-HOST ./.venv/bin/python -m uvicorn server:app --app-dir src/nl2infra --host 127.0.0.1 --port 8001
```

In a second terminal:

```bash
ngrok http 8001 --url https://YOUR-NGROK-HOST --traffic-policy-file infra/ngrok-team-policy.json --inspect=false
```

Only one ngrok tunnel can serve that hostname. Stop the earlier tunnel before
restarting it with this command. The public Slack interactions URL stays the
same when its hostname is reused. Keep `PUBLIC_BASE_URL` in `.env` set to that
HTTPS origin. The environment override above gives the shared process its
public sign-in callback while the server on port 8000 retains local sign-in.

The tunnel policy permits the dashboard, GitHub sign-in, protected project APIs,
notification feed and signed Slack interactions. It blocks other paths,
including metrics, monitoring webhooks and API documentation. Uninvited users
can see the sign-in page, but cannot read project data or submit changes.

## GitHub and team members

In the existing GitHub OAuth app, set the homepage to the shared HTTPS address
and add `https://YOUR-NGROK-HOST/auth/callback` as a redirect URI. Keep
`http://localhost:8000/auth/callback` if local sign-in is still needed.

From the shared address, sign in as the owner admin and use **About → Project
members** to invite teammates by GitHub username. Assign **Developer**. Their
GitHub account IDs are resolved and stored, so a browser-selected role or
changed display name cannot grant admin access.

Share the HTTPS link with them. A developer signs in, submits an infrastructure
request and approves their own plan. When the generated PR is ready, an admin
opens it from History and selects **Approve and merge**, or reviews it through
a linked admin Slack identity. Developers cannot merge through NL2Infra.
GitHub repository permissions independently govern merges directly on GitHub.

Opening the dashboard does not add a collaborator to the GitHub repository or
send an invitation message. Each teammate must sign in with the invited account.

## Stop sharing

Stop the ngrok process with Ctrl+C and stop the shared server on port 8001.
This leaves the local server on port 8000 and stored memberships in place. An
admin can disable a member in About; their existing session will lose access.
