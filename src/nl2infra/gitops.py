import os
import logging
from typing import Dict, Optional
from urllib.parse import urlparse

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

    def _pull_request(self, pr_url: str):
        """Resolve a PR URL only inside this service's configured GitHub repository."""
        if not self.token or not self.token.strip():
            raise RuntimeError("GITHUB_TOKEN is required for pull request actions")
        parsed = urlparse(pr_url)
        pieces = [part for part in parsed.path.strip("/").split("/") if part]
        if parsed.scheme != "https" or parsed.hostname != "github.com" or len(pieces) != 4 or pieces[2] != "pull":
            raise ValueError("invalid GitHub pull request URL")
        owner_repo = "/".join(pieces[:2])
        if owner_repo.casefold() != self.repo_name.casefold():
            raise ValueError("pull request is outside the configured manifests repository")
        try:
            number = int(pieces[3])
        except ValueError as error:
            raise ValueError("invalid GitHub pull request number") from error
        from github import Github
        repo = Github(self.token).get_repo(self.repo_name)
        return repo.get_pull(number)

    def merge_pull_request(self, pr_url: str) -> None:
        """Merge a reviewed PR; never silently fall back to a local simulation."""
        result = self._pull_request(pr_url).merge()
        if not getattr(result, "merged", False):
            raise RuntimeError(getattr(result, "message", "GitHub did not merge the pull request"))

    def close_pull_request(self, pr_url: str) -> None:
        """Close a declined PR in the configured manifests repository."""
        self._pull_request(pr_url).edit(state="closed")

    def pull_request_status(self, pr_url: str, filenames=()) -> dict:
        """Observed state of a PR, plus this request's manifests as they are at the merge commit.

        Read-only. `filenames` are the files the run generated; unchanged ones do not appear in
        the PR diff but are still part of what must be running.
        """
        pr = self._pull_request(pr_url)
        merged = bool(pr.merged)
        status = {
            "number": pr.number, "state": pr.state, "merged": merged,
            "head_ref": pr.head.ref, "base_ref": pr.base.ref,
            "default_branch": pr.base.repo.default_branch,
            "merge_commit_sha": pr.merge_commit_sha if merged else None,
            "merged_at": pr.merged_at.isoformat() if merged and pr.merged_at else None,
            "environment": None, "files": {},
        }
        if not merged:
            return status

        changed = [item.filename for item in pr.get_files() if item.status != "removed"]
        folders = {path.split("/", 1)[0] for path in changed if "/" in path}
        if len(folders) != 1 or any("/" not in path for path in changed):
            return status
        environment = folders.pop()
        status["environment"] = environment

        from github import UnknownObjectException
        repo = pr.base.repo
        paths = set(changed) | {f"{environment}/{name}" for name in filenames}
        for path in sorted(paths):
            if not path.endswith((".yaml", ".yml", ".json")):
                continue
            try:
                content = repo.get_contents(path, ref=status["merge_commit_sha"])
            except UnknownObjectException:
                continue  # removed during review
            status["files"][path] = content.decoded_content.decode("utf-8")
        return status
