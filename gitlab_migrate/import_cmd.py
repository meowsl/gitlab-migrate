from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from .client import GitLabClient, GitLabError
from .plan import (
    build_export_actions,
    build_import_actions,
    inventory_from_meta,
    print_dry_run_report,
)


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
    if http_url.startswith("https://"):
        return http_url.replace("https://", f"https://oauth2:{token}@", 1)
    if http_url.startswith("http://"):
        return http_url.replace("http://", f"http://oauth2:{token}@", 1)
    return http_url


def _access_level_value(item: Any, default: int = 40) -> int:
    if isinstance(item, dict):
        return int(item.get("access_level", default))
    if isinstance(item, int):
        return item
    return default


def discover_export_dirs(input_dir: Path) -> list[Path]:
    dirs: list[Path] = []
    for child in sorted(input_dir.iterdir()):
        if child.is_dir() and (child / "metadata.json").exists():
            dirs.append(child)
    return dirs


def create_or_get_project(
    client: GitLabClient,
    meta_project: dict,
    *,
    target_path_with_namespace: Optional[str] = None,
    visibility: Optional[str] = None,
) -> dict:
    path_with_namespace = target_path_with_namespace or meta_project["path_with_namespace"]
    existing = client.find_project(path_with_namespace)
    if existing:
        return existing

    parts = path_with_namespace.strip("/").split("/")
    if len(parts) < 2:
        # Create under current user namespace
        payload = {
            "name": meta_project.get("name") or parts[0],
            "path": parts[0],
            "description": meta_project.get("description") or "",
            "visibility": visibility or meta_project.get("visibility") or "private",
            "initialize_with_readme": False,
        }
        return client.create_project(payload)

    namespace_path = "/".join(parts[:-1])
    project_path = parts[-1]
    ns = client.ensure_namespace(
        namespace_path,
        visibility=visibility or meta_project.get("visibility") or "private",
    )

    payload = {
        "name": meta_project.get("name") or project_path,
        "path": project_path,
        "description": meta_project.get("description") or "",
        "visibility": visibility or meta_project.get("visibility") or "private",
        "initialize_with_readme": False,
        "namespace_id": ns["id"],
    }

    # Prefer group endpoint when possible
    try:
        return client.create_group_project(ns["id"], payload)
    except GitLabError:
        return client.create_project(payload)


def push_mirror(mirror_path: Path, project: dict, token: str) -> None:
    http_url = project.get("http_url_to_repo")
    if not http_url:
        raise RuntimeError("Target project has no http_url_to_repo")
    auth_url = _git_auth_url(http_url, token)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    # Ensure remote points to destination
    remotes = subprocess.run(
        ["git", "remote"],
        cwd=str(mirror_path),
        capture_output=True,
        text=True,
        check=False,
    )
    remote_names = set(remotes.stdout.split())
    if "target" in remote_names:
        _run(["git", "remote", "set-url", "target", auth_url], cwd=mirror_path, env=env)
    else:
        _run(["git", "remote", "add", "target", auth_url], cwd=mirror_path, env=env)

    # Push only branches/tags. A full --mirror also tries to push GitLab-internal
    # refs (merge-requests/*, pipelines/*), which destination GitLab rejects as
    # "deny updating a hidden ref" and fails the whole command.
    _run(
        [
            "git",
            "push",
            "--prune",
            "target",
            "+refs/heads/*:refs/heads/*",
            "+refs/tags/*:refs/tags/*",
        ],
        cwd=mirror_path,
        env=env,
    )


def import_variables(client: GitLabClient, project_id: int, variables: list[dict]) -> list[str]:
    warnings: list[str] = []
    for var in variables:
        payload = {
            "key": var["key"],
            "value": var.get("value", ""),
            "variable_type": var.get("variable_type", "env_var"),
            "protected": bool(var.get("protected", False)),
            "masked": bool(var.get("masked", False)),
            "raw": bool(var.get("raw", False)),
            "environment_scope": var.get("environment_scope", "*"),
        }
        # GitLab may reject masked values that don't meet format requirements after re-import.
        try:
            client.create_variable(project_id, payload)
        except GitLabError as exc:
            if payload["masked"]:
                payload["masked"] = False
                try:
                    client.create_variable(project_id, payload)
                    warnings.append(
                        f"Variable {payload['key']}: created without masked=true ({exc})"
                    )
                    continue
                except GitLabError as exc2:
                    warnings.append(f"Variable {payload['key']}: {exc2}")
                    continue
            warnings.append(f"Variable {payload['key']}: {exc}")
    return warnings


