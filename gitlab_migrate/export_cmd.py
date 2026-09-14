from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterable, Optional

from .client import GitLabClient, GitLabError
from .plan import build_export_actions, inventory_from_meta, print_dry_run_report


def load_repo_list(path: Path) -> list[str]:
    repos: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        repos.append(line.strip("/"))
    return repos


def _safe_dirname(path_with_namespace: str) -> str:
    return path_with_namespace.replace("/", "__")


def _run(cmd: list[str], *, cwd: Optional[Path] = None, env: Optional[dict] = None) -> None:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )


def _git_auth_url(http_url: str, token: str) -> str:
    """Inject oauth2 token into https clone URL."""
    if http_url.startswith("https://"):
        return http_url.replace("https://", f"https://oauth2:{token}@", 1)
    if http_url.startswith("http://"):
        return http_url.replace("http://", f"http://oauth2:{token}@", 1)
    return http_url


def _pick(d: dict, keys: Iterable[str]) -> dict:
    return {k: d[k] for k in keys if k in d}


def export_project_metadata(client: GitLabClient, project: dict) -> dict[str, Any]:
    project_id = project["id"]
    meta: dict[str, Any] = {
        "project": _pick(
            project,
            [
                "id",
                "name",
                "path",
                "path_with_namespace",
                "description",
                "visibility",
                "default_branch",
                "topics",
                "tag_list",
                "issues_enabled",
                "merge_requests_enabled",
                "wiki_enabled",
                "jobs_enabled",
                "snippets_enabled",
                "container_registry_enabled",
                "shared_runners_enabled",
                "lfs_enabled",
                "packages_enabled",
                "request_access_enabled",
                "only_allow_merge_if_pipeline_succeeds",
                "only_allow_merge_if_all_discussions_are_resolved",
                "remove_source_branch_after_merge",
                "printing_merge_request_link_enabled",
                "merge_method",
                "autoclose_referenced_issues",
                "ci_config_path",
                "http_url_to_repo",
                "ssh_url_to_repo",
                "web_url",
                "created_at",
                "last_activity_at",
                "archived",
                "empty_repo",
                "namespace",
            ],
        ),
        "variables": [],
        "labels": [],
        "protected_branches": [],
        "protected_tags": [],
        "hooks": [],
        "deploy_keys": [],
        "branches": [],
        "tags": [],
        "commits_sample": [],
        "warnings": [],
    }

    collectors = [
        ("variables", lambda: client.list_variables(project_id)),
        ("labels", lambda: client.list_labels(project_id)),
        ("protected_branches", lambda: client.list_protected_branches(project_id)),
        ("protected_tags", lambda: client.list_protected_tags(project_id)),
        ("hooks", lambda: client.list_hooks(project_id)),
        ("deploy_keys", lambda: client.list_deploy_keys(project_id)),
        ("branches", lambda: client.list_branches(project_id)),
        ("tags", lambda: client.list_tags(project_id)),
    ]

    for key, fn in collectors:
        try:
            meta[key] = fn()
        except GitLabError as exc:
            meta["warnings"].append(f"Failed to fetch {key}: {exc}")

    try:
        default_branch = project.get("default_branch")
        commits = client.list_commits(project_id, ref_name=default_branch)
        # Keep a compact sample for inventory; full history comes from git mirror.
        meta["commits_sample"] = commits[:50]
        meta["commits_count_sampled"] = len(commits)
    except GitLabError as exc:
        meta["warnings"].append(f"Failed to fetch commits: {exc}")

    return meta


def mirror_clone(project: dict, token: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    mirror_path = dest_dir / "repo.git"
    http_url = project.get("http_url_to_repo")
    if not http_url:
        raise RuntimeError("Project has no http_url_to_repo")

    auth_url = _git_auth_url(http_url, token)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    if mirror_path.exists():
        _run(["git", "remote", "set-url", "origin", auth_url], cwd=mirror_path, env=env)
        _run(["git", "fetch", "--prune", "origin", "+refs/*:refs/*"], cwd=mirror_path, env=env)
    else:
        _run(
            ["git", "clone", "--mirror", auth_url, str(mirror_path)],
            env=env,
        )
    return mirror_path


def export_official_bundle(
    client: GitLabClient,
    project: dict,
    dest_dir: Path,
    *,
    timeout_sec: int = 1800,
) -> Optional[Path]:
    """Use GitLab project export API (issues/MRs/wiki/snippets when enabled)."""
    project_id = project["id"]
    try:
        client.start_export(project_id)
        client.wait_for_export(project_id, timeout_sec=timeout_sec)
        bundle = dest_dir / "gitlab_export.tar.gz"
        client.download_export(project_id, str(bundle))
        return bundle
    except GitLabError as exc:
        warning_path = dest_dir / "export_bundle_error.txt"
        warning_path.write_text(str(exc), encoding="utf-8")
        return None


def export_repositories(
    client: GitLabClient,
    token: str,
    repos: list[str],
    output_dir: Path,
    *,
    include_git: bool = True,
    include_bundle: bool = False,
    skip_missing: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "mode": "dry-run" if dry_run else "export",
        "source": client.base_url,
        "exported": [],
        "would_export": [],
        "missing": [],
        "failed": [],
    }

    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for path_with_namespace in repos:
        print(f"[export] Looking up {path_with_namespace} ...")
        project = client.find_project(path_with_namespace)
        if project is None:
            print(f"[export] NOT FOUND: {path_with_namespace}")
            summary["missing"].append(path_with_namespace)
            if not skip_missing:
                raise GitLabError(f"Project not found: {path_with_namespace}", status=404)
            continue

        project_dir = output_dir / _safe_dirname(path_with_namespace)

        try:
            print(f"[export] Metadata for {path_with_namespace} (id={project['id']})")
            meta = export_project_metadata(client, project)
            inventory = inventory_from_meta(meta)
            actions = build_export_actions(
                include_git=include_git,
                include_bundle=include_bundle,
                empty_repo=bool(project.get("empty_repo")),
            )

            if dry_run:
                item = {
                    "path_with_namespace": path_with_namespace,
                    "id": project["id"],
                    "web_url": project.get("web_url"),
                    "inventory": inventory,
                    "actions": actions,
                    "warnings": inventory.get("warnings") or [],
                }
                summary["would_export"].append(item)
                print(f"[export][dry-run] would export {path_with_namespace}")
                continue

            project_dir.mkdir(parents=True, exist_ok=True)
            (project_dir / "metadata.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            if include_git and not project.get("empty_repo"):
                print(f"[export] git mirror clone {path_with_namespace}")
                mirror_clone(project, token, project_dir)
            elif project.get("empty_repo"):
                meta.setdefault("warnings", []).append("Repository is empty; skipped git clone")
                (project_dir / "metadata.json").write_text(
                    json.dumps(meta, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

            if include_bundle:
                print(f"[export] Official GitLab export bundle for {path_with_namespace}")
                export_official_bundle(client, project, project_dir)

            summary["exported"].append(
                {
                    "path_with_namespace": path_with_namespace,
                    "id": project["id"],
                    "dir": str(project_dir),
                    "inventory": inventory,
                    "actions": actions,
                }
            )
            print(f"[export] OK -> {project_dir}")
        except Exception as exc:  # noqa: BLE001 - collect per-repo failures
            print(f"[export] FAILED {path_with_namespace}: {exc}")
            summary["failed"].append({"path_with_namespace": path_with_namespace, "error": str(exc)})

    if dry_run:
        print_dry_run_report("export from source GitLab", summary)
        summary["found"] = summary["would_export"]
        return summary

    (output_dir / "export_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary
