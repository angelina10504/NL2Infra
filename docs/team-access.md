# Team access

Every team member signs in with GitHub. The server looks up their numeric GitHub
ID in its membership database; a browser role picker or request header grants no
permissions. Uninvited users have no project access. There is no anonymous/demo
admin fallback when sign-in is unconfigured.

| Role | Allowed actions |
| --- | --- |
| Developer | View the project, submit infrastructure requests and approve/reject their own plans to generate pull requests. |
| Admin | All developer actions, plus merge or decline passed pull requests and manage members, roles and Slack identity links. |
| Viewer | View runs, generated files, validation results, notifications and history; ask questions about existing runs. |

Admins and developers can create requests and approve their own plans. Only
admins can merge or decline PRs. The existing input filter still limits developer
requests to dev/staging and allows admins to target production. No pipeline or
policy behavior was changed.

## First deployment

For a teammate demo using this Mac and the existing validation cluster, use
[Temporary team sharing](temporary-sharing.md).

1. Choose the real HTTPS address for the app and set `PUBLIC_BASE_URL` in `.env`.
   A public deployment must serve the dashboard as well as `/auth/*`, `/api/*` and
   `/events*`. The temporary tunnel configured to allow only
   `/slack/interactions` cannot serve team sign-in.
   For local testing while Slack uses that tunnel, set
   `NL2INFRA_AUTH_BASE_URL=http://localhost:8000` and register the OAuth app with
   homepage `http://localhost:8000` and callback `http://localhost:8000/auth/callback`.
   On deployment, clear this override or set it to the deployed HTTPS origin.
2. In GitHub, open **Settings → Developer settings → OAuth Apps → New OAuth App**.
   Set the homepage URL to your app's HTTPS origin and the authorization callback
   URL to that same origin followed by `/auth/callback`.
3. Put the OAuth app's client ID and client secret in `GITHUB_OAUTH_CLIENT_ID` and
   `GITHUB_OAUTH_CLIENT_SECRET`. This is a separate credential from `GITHUB_TOKEN`,
   which the server uses for GitOps. Sign-in requests no repository scopes and
   does not store users' GitHub access tokens.
4. Find the initial owner's numeric GitHub ID at
   `https://api.github.com/users/YOUR_GITHUB_USERNAME` (the `id` field).
   Put it in `NL2INFRA_BOOTSTRAP_ADMIN_IDS`. A comma-separated list seeds multiple
   admins when the auth database is empty. Subsequent role changes are managed
   in the app; changing this variable does not undo a revocation.
5. Persist `data/` (or the directory chosen in `NL2INFRA_AUTH_DB`) and the run-log
   directory across restarts. Start one app worker: pending pipeline plans live
   in the existing worker's memory. Back up the database and run logs together.
6. Restart the app and sign in from the configured public HTTPS origin. Cookies
   are Secure on HTTPS, HttpOnly and SameSite=Lax. A local HTTP development
   instance may use `http://localhost:8000` as its separate configured origin.

The GitHub flow checks a single-use, browser-bound OAuth state and PKCE verifier.
Sessions expire after 12 hours and roles are re-read for each request. Mutation
endpoints require a CSRF token, and disabling a member also ends their SSE feed.
Avoid logging OAuth callback query strings in any reverse proxy or hosting layer.

## Invite the team

Sign in as an admin and open **About → Project members**. Enter a GitHub username
(resolved by GitHub to an immutable numeric ID) or numeric ID, choose a role and
save. Teammates then sign in with GitHub. There is no self-registration or
self-promotion. Edit an existing member to change their role or disable them;
existing sessions use the updated permission immediately. The last active admin
cannot be removed or demoted. Membership changes are recorded in the database.

## Slack reviews

In the same member form, an admin can link their **Slack workspace ID** (`T…`)
and **Slack member ID** (`U…` or `W…`). Get the member ID from their Slack profile's
**More → Copy member ID** menu. The workspace ID is the `T…` component in the
Slack browser URL, for example `app.slack.com/client/T…/C…`.

Both IDs must be set together. A Slack identity can belong to only one member.
Only linked, active admins may merge or decline. Slack usernames, channel
membership and workspace-admin status do not grant app-admin rights. An
unauthorized click receives a private permission message without changing the
shared buttons, the run or GitHub. Permission is checked again in the background.
The dashboard and Slack share the existing run approval record and file lock,
so competing clicks do not perform a second GitHub action. The record includes
the reviewer ID and source. Failed/uncertain actions require manual verification.

## Repository permissions

App permissions govern actions through NL2Infra. Also configure permissions and
branch protection/rulesets on the manifests repository to prevent developers from
merging directly on GitHub. Restrict updates to the target branch to the admin
team and the server's integration credential. Keep `GITHUB_TOKEN` server-side.
Do not give developers a bypass that lets them update the protected branch.

## Monitoring

`/metrics` remains a machine-readable scrape endpoint; keep monitoring services
on a private network. `/alerts/webhook` now requires an authenticated admin and
CSRF token, so an unauthenticated Alertmanager receiver must be adapted before
it can publish dashboard alerts. The dashboard's run notifications and Slack
delivery use the existing event path and continue normally.

Reference: [GitHub's OAuth authorization flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps).
