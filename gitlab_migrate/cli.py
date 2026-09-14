from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import click

from . import __version__
from .client import GitLabClient
from .export_cmd import export_repositories, load_repo_list
from .import_cmd import import_repositories, plan_migration


def _client(url: str, token: str, insecure: bool) -> GitLabClient:
    return GitLabClient(url, token, verify_ssl=not insecure)


@click.group()
@click.version_option(__version__, prog_name="gitlab-migrate")
def main() -> None:
    """Migrate GitLab projects between two GitLab instances."""


@main.command("whoami")
@click.option("--url", required=True, envvar="GITLAB_URL", help="GitLab base URL")
@click.option("--token", required=True, envvar="GITLAB_TOKEN", help="Personal/Project access token")
@click.option("--insecure", is_flag=True, help="Disable TLS certificate verification")
def whoami(url: str, token: str, insecure: bool) -> None:
    """Check token and print current user."""
    client = _client(url, token, insecure)
    user = client.get_current_user()
    click.echo(json.dumps({"url": client.base_url, "user": user}, ensure_ascii=False, indent=2))


@main.command("export")
@click.option("--url", required=True, envvar="SRC_GITLAB_URL", help="Source GitLab base URL")
@click.option("--token", required=True, envvar="SRC_GITLAB_TOKEN", help="Source GitLab token")
@click.option(
    "--repos-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Text file with path_with_namespace per line",
)
@click.option(
    "--out",
    "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("export_data"),
    show_default=True,
    help="Directory to store exported projects",
)
@click.option("--no-git", is_flag=True, help="Do not mirror-clone git repositories")
@click.option(
    "--with-bundle",
    is_flag=True,
    help="Also download official GitLab project export archive (issues/MRs/wiki)",
)
@click.option("--skip-missing", is_flag=True, help="Continue if a repo from the list is not found")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Only discover projects and print what would be exported (no clone/write)",
)
@click.option("--insecure", is_flag=True, help="Disable TLS certificate verification")
def export_cmd(
    url: str,
    token: str,
    repos_file: Path,
    output_dir: Path,
    no_git: bool,
    with_bundle: bool,
    skip_missing: bool,
    dry_run: bool,
    insecure: bool,
) -> None:
    """Export projects from source GitLab into a local pool."""
    repos = load_repo_list(repos_file)
    if not repos:
        raise click.ClickException(f"No repositories found in {repos_file}")

    client = _client(url, token, insecure)
    summary = export_repositories(
        client,
        token,
        repos,
        output_dir,
        include_git=not no_git,
        include_bundle=with_bundle,
        skip_missing=skip_missing,
        dry_run=dry_run,
    )
    click.echo(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["failed"] or (summary["missing"] and not skip_missing):
        raise SystemExit(1)


@main.command("import")
@click.option("--url", required=True, envvar="DST_GITLAB_URL", help="Destination GitLab base URL")
@click.option("--token", required=True, envvar="DST_GITLAB_TOKEN", help="Destination GitLab token")
@click.option(
    "--in",
    "input_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("export_data"),
    show_default=True,
    help="Directory produced by the export command",
)
@click.option("--no-git", is_flag=True, help="Do not push git mirrors")
@click.option("--no-variables", is_flag=True, help="Do not import CI/CD variables")
@click.option("--no-labels", is_flag=True, help="Do not import labels")
@click.option("--no-protections", is_flag=True, help="Do not import protected branches/tags")
@click.option("--with-hooks", is_flag=True, help="Import project webhooks")
@click.option("--with-deploy-keys", is_flag=True, help="Import deploy keys")
@click.option(
    "--prefer-bundle",
    is_flag=True,
    help="If gitlab_export.tar.gz exists, import via official GitLab import API",
)
@click.option(
    "--path-prefix",
    default=None,
    help="Optional namespace prefix on destination, e.g. migrated",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be imported and whether targets already exist (no changes)",
)
@click.option("--insecure", is_flag=True, help="Disable TLS certificate verification")
def import_cmd(
    url: str,
    token: str,
    input_dir: Path,
    no_git: bool,
    no_variables: bool,
    no_labels: bool,
    no_protections: bool,
    with_hooks: bool,
    with_deploy_keys: bool,
    prefer_bundle: bool,
    path_prefix: Optional[str],
    dry_run: bool,
    insecure: bool,
) -> None:
    """Import previously exported project pool into destination GitLab."""
    client = _client(url, token, insecure)
    summary = import_repositories(
        client,
        token,
        input_dir,
        include_git=not no_git,
        include_variables=not no_variables,
        include_labels=not no_labels,
        include_protections=not no_protections,
        include_hooks=with_hooks,
        include_deploy_keys=with_deploy_keys,
        prefer_bundle=prefer_bundle,
        path_prefix=path_prefix,
        dry_run=dry_run,
    )
    click.echo(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["failed"]:
        raise SystemExit(1)


@main.command("migrate")
@click.option("--src-url", required=True, envvar="SRC_GITLAB_URL")
@click.option("--src-token", required=True, envvar="SRC_GITLAB_TOKEN")
@click.option("--dst-url", required=True, envvar="DST_GITLAB_URL")
@click.option("--dst-token", required=True, envvar="DST_GITLAB_TOKEN")
@click.option(
    "--repos-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--work-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("export_data"),
    show_default=True,
)
@click.option("--with-bundle", is_flag=True)
@click.option("--prefer-bundle", is_flag=True)
@click.option("--skip-missing", is_flag=True)
@click.option("--with-hooks", is_flag=True)
@click.option("--with-deploy-keys", is_flag=True)
@click.option("--path-prefix", default=None)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Discover source projects and destination targets; print migration plan only",
)
@click.option("--insecure", is_flag=True)
def migrate_cmd(
    src_url: str,
    src_token: str,
    dst_url: str,
    dst_token: str,
    repos_file: Path,
    work_dir: Path,
    with_bundle: bool,
    prefer_bundle: bool,
    skip_missing: bool,
    with_hooks: bool,
    with_deploy_keys: bool,
    path_prefix: Optional[str],
    dry_run: bool,
    insecure: bool,
) -> None:
    """Export from source GitLab and import into destination in one run."""
    repos = load_repo_list(repos_file)
    if not repos:
        raise click.ClickException(f"No repositories found in {repos_file}")

    src = _client(src_url, src_token, insecure)
    dst = _client(dst_url, dst_token, insecure)

    if dry_run:
        summary = plan_migration(
            src,
            dst,
            repos,
            include_git=True,
            include_bundle=with_bundle,
            include_hooks=with_hooks,
            include_deploy_keys=with_deploy_keys,
            prefer_bundle=prefer_bundle,
            path_prefix=path_prefix,
            skip_missing=skip_missing,
        )
        click.echo(json.dumps(summary, ensure_ascii=False, indent=2))
        if summary["failed"] or (summary["missing"] and not skip_missing):
            raise SystemExit(1)
        return

    export_summary = export_repositories(
        src,
        src_token,
        repos,
        work_dir,
        include_git=True,
        include_bundle=with_bundle or prefer_bundle,
        skip_missing=skip_missing,
    )

    import_summary = import_repositories(
        dst,
        dst_token,
        work_dir,
        include_git=True,
        include_variables=True,
        include_labels=True,
        include_protections=True,
        include_hooks=with_hooks,
        include_deploy_keys=with_deploy_keys,
        prefer_bundle=prefer_bundle,
        path_prefix=path_prefix,
    )

    click.echo(
        json.dumps(
            {"export": export_summary, "import": import_summary},
            ensure_ascii=False,
            indent=2,
        )
    )
    if export_summary["failed"] or import_summary["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
