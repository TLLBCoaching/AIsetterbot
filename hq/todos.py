"""To-do backends. With ASANA_TOKEN set, to-dos live in Asana (your My Tasks); otherwise in HQ's own database.

Both return the same shape: {id, title, notes, due, area, priority, done, url}. IDs are strings.
"""

import logging
from datetime import datetime, timedelta, timezone

import httpx

from .config import hq_settings
from .store import HQStore

log = logging.getLogger(__name__)

ASANA_API = "https://app.asana.com/api/1.0"
TASK_FIELDS = "name,notes,due_on,completed,completed_at,permalink_url,projects.name"
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
        return {**t, "id": str(t["id"]), "url": ""}

    async def list(self, include_done: bool = False) -> list[dict]:
        return [self._out(t) for t in self.store.list_todos(include_done=include_done)]

    async def add(self, title: str, notes: str = "", due: str | None = None, area: str = "business",
                  priority: int = 0) -> dict:
        return self._out(self.store.add_todo(title, notes, due, area, priority))

    async def update(self, todo_id: str, **changes) -> dict | None:
        if not str(todo_id).isdigit():
            return None
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
        return {
            "id": task["gid"],
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


def todo_backend(store: HQStore) -> LocalTodos | AsanaTodos:
    if hq_settings.asana_token:
        return AsanaTodos(store, hq_settings.asana_token, hq_settings.asana_workspace, hq_settings.asana_project)
    return LocalTodos(store)
