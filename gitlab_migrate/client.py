from __future__ import annotations

import time
from typing import Any, Optional, Union
from urllib.parse import quote, urljoin

import requests


class GitLabError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class GitLabClient:
    """Thin wrapper around GitLab REST API v4."""

    def __init__(self, base_url: str, token: str, timeout: int = 60, verify_ssl: bool = True):
        self.base_url = base_url.rstrip("/")
        self.api_url = f"{self.base_url}/api/v4"
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.session = requests.Session()
        # Do not set Content-Type globally — multipart uploads must let requests
        # choose boundary. JSON requests set it per-call.
        self.session.headers.update(
            {
                "PRIVATE-TOKEN": token,
            }
        )

    def _url(self, path: str) -> str:
        return urljoin(self.api_url + "/", path.lstrip("/"))

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json: Any = None,
        data: Any = None,
        files: Any = None,
        stream: bool = False,
        expected: tuple[int, ...] = (200, 201, 202, 204),
    ) -> requests.Response:
        headers: dict[str, str] = {}
        if json is not None and files is None:
            headers["Content-Type"] = "application/json"
        # When files= is set, do not send Content-Type — requests must add
        # multipart boundary. A stale application/json yields opaque 400s.
        response = self.session.request(
            method,
            self._url(path),
            params=params,
            json=json,
            data=data,
            files=files,
            headers=headers,
            timeout=self.timeout,
            verify=self.verify_ssl,
            stream=stream,
        )
        if response.status_code not in expected:
            try:
                body = response.json()
            except Exception:
                body = (response.text or "")[:2000]
            raise GitLabError(
                f"{method} {path} failed ({response.status_code}): {body}",
                status=response.status_code,
                body=body,
            )
        return response

    def get_json(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs).json()

    def post_json(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs).json()

    def put_json(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs).json()

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("DELETE", path, expected=(200, 202, 204), **kwargs)

    def paginate(self, path: str, params: Optional[dict] = None) -> list[Any]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        page = 1
        items: list[Any] = []
        while True:
            params["page"] = page
            response = self.request("GET", path, params=params)
            batch = response.json()
            if not batch:
                break
            items.extend(batch)
            next_page = response.headers.get("X-Next-Page")
            if not next_page:
                break
            page = int(next_page)
        return items

    def get_current_user(self) -> dict:
        return self.get_json("/user")

    def find_project(self, path_with_namespace: str) -> Optional[dict]:
        encoded = quote(path_with_namespace.strip("/"), safe="")
        try:
            return self.get_json(f"/projects/{encoded}")
        except GitLabError as exc:
            if exc.status == 404:
                return None
            raise

    def get_project(self, project_id: Union[int, str]) -> dict:
        encoded = quote(str(project_id), safe="")
        return self.get_json(f"/projects/{encoded}")

    def create_project(self, payload: dict) -> dict:
        return self.post_json("/projects", json=payload)

    def create_group_project(self, group_id: int, payload: dict) -> dict:
        return self.post_json(f"/groups/{group_id}/projects", json=payload)

    def ensure_namespace(self, full_path: str, visibility: str = "private") -> dict:
        """Ensure group/namespace path exists; returns the leaf namespace object."""
        parts = [p for p in full_path.strip("/").split("/") if p]
        if not parts:
            raise GitLabError("Empty namespace path")

        parent_id: Optional[int] = None
        current_path = ""
        leaf: Optional[dict] = None

        for part in parts:
            current_path = f"{current_path}/{part}" if current_path else part
            encoded = quote(current_path, safe="")
            try:
                leaf = self.get_json(f"/groups/{encoded}")
                parent_id = leaf["id"]
                continue
            except GitLabError as exc:
                if exc.status != 404:
                    raise

            payload: dict[str, Any] = {
                "name": part,
                "path": part,
                "visibility": visibility,
            }
            if parent_id is not None:
                payload["parent_id"] = parent_id
            leaf = self.post_json("/groups", json=payload)
            parent_id = leaf["id"]

        assert leaf is not None
        return leaf

    def list_variables(self, project_id: Union[int, str]) -> list[dict]:
        encoded = quote(str(project_id), safe="")
        return self.paginate(f"/projects/{encoded}/variables")

    def create_variable(self, project_id: Union[int, str], variable: dict) -> dict:
        encoded = quote(str(project_id), safe="")
        return self.post_json(f"/projects/{encoded}/variables", json=variable)

    def list_labels(self, project_id: Union[int, str]) -> list[dict]:
        encoded = quote(str(project_id), safe="")
        return self.paginate(f"/projects/{encoded}/labels")

    def create_label(self, project_id: Union[int, str], label: dict) -> dict:
        encoded = quote(str(project_id), safe="")
        return self.post_json(f"/projects/{encoded}/labels", json=label)

    def list_protected_branches(self, project_id: Union[int, str]) -> list[dict]:
        encoded = quote(str(project_id), safe="")
        return self.paginate(f"/projects/{encoded}/protected_branches")

    def protect_branch(self, project_id: Union[int, str], payload: dict) -> dict:
        encoded = quote(str(project_id), safe="")
        return self.post_json(f"/projects/{encoded}/protected_branches", json=payload)

    def list_protected_tags(self, project_id: Union[int, str]) -> list[dict]:
        encoded = quote(str(project_id), safe="")
        return self.paginate(f"/projects/{encoded}/protected_tags")

    def protect_tag(self, project_id: Union[int, str], payload: dict) -> dict:
        encoded = quote(str(project_id), safe="")
        return self.post_json(f"/projects/{encoded}/protected_tags", json=payload)

    def list_hooks(self, project_id: Union[int, str]) -> list[dict]:
        encoded = quote(str(project_id), safe="")
        return self.paginate(f"/projects/{encoded}/hooks")

    def create_hook(self, project_id: Union[int, str], payload: dict) -> dict:
        encoded = quote(str(project_id), safe="")
        return self.post_json(f"/projects/{encoded}/hooks", json=payload)

    def list_deploy_keys(self, project_id: Union[int, str]) -> list[dict]:
        encoded = quote(str(project_id), safe="")
        return self.paginate(f"/projects/{encoded}/deploy_keys")

    def enable_deploy_key(self, project_id: Union[int, str], key_id: int) -> dict:
        encoded = quote(str(project_id), safe="")
        return self.post_json(f"/projects/{encoded}/deploy_keys/{key_id}/enable")

    def add_deploy_key(self, project_id: Union[int, str], payload: dict) -> dict:
        encoded = quote(str(project_id), safe="")
        return self.post_json(f"/projects/{encoded}/deploy_keys", json=payload)

    def list_branches(self, project_id: Union[int, str]) -> list[dict]:
        encoded = quote(str(project_id), safe="")
        return self.paginate(f"/projects/{encoded}/repository/branches")

    def list_tags(self, project_id: Union[int, str]) -> list[dict]:
        encoded = quote(str(project_id), safe="")
        return self.paginate(f"/projects/{encoded}/repository/tags")

    def list_commits(self, project_id: Union[int, str], ref_name: Optional[str] = None) -> list[dict]:
        encoded = quote(str(project_id), safe="")
        params = {"ref_name": ref_name} if ref_name else None
        return self.paginate(f"/projects/{encoded}/repository/commits", params=params)

    def start_export(self, project_id: Union[int, str]) -> dict:
        encoded = quote(str(project_id), safe="")
        return self.post_json(f"/projects/{encoded}/export")

    def get_export_status(self, project_id: Union[int, str]) -> dict:
        encoded = quote(str(project_id), safe="")
        return self.get_json(f"/projects/{encoded}/export")

    def download_export(self, project_id: Union[int, str], dest_path: str) -> None:
        encoded = quote(str(project_id), safe="")
        response = self.request(
            "GET",
            f"/projects/{encoded}/export/download",
            stream=True,
            expected=(200,),
        )
        with open(dest_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    fh.write(chunk)

    def wait_for_export(self, project_id: Union[int, str], timeout_sec: int = 1800, poll: int = 5) -> dict:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            status = self.get_export_status(project_id)
            export_status = status.get("export_status")
            if export_status == "finished":
                return status
            if export_status in {"failed", "none"} and status.get("export_error"):
                raise GitLabError(f"Export failed for project {project_id}: {status}")
            if export_status == "failed":
                raise GitLabError(f"Export failed for project {project_id}: {status}")
            time.sleep(poll)
        raise GitLabError(f"Timed out waiting for export of project {project_id}")

    def import_project_file(
        self,
        *,
        namespace_id: int,
        path: str,
        name: str,
        file_path: str,
        overwrite: bool = False,
    ) -> dict:
        # Large export archives can take a while to upload/process.
        old_timeout = self.timeout
        self.timeout = max(self.timeout, 600)
        try:
            with open(file_path, "rb") as fh:
                files = {
                    "file": (
                        f"{path}.tar.gz",
                        fh,
                        "application/octet-stream",
                    )
                }
                data = {
                    "path": path,
                    "name": name,
                    "namespace": str(namespace_id),
                    "overwrite": "true" if overwrite else "false",
                }
                response = self.request(
                    "POST",
                    "/projects/import",
                    data=data,
                    files=files,
                    expected=(200, 201, 202),
                )
                return response.json()
        finally:
            self.timeout = old_timeout

    def get_import_status(self, project_id: Union[int, str]) -> dict:
        encoded = quote(str(project_id), safe="")
        return self.get_json(f"/projects/{encoded}/import")

    def wait_for_import(self, project_id: Union[int, str], timeout_sec: int = 1800, poll: int = 5) -> dict:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            status = self.get_import_status(project_id)
            import_status = status.get("import_status")
            if import_status == "finished":
                return status
            if import_status in {"failed", "none"}:
                raise GitLabError(f"Import failed for project {project_id}: {status}")
            time.sleep(poll)
        raise GitLabError(f"Timed out waiting for import of project {project_id}")
