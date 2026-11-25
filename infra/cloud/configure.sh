#!/usr/bin/env bash
# Run on the VM as root, in your own terminal. Asks for the settings and secrets and writes them
# to files only root (and the ngrok service user) can read. Secret input is hidden and never
# printed. Press Enter to keep the current value.
set -euo pipefail
cd "$(dirname "$0")/../.."
env_file=.env
ngrok_dir=/etc/nl2infra
umask 077
[ -f "$env_file" ] || : > "$env_file"

current() { sed -n "s/^$1=//p" "$env_file" | head -1; }
put() {  # NAME VALUE: replace the line without putting the value on any command line
  local tmp; tmp=$(mktemp)
  grep -v "^$1=" "$env_file" > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  cat "$tmp" > "$env_file"; rm -f "$tmp"
}
ask() {  # NAME PROMPT [secret]
  local value hint=""
  [ -n "$(current "$1")" ] && hint=" [already set, Enter keeps it]"
  if [ "${3:-}" = secret ]; then read -rsp "$2$hint: " value; echo; else read -rp "$2$hint: " value; fi
  if [ -n "$value" ]; then put "$1" "$value"; fi
}

ask PUBLIC_BASE_URL "Public HTTPS URL, like https://name.ngrok-free.dev"
ask MANIFESTS_REPO "Manifests repository, OWNER/REPO"
ask GITHUB_TOKEN "GitHub token for the manifests repository" secret
ask GITHUB_OAUTH_CLIENT_ID "GitHub OAuth client ID"
ask GITHUB_OAUTH_CLIENT_SECRET "GitHub OAuth client secret" secret
ask NL2INFRA_BOOTSTRAP_ADMIN_IDS "Admin GitHub numeric user ID"

url=$(current PUBLIC_BASE_URL); url=${url%/}
[[ $url =~ ^https://[A-Za-z0-9.-]+$ ]] || { echo "PUBLIC_BASE_URL must be https://host" >&2; exit 1; }
put PUBLIC_BASE_URL "$url"
put NL2INFRA_AUTH_BASE_URL "$url"
put NL2INFRA_KUBECONFIG "$PWD/secrets/app.kubeconfig"
put NL2INFRA_DEPLOY_OBSERVER true
[ -n "$(current MODEL_NAME)" ] || put MODEL_NAME openai/gpt-oss-120b
# Real model calls stay off until they are switched on deliberately.
[ -n "$(current NL2INFRA_MOCK_MODE)" ] || put NL2INFRA_MOCK_MODE true
chmod 0600 "$env_file"

id nl2infra-ngrok >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin nl2infra-ngrok
install -d -m 0755 "$ngrok_dir"
hint=""; [ -s "$ngrok_dir/ngrok.yml" ] && hint=" [already set, Enter keeps it]"
read -rsp "ngrok authtoken$hint: " token; echo
if [ -n "$token" ]; then
  printf 'version: "3"\nagent:\n  authtoken: %s\n' "$token" > "$ngrok_dir/ngrok.yml"
fi
[ -s "$ngrok_dir/ngrok.yml" ] || { echo "an ngrok authtoken is required" >&2; exit 1; }
chown nl2infra-ngrok "$ngrok_dir/ngrok.yml"; chmod 0400 "$ngrok_dir/ngrok.yml"
printf 'NGROK_URL=%s\n' "$url" > "$ngrok_dir/ngrok.env"
chmod 0644 "$ngrok_dir/ngrok.env"
echo "CONFIGURE_OK. OAuth callback URL: $url/auth/callback"
