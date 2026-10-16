"""To-do backends. With ASANA_TOKEN set, to-dos live in Asana (your My Tasks); otherwise in HQ's own database.

Both return the same shape: {id, title, notes, due, area, project_id, priority, done, url}. IDs are strings.
detail() adds subtasks, comments and the projects a task can be moved to.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from .config import hq_settings
from .store import HQStore

log = logging.getLogger(__name__)

ASANA_API = "https://app.asana.com/api/1.0"
TASK_FIELDS = "name,notes,due_on,completed,completed_at,permalink_url,projects.name"
# HQ's own areas, used as "projects" when to-dos live in HQ rather than Asana.
AREAS = ["business", "clients", "content", "personal", "health", "home"]
# How far back the "Done" list looks in Asana.
DONE_LOOKBACK_DAYS = 14


def sort_todos(todos: list[dict]) -> list[dict]:
    """Open first, then by due date (someday last), important first."""
    return sorted(todos, key=lambda t: (t["done"], t["due"] is None, t["due"] or "", -t["priority"], t["title"].lower()))


class LocalTodos:
    source = "hq"

    def __init__(self, store: HQStore):
        self.store = store

    @staticmethod
    def _out(t: dict | None) -> dict | None:
        if t is None:
            return None
        return {**t, "id": str(t["id"]), "url": "", "project_id": t["area"]}

    async def detail(self, todo_id: str) -> dict | None:
        todo = self._out(self.store.get_todo(int(todo_id))) if str(todo_id).isdigit() else None
        if todo is None:
            return None
        return {
            **todo,
            "subtasks": [{"id": str(s["id"]), "title": s["title"], "done": bool(s["done"])}
                         for s in self.store.list_subtasks(int(todo_id))],
            "comments": [],
            "supports_comments": False,
            "projects": [{"id": a, "name": a.capitalize()} for a in AREAS],
        }

    async def add_subtask(self, todo_id: str, title: str) -> dict:
        s = self.store.add_subtask(int(todo_id), title)
        return {"id": str(s["id"]), "title": s["title"], "done": bool(s["done"])}

    async def update_subtask(self, todo_id: str, subtask_id: str, title: str | None = None,
                             done: bool | None = None) -> dict | None:
        s = self.store.update_subtask(int(todo_id), int(subtask_id), title=title, done=done)
        return {"id": str(s["id"]), "title": s["title"], "done": bool(s["done"])} if s else None

    async def delete_subtask(self, todo_id: str, subtask_id: str) -> None:
        self.store.delete_subtask(int(todo_id), int(subtask_id))

    async def add_comment(self, todo_id: str, text: str) -> dict:
        raise NotImplementedError("Comments need Asana connected")

    async def list(self, include_done: bool = False) -> list[dict]:
        return [self._out(t) for t in self.store.list_todos(include_done=include_done)]

    async def add(self, title: str, notes: str = "", due: str | None = None, area: str = "business",
                  priority: int = 0) -> dict:
        return self._out(self.store.add_todo(title, notes, due, area, priority))

    async def update(self, todo_id: str, **changes) -> dict | None:
        if not str(todo_id).isdigit():
            return None
        if "project" in changes:
            changes["area"] = changes.pop("project") or "business"
        return self._out(self.store.update_todo(int(todo_id), **changes))

    async def delete(self, todo_id: str) -> None:
        if str(todo_id).isdigit():
            self.store.delete_todo(int(todo_id))


class AsanaTodos:
    """Your Asana My Tasks. HQ keeps only the star and area for each task locally, since Asana has no equivalent."""

    source = "asana"

    def __init__(self, store: HQStore, token: str, workspace: str = "", project: str = "",
                 transport: httpx.AsyncBaseTransport | None = None):
        self.store = store
        self._transport = transport
        self.workspace = workspace
        self.project = project
        self._token = token
        self._projects: tuple[float, list[dict]] | None = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=ASANA_API, headers={"Authorization": f"Bearer {self._token}"},
                                 timeout=20.0, transport=self._transport)

    async def _workspace(self, http: httpx.AsyncClient) -> str:
        if not self.workspace:
            resp = await http.get("/users/me", params={"opt_fields": "workspaces"})
            resp.raise_for_status()
            workspaces = resp.json()["data"]["workspaces"]
            if not workspaces:
                raise RuntimeError("This Asana account has no workspace")
            self.workspace = workspaces[0]["gid"]
        return self.workspace

    def _out(self, task: dict) -> dict:
        flags = self.store.todo_flags(task["gid"])
        projects = [p.get("name", "") for p in task.get("projects") or [] if p.get("name")]
        project_ids = [p["gid"] for p in task.get("projects") or [] if p.get("gid")]
        return {
            "id": task["gid"],
            "project_id": project_ids[0] if project_ids else "",
            "title": task.get("name") or "(untitled)",
            "notes": task.get("notes") or "",
            "due": task.get("due_on"),
            "area": flags.get("area") or (projects[0] if projects else "My Tasks"),
            "priority": flags.get("priority", 0),
            "done": bool(task.get("completed")),
            "url": task.get("permalink_url") or "",
        }

    async def list(self, include_done: bool = False) -> list[dict]:
        since = "now"
        if include_done:
            since = (datetime.now(timezone.utc) - timedelta(days=DONE_LOOKBACK_DAYS)).isoformat()
        tasks, offset = [], None
        async with self._client() as http:
            params = {"assignee": "me", "workspace": await self._workspace(http), "completed_since": since,
                      "opt_fields": TASK_FIELDS, "limit": 100}
            for _ in range(10):  # up to 1,000 tasks
                if offset:
                    params["offset"] = offset
                resp = await http.get("/tasks", params=params)
                resp.raise_for_status()
                body = resp.json()
                tasks.extend(body.get("data", []))
                offset = (body.get("next_page") or {}).get("offset")
                if not offset:
                    break
        return sort_todos([self._out(t) for t in tasks])

    async def add(self, title: str, notes: str = "", due: str | None = None, area: str = "business",
                  priority: int = 0) -> dict:
        async with self._client() as http:
            data = {"name": title.strip(), "notes": notes, "assignee": "me", "workspace": await self._workspace(http)}
            if due:
                data["due_on"] = due
            if self.project:
                data["projects"] = [self.project]
            resp = await http.post("/tasks", json={"data": data}, params={"opt_fields": TASK_FIELDS})
            resp.raise_for_status()
            task = resp.json()["data"]
        self.store.set_todo_flags(task["gid"], priority=int(priority), area=area or "")
        return self._out(task)

    async def update(self, todo_id: str, **changes) -> dict | None:
        if "project" in changes:
            await self.set_project(todo_id, changes.pop("project") or "")
            changes["area"] = ""  # show the Asana project's name from now on
        flag_changes = {k: changes.pop(k) for k in ("priority", "area") if k in changes}
        if flag_changes:
            self.store.set_todo_flags(todo_id, **flag_changes)
        data = {}
        if "title" in changes:
            data["name"] = changes["title"]
        if "notes" in changes:
            data["notes"] = changes["notes"]
        if "due" in changes:
            data["due_on"] = changes["due"] or None
        if "done" in changes:
            data["completed"] = bool(changes["done"])
        async with self._client() as http:
            if data:
                resp = await http.put(f"/tasks/{todo_id}", json={"data": data}, params={"opt_fields": TASK_FIELDS})
            else:
                resp = await http.get(f"/tasks/{todo_id}", params={"opt_fields": TASK_FIELDS})
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return self._out(resp.json()["data"])

    async def delete(self, todo_id: str) -> None:
        async with self._client() as http:
            resp = await http.delete(f"/tasks/{todo_id}")
            if resp.status_code != 404:
                resp.raise_for_status()
        self.store.clear_todo_flags(todo_id)

    # The full task screen: subtasks, comments, projects

    async def projects(self) -> list[dict]:
        if self._projects and time.monotonic() - self._projects[0] < 600:
            return self._projects[1]
        async with self._client() as http:
            resp = await http.get("/projects", params={
                "workspace": await self._workspace(http), "archived": "false", "opt_fields": "name", "limit": 100})
            resp.raise_for_status()
        found = sorted(({"id": p["gid"], "name": p.get("name") or "(untitled)"} for p in resp.json().get("data", [])),
                       key=lambda p: p["name"].lower())
        self._projects = (time.monotonic(), found)
        return found

    async def detail(self, todo_id: str) -> dict | None:
        async with self._client() as http:
            resp = await http.get(f"/tasks/{todo_id}", params={"opt_fields": TASK_FIELDS})
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            task = resp.json()["data"]
            subs = await http.get(f"/tasks/{todo_id}/subtasks", params={"opt_fields": "name,completed"})
            subs.raise_for_status()
            stories = await http.get(f"/tasks/{todo_id}/stories",
                                     params={"opt_fields": "type,text,created_at,created_by.name"})
            stories.raise_for_status()
        return {
            **self._out(task),
            "subtasks": [{"id": s["gid"], "title": s.get("name") or "", "done": bool(s.get("completed"))}
                         for s in subs.json().get("data", [])],
            "comments": [_comment(c) for c in stories.json().get("data", []) if c.get("type") == "comment"],
            "supports_comments": True,
            "projects": await self.projects(),
        }

    async def set_project(self, todo_id: str, project_id: str) -> None:
        """Moves a task to one project (or none), leaving it assigned to you."""
        async with self._client() as http:
            resp = await http.get(f"/tasks/{todo_id}", params={"opt_fields": "projects"})
            resp.raise_for_status()
            current = [p["gid"] for p in resp.json()["data"].get("projects") or []]
            for gid in current:
                if gid != project_id:
                    (await http.post(f"/tasks/{todo_id}/removeProject", json={"data": {"project": gid}})).raise_for_status()
            if project_id and project_id not in current:
                (await http.post(f"/tasks/{todo_id}/addProject", json={"data": {"project": project_id}})).raise_for_status()

    async def add_subtask(self, todo_id: str, title: str) -> dict:
        async with self._client() as http:
            resp = await http.post(f"/tasks/{todo_id}/subtasks", json={"data": {"name": title.strip()}},
                                   params={"opt_fields": "name,completed"})
            resp.raise_for_status()
        s = resp.json()["data"]
        return {"id": s["gid"], "title": s.get("name") or "", "done": bool(s.get("completed"))}

    async def update_subtask(self, todo_id: str, subtask_id: str, title: str | None = None,
                             done: bool | None = None) -> dict | None:
        data = {}
        if title is not None:
            data["name"] = title
        if done is not None:
            data["completed"] = bool(done)
        async with self._client() as http:
            resp = await http.put(f"/tasks/{subtask_id}", json={"data": data}, params={"opt_fields": "name,completed"})
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
        s = resp.json()["data"]
        return {"id": s["gid"], "title": s.get("name") or "", "done": bool(s.get("completed"))}

    async def delete_subtask(self, todo_id: str, subtask_id: str) -> None:
        async with self._client() as http:
            resp = await http.delete(f"/tasks/{subtask_id}")
            if resp.status_code != 404:
                resp.raise_for_status()

    async def add_comment(self, todo_id: str, text: str) -> dict:
        async with self._client() as http:
            resp = await http.post(f"/tasks/{todo_id}/stories", json={"data": {"text": text.strip()}},
                                   params={"opt_fields": "type,text,created_at,created_by.name"})
            resp.raise_for_status()
        return _comment(resp.json()["data"])


def _comment(story: dict) -> dict:
    return {
        "id": story.get("gid", ""),
        "text": story.get("text") or "",
        "author": (story.get("created_by") or {}).get("name") or "",
        "at": story.get("created_at") or "",
    }


def todo_backend(store: HQStore) -> LocalTodos | AsanaTodos:
    if hq_settings.asana_token:
        return AsanaTodos(store, hq_settings.asana_token, hq_settings.asana_workspace, hq_settings.asana_project)
    return LocalTodos(store)
