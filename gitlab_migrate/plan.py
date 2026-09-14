from __future__ import annotations

from typing import Any, Optional


def inventory_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    project = meta.get("project") or {}
    return {
        "name": project.get("name"),
        "path_with_namespace": project.get("path_with_namespace"),
        "visibility": project.get("visibility"),
        "default_branch": project.get("default_branch"),
        "empty_repo": bool(project.get("empty_repo")),
        "archived": bool(project.get("archived")),
        "counts": {
            "branches": len(meta.get("branches") or []),
            "tags": len(meta.get("tags") or []),
            "variables": len(meta.get("variables") or []),
            "labels": len(meta.get("labels") or []),
            "protected_branches": len(meta.get("protected_branches") or []),
            "protected_tags": len(meta.get("protected_tags") or []),
            "hooks": len(meta.get("hooks") or []),
            "deploy_keys": len(meta.get("deploy_keys") or []),
            "commits_sampled": meta.get("commits_count_sampled", len(meta.get("commits_sample") or [])),
        },
        "variable_keys": [v.get("key") for v in (meta.get("variables") or []) if v.get("key")],
        "branch_names": [b.get("name") for b in (meta.get("branches") or []) if b.get("name")],
        "tag_names": [t.get("name") for t in (meta.get("tags") or []) if t.get("name")],
        "warnings": list(meta.get("warnings") or []),
    }


def build_export_actions(
    *,
    include_git: bool,
    include_bundle: bool,
    empty_repo: bool,
) -> list[str]:
    actions = ["fetch metadata (project, variables, labels, protections, branches, tags)"]
    if include_git and not empty_repo:
        actions.append("git clone --mirror (full history, branches, tags)")
    elif include_git and empty_repo:
        actions.append("skip git clone (empty repository)")
    if include_bundle:
        actions.append("download official GitLab export bundle (issues/MRs/wiki)")
    return actions


def build_import_actions(
    *,
    include_git: bool,
    include_variables: bool,
    include_labels: bool,
    include_protections: bool,
    include_hooks: bool,
    include_deploy_keys: bool,
    prefer_bundle: bool,
    has_bundle: bool,
    has_git_mirror: bool,
    counts: Optional[dict[str, int]] = None,
) -> list[str]:
    counts = counts or {}
    actions: list[str] = []

    if prefer_bundle and has_bundle:
        actions.append("create project via official GitLab import API (bundle)")
    else:
        actions.append("create project/namespace on destination (if missing)")
        if include_git and has_git_mirror:
            actions.append("git push --mirror (branches, tags, history)")
        elif include_git:
            actions.append("skip git push (repo.git missing in export pool)")

    if include_variables:
        actions.append(f"import CI/CD variables ({counts.get('variables', 0)})")
    if include_labels:
        actions.append(f"import labels ({counts.get('labels', 0)})")
    if include_protections:
        actions.append(
            "import protected branches/tags "
            f"({counts.get('protected_branches', 0)}/{counts.get('protected_tags', 0)})"
        )
    if include_hooks:
        actions.append(f"import webhooks ({counts.get('hooks', 0)})")
    if include_deploy_keys:
        actions.append(f"import deploy keys ({counts.get('deploy_keys', 0)})")
    return actions


def print_dry_run_report(title: str, summary: dict[str, Any]) -> None:
    print()
    print("=" * 72)
    print(f"DRY-RUN: {title}")
    print("=" * 72)

    mode = summary.get("mode")
    if mode:
        print(f"mode: {mode}")
    if summary.get("source"):
        print(f"source: {summary['source']}")
    if summary.get("target"):
        print(f"target: {summary['target']}")

    found = summary.get("found") or summary.get("would_export") or summary.get("would_import") or []
    missing = summary.get("missing") or []
    failed = summary.get("failed") or []

    print(f"items: {len(found)} | missing: {len(missing)} | failed: {len(failed)}")
    print("-" * 72)

    for item in found:
        src = item.get("path_with_namespace") or item.get("source_path") or "?"
        target = item.get("target_path")
        header = f"* {src}"
        if target:
            header += f"  ->  {target}"
        print(header)

        status = item.get("destination_status")
        if status:
            print(f"    destination: {status}")

        inv = item.get("inventory")
        if inv:
            counts = inv.get("counts") or {}
            print(
                "    inventory: "
                f"branches={counts.get('branches', 0)}, "
                f"tags={counts.get('tags', 0)}, "
                f"variables={counts.get('variables', 0)}, "
                f"labels={counts.get('labels', 0)}, "
                f"protected_branches={counts.get('protected_branches', 0)}, "
                f"protected_tags={counts.get('protected_tags', 0)}"
            )
            if inv.get("empty_repo"):
                print("    note: empty repository")
            if inv.get("archived"):
                print("    note: archived on source")

        for action in item.get("actions") or []:
            print(f"    - {action}")

        for warning in item.get("warnings") or []:
            print(f"    ! {warning}")
        print()

    if missing:
        print("Missing / not found:")
        for path in missing:
            print(f"  - {path}")
        print()

    if failed:
        print("Failed during dry-run lookup:")
        for item in failed:
            print(f"  - {item}")
        print()

    print("No changes were made (dry-run).")
    print("=" * 72)
