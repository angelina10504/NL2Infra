import os
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

class GitOpsService:
    def __init__(self, token: Optional[str] = None, repo_name: Optional[str] = None):
        self.token = token or os.environ.get("GITHUB_TOKEN")
        self.repo_name = repo_name or os.environ.get("GITHUB_REPO") or os.environ.get("MANIFESTS_REPO", "nl2infra/nl2infra-manifests")

    def commit_and_pr(self, files: Dict[str, str], request_id: str, environment: str = "dev") -> str:
        """
        Commits generated manifests to a Git branch and opens a Pull Request using PyGithub.
        If GITHUB_TOKEN is not configured, it writes files locally to a manifests directory
        and returns a simulated PR URL with instructions.
        """
        if not self.token or self.token.strip() in ("", "your_github_token_here"):
            return self._local_fallback(files, request_id, environment)

        try:
            from github import Github, GithubException
        except ImportError:
            logger.warning("PyGithub is not installed. Falling back to local manifest storage.")
            return self._local_fallback(files, request_id, environment)

        try:
            gh = Github(self.token)
            repo = gh.get_repo(self.repo_name)
            default_branch = repo.default_branch or "main"
            branch_name = f"nl2infra/{request_id}"

            # 1. Create or get branch reference
            try:
                ref = repo.get_git_ref(f"heads/{branch_name}")
                logger.info(f"Branch {branch_name} already exists.")
            except GithubException:
                base_ref = repo.get_git_ref(f"heads/{default_branch}")
                repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_ref.object.sha)
                logger.info(f"Created branch {branch_name} from {default_branch}.")

            # 2. Commit each manifest to the branch
            for filename, content in files.items():
                file_path = f"{environment}/{filename}"
                commit_msg = f"Add/update {filename} for request {request_id}"
                try:
                    existing_file = repo.get_contents(file_path, ref=branch_name)
                    repo.update_file(
                        path=file_path,
                        message=commit_msg,
                        content=content,
                        sha=existing_file.sha,
                        branch=branch_name
                    )
                except GithubException:
                    repo.create_file(
                        path=file_path,
                        message=commit_msg,
                        content=content,
                        branch=branch_name
                    )

            # 3. Create or reuse Pull Request
            head_query = f"{repo.owner.login}:{branch_name}"
            open_prs = repo.get_pulls(state="open", head=head_query, base=default_branch)
            if open_prs.totalCount > 0:
                pr = open_prs[0]
                logger.info(f"Existing PR found: {pr.html_url}")
                return pr.html_url

            manifest_list = "\n".join(f"- `{k}`" for k in files.keys())
            pr_body = (
                f"## NL2Infra Automated Infrastructure Pull Request\n\n"
                f"- **Request ID**: `{request_id}`\n"
                f"- **Target Environment**: `{environment}`\n"
                f"- **Files Generated**:\n{manifest_list}\n\n"
                f"*All files passed the Gauntlet security and policy checks (Checkov, Conftest/OPA, Kubectl dry-run).* "
                f"Please review and merge to deploy via ArgoCD."
            )

            pr = repo.create_pull(
                title=f"infra({environment}): deploy request {request_id}",
                body=pr_body,
                head=branch_name,
                base=default_branch
            )
            logger.info(f"Created new PR: {pr.html_url}")
            return pr.html_url

        except Exception as e:
            logger.error(f"GitHub GitOps operation failed: {e}. Falling back to local storage.")
            fallback_url = self._local_fallback(files, request_id, environment)
            return f"{fallback_url} (Note: GitHub push failed: {str(e)})"

    def _local_fallback(self, files: Dict[str, str], request_id: str, environment: str) -> str:
        """Stores manifests locally in ./nl2infra-manifests/<env>/<request_id> when no GitHub token is present."""
        local_dir = os.path.join(os.getcwd(), "nl2infra-manifests", environment, request_id)
        os.makedirs(local_dir, exist_ok=True)
        for fname, content in files.items():
            fpath = os.path.join(local_dir, os.path.basename(fname))
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)

        return f"file://{local_dir} (Local GitOps simulated: set GITHUB_TOKEN and GITHUB_REPO in .env for real PRs)"