def import_labels(client: GitLabClient, project_id: int, labels: list[dict]) -> list[str]:
    warnings: list[str] = []
    for label in labels:
        payload = {
            "name": label["name"],
            "color": label.get("color") or "#428BCA",
            "description": label.get("description") or "",
            "priority": label.get("priority"),
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            client.create_label(project_id, payload)
        except GitLabError as exc:
            warnings.append(f"Label {payload['name']}: {exc}")
    return warnings


def import_protected_branches(
    client: GitLabClient, project_id: int, branches: list[dict]
) -> list[str]:
    warnings: list[str] = []
    for branch in branches:
        payload = {
            "name": branch["name"],
            "push_access_level": _access_level_value(
                (branch.get("push_access_levels") or [{}])[0], 40
            ),
            "merge_access_level": _access_level_value(
                (branch.get("merge_access_levels") or [{}])[0], 40
            ),
            "allow_force_push": bool(branch.get("allow_force_push", False)),
        }
        code_owner = branch.get("code_owner_approval_required")
        if code_owner is not None:
            payload["code_owner_approval_required"] = bool(code_owner)
        try:
            client.protect_branch(project_id, payload)
        except GitLabError as exc:
            warnings.append(f"Protected branch {payload['name']}: {exc}")
    return warnings


def import_protected_tags(client: GitLabClient, project_id: int, tags: list[dict]) -> list[str]:
    warnings: list[str] = []
    for tag in tags:
        payload = {
            "name": tag["name"],
            "create_access_level": _access_level_value(
                (tag.get("create_access_levels") or [{}])[0], 40
            ),
        }
        try:
            client.protect_tag(project_id, payload)
        except GitLabError as exc:
            warnings.append(f"Protected tag {payload['name']}: {exc}")
    return warnings


def import_hooks(client: GitLabClient, project_id: int, hooks: list[dict]) -> list[str]:
    warnings: list[str] = []
    for hook in hooks:
        payload = {
            "url": hook.get("url"),
            "push_events": hook.get("push_events", True),
            "issues_events": hook.get("issues_events", False),
            "confidential_issues_events": hook.get("confidential_issues_events", False),
            "merge_requests_events": hook.get("merge_requests_events", False),
            "tag_push_events": hook.get("tag_push_events", False),
            "note_events": hook.get("note_events", False),
            "job_events": hook.get("job_events", False),
            "pipeline_events": hook.get("pipeline_events", False),
            "wiki_page_events": hook.get("wiki_page_events", False),
            "deployment_events": hook.get("deployment_events", False),
            "releases_events": hook.get("releases_events", False),
            "enable_ssl_verification": hook.get("enable_ssl_verification", True),
            "token": hook.get("token") or "",
        }
        if not payload["url"]:
            warnings.append("Hook skipped: missing url")
            continue
        try:
            client.create_hook(project_id, payload)
        except GitLabError as exc:
            warnings.append(f"Hook {payload['url']}: {exc}")
    return warnings


def import_deploy_keys(client: GitLabClient, project_id: int, keys: list[dict]) -> list[str]:
    warnings: list[str] = []
    for key in keys:
        payload = {
            "title": key.get("title") or "migrated-key",
            "key": key.get("key"),
            "can_push": bool(key.get("can_push", False)),
        }
        if not payload["key"]:
            warnings.append(f"Deploy key {payload['title']}: missing public key material")
            continue
        try:
            client.add_deploy_key(project_id, payload)
        except GitLabError as exc:
            warnings.append(f"Deploy key {payload['title']}: {exc}")
    return warnings


def import_from_official_bundle(
    client: GitLabClient,
    *,
    bundle_path: Path,
    meta_project: dict,
    target_path_with_namespace: Optional[str] = None,
) -> dict:
    path_with_namespace = target_path_with_namespace or meta_project["path_with_namespace"]
    parts = path_with_namespace.strip("/").split("/")
    project_path = parts[-1]
    if len(parts) >= 2:
        ns = client.ensure_namespace("/".join(parts[:-1]), visibility=meta_project.get("visibility", "private"))
        namespace_id = ns["id"]
    else:
        user = client.get_current_user()
        namespace_id = user["namespace_id"] if "namespace_id" in user else user["id"]

    imported = client.import_project_file(
        namespace_id=namespace_id,
        path=project_path,
        name=meta_project.get("name") or project_path,
        file_path=str(bundle_path),
        overwrite=False,
    )
    project_id = imported.get("id")
    if project_id:
        client.wait_for_import(project_id)
        return client.get_project(project_id)
    return imported


def import_repositories(
    client: GitLabClient,
    token: str,
    input_dir: Path,
    *,
    include_git: bool = True,
    include_variables: bool = True,
    include_labels: bool = True,
    include_protections: bool = True,
    include_hooks: bool = False,
    include_deploy_keys: bool = False,
    prefer_bundle: bool = False,
    path_prefix: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "mode": "dry-run" if dry_run else "import",
        "target": client.base_url,
        "imported": [],
        "would_import": [],
        "failed": [],
    }

    export_dirs = discover_export_dirs(input_dir)
    if not export_dirs:
        print(f"[import] No exported projects found in {input_dir}")

    for project_dir in export_dirs:
        meta = json.loads((project_dir / "metadata.json").read_text(encoding="utf-8"))
        src_project = meta["project"]
        src_path = src_project["path_with_namespace"]
        target_path = f"{path_prefix.strip('/')}/{src_path}" if path_prefix else src_path

        print(f"[import] {src_path} -> {target_path}")
        warnings: list[str] = []
        inventory = inventory_from_meta(meta)
        bundle = project_dir / "gitlab_export.tar.gz"
        mirror_path = project_dir / "repo.git"
        has_bundle = bundle.exists()
        has_git_mirror = mirror_path.exists()
        actions = build_import_actions(
            include_git=include_git,
            include_variables=include_variables,
            include_labels=include_labels,
            include_protections=include_protections,
            include_hooks=include_hooks,
            include_deploy_keys=include_deploy_keys,
            prefer_bundle=prefer_bundle,
            has_bundle=has_bundle,
            has_git_mirror=has_git_mirror,
            counts=inventory.get("counts"),
        )

        try:
            if dry_run:
                existing = client.find_project(target_path)
                if existing:
                    destination_status = (
                        f"EXISTS id={existing['id']} url={existing.get('web_url')}"
                    )
                    warnings.append(
                        "target project already exists; import would reuse/update it"
                    )
                else:
                    destination_status = "MISSING (will be created)"

                local_assets = {
                    "metadata": True,
                    "repo.git": has_git_mirror,
                    "gitlab_export.tar.gz": has_bundle,
                }
                item = {
                    "source_path": src_path,
                    "target_path": target_path,
                    "destination_status": destination_status,
                    "local_assets": local_assets,
                    "inventory": inventory,
                    "actions": actions,
                    "warnings": warnings + list(inventory.get("warnings") or []),
                }
                summary["would_import"].append(item)
                print(f"[import][dry-run] would import {src_path} -> {target_path}")
                continue

            if prefer_bundle and has_bundle:
                print(f"[import] Using official export bundle for {src_path}")
                project = import_from_official_bundle(
                    client,
                    bundle_path=bundle,
                    meta_project=src_project,
                    target_path_with_namespace=target_path,
                )
            else:
                project = create_or_get_project(
                    client,
                    src_project,
                    target_path_with_namespace=target_path,
                )

                if include_git and has_git_mirror:
                    print(f"[import] git push --mirror -> {target_path}")
                    push_mirror(mirror_path, project, token)
                elif include_git:
                    warnings.append("repo.git missing; skipped git push")

            project_id = project["id"]

            if include_variables:
                warnings.extend(import_variables(client, project_id, meta.get("variables") or []))
            if include_labels:
                warnings.extend(import_labels(client, project_id, meta.get("labels") or []))
            if include_protections:
                warnings.extend(
                    import_protected_branches(
                        client, project_id, meta.get("protected_branches") or []
                    )
                )
                warnings.extend(
                    import_protected_tags(client, project_id, meta.get("protected_tags") or [])
                )
            if include_hooks:
                warnings.extend(import_hooks(client, project_id, meta.get("hooks") or []))
            if include_deploy_keys:
                warnings.extend(import_deploy_keys(client, project_id, meta.get("deploy_keys") or []))

            result = {
                "source_path": src_path,
                "target_path": project.get("path_with_namespace", target_path),
                "target_id": project_id,
                "warnings": warnings,
            }
            (project_dir / "import_result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            summary["imported"].append(result)
            print(f"[import] OK {target_path} (warnings={len(warnings)})")
        except Exception as exc:  # noqa: BLE001
            print(f"[import] FAILED {src_path}: {exc}")
            summary["failed"].append({"source_path": src_path, "error": str(exc)})

    if dry_run:
        print_dry_run_report("import into destination GitLab", summary)
        summary["found"] = summary["would_import"]
        return summary

    (input_dir / "import_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def plan_migration(
    src_client: GitLabClient,
    dst_client: GitLabClient,
    repos: list[str],
    *,
    include_git: bool = True,
    include_bundle: bool = False,
    include_hooks: bool = False,
    include_deploy_keys: bool = False,
    prefer_bundle: bool = False,
    path_prefix: Optional[str] = None,
    skip_missing: bool = False,
) -> dict[str, Any]:
    """Dry-run for end-to-end migrate without writing an export pool."""
    summary: dict[str, Any] = {
        "mode": "dry-run",
        "source": src_client.base_url,
        "target": dst_client.base_url,
        "would_migrate": [],
        "missing": [],
        "failed": [],
    }

    for path_with_namespace in repos:
        print(f"[migrate][dry-run] Looking up source {path_with_namespace} ...")
        try:
            project = src_client.find_project(path_with_namespace)
            if project is None:
                print(f"[migrate][dry-run] NOT FOUND on source: {path_with_namespace}")
                summary["missing"].append(path_with_namespace)
                if not skip_missing:
                    raise GitLabError(f"Project not found: {path_with_namespace}", status=404)
                continue

            from .export_cmd import export_project_metadata

            meta = export_project_metadata(src_client, project)
            inventory = inventory_from_meta(meta)
            target_path = (
                f"{path_prefix.strip('/')}/{path_with_namespace}"
                if path_prefix
                else path_with_namespace
            )

            existing = dst_client.find_project(target_path)
            if existing:
                destination_status = f"EXISTS id={existing['id']} url={existing.get('web_url')}"
                warnings = ["target project already exists; import would reuse/update it"]
            else:
                destination_status = "MISSING (will be created)"
                warnings = []

            export_actions = build_export_actions(
                include_git=include_git,
                include_bundle=include_bundle or prefer_bundle,
                empty_repo=bool(project.get("empty_repo")),
            )
            import_actions = build_import_actions(
                include_git=include_git,
                include_variables=True,
                include_labels=True,
                include_protections=True,
                include_hooks=include_hooks,
                include_deploy_keys=include_deploy_keys,
                prefer_bundle=prefer_bundle,
                has_bundle=include_bundle or prefer_bundle,
                has_git_mirror=include_git and not project.get("empty_repo"),
                counts=inventory.get("counts"),
            )

            item = {
                "source_path": path_with_namespace,
                "path_with_namespace": path_with_namespace,
                "target_path": target_path,
                "id": project["id"],
                "web_url": project.get("web_url"),
                "destination_status": destination_status,
                "inventory": inventory,
                "actions": [f"export: {a}" for a in export_actions]
                + [f"import: {a}" for a in import_actions],
                "warnings": warnings + list(inventory.get("warnings") or []),
            }
            summary["would_migrate"].append(item)
            print(f"[migrate][dry-run] {path_with_namespace} -> {target_path}")
        except Exception as exc:  # noqa: BLE001
            print(f"[migrate][dry-run] FAILED {path_with_namespace}: {exc}")
            summary["failed"].append({"path_with_namespace": path_with_namespace, "error": str(exc)})

    summary["would_import"] = summary["would_migrate"]
    summary["found"] = summary["would_migrate"]
    print_dry_run_report("full migration (source -> destination)", summary)
    return summary
