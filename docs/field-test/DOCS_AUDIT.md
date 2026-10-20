# Documentation Audit & Update List (Field Test Follow-Up)

**Context:** Following resolution of issues identified in field tests `Run A` and `Run B`, the following documentation surfaces have been updated or require alignment in subsequent documentation passes:

---

## 1. Updates Completed in Current Pass
- **`mesh/skills/mesh-context-layer/SKILL.md`**:
  - Added `status="reopened"` to task lifecycle instructions and Decision Table.
  - Added rule in Section 2 to create tasks/goals via `mesh_task_create` before updating or completing them.
  - Validated with `scripts/check_skill.py`.
- **`mesh/mcp_server.py`**:
  - Added `"reopened"` to `mesh_task_update` inputSchema enum and updated description.
- **`CHANGELOG.md`**:
  - Appended field-test resolutions under version `0.8.0` / upcoming release notes.

---

## 2. Recommended Follow-Up Documentation Pass Items

| File | Target Section | Recommended Update |
|---|---|---|
| `docs/DEVELOPER_GUIDE.md` | "Python SDK Usage" | Document method aliases (`mesh.task_create`, `mesh.task_update`, `mesh.task_complete`, `mesh.task_list`, `mesh.log`) alongside existing methods. |
| `docs/DEVELOPER_GUIDE.md` | "CLI Reference" | Note `mesh mcp` subcommand alias for `mesh server`. |
| `docs/DEVELOPER_GUIDE.md` | "Installer & Scopes" | Document `--mesh-home` flag and project-scoped `.mesh-home` directory binding. |
| `docs/ARCHITECTURE.md` | "Work Items & Tasks" | Note lifecycle transition support for `reopened` status (mapping to `todo` with `work_events` audit trail). |
| `mesh/skills/mesh-context-layer/references/tool_reference.md` | `mesh_task_update` | Add example showing reopen transition: `mesh_task_update(item_id="task-1", status="reopened", note="Issue recurred")`. |
| `README.md` | "Quickstart / CLI" | Highlight `mesh install --apply` for non-interactive CI/CD and automation scripts. |
