# Revit MCP Bridge

Bridge between Claude Code and Autodesk Revit via [pyRevit](https://github.com/pyrevitlabs/pyRevit) Routes. Enables Claude Code to create, query, and modify Revit models in real time through MCP (Model Context Protocol) tools.

## Architecture

```
Claude Code  <--stdio/MCP-->  revit-mcp-server  <--HTTP/JSON-->  pyRevit Routes  <--Revit API-->  Revit Model
```

Two components:

1. **`camper-mcp.extension/`** — A pyRevit extension that runs inside Revit's process, exposing the Revit API over HTTP on `localhost:48884`.
2. **`revit-mcp-server/`** — A standalone Python (CPython 3.10+) MCP server that Claude Code connects to over stdio. It translates MCP tool calls into HTTP requests to the pyRevit extension.

## Prerequisites

- Windows 10/11
- Autodesk Revit 2022+ (tested on Revit 2026)
- [pyRevit 6.4.0+](https://github.com/pyrevitlabs/pyRevit/releases) installed and loading in Revit
- Python 3.10+ (CPython) for the MCP server
- `mcp` and `httpx` Python packages

**Check for existing pyRevit installations first.** Run `pyrevit` in PowerShell — if you get anything other than "command not found", there's a previous installation on this machine. Follow [docs/guides/pyrevit-clean-slate.md](docs/guides/pyrevit-clean-slate.md) to fully remove all traces before proceeding. Leftover addin files, clones, or config from old installations will cause failures ranging from Revit crashes on startup to extensions that silently refuse to load.

## Setup

### 1. Install Python dependencies for the MCP server

```powershell
pip install mcp httpx
```

### 2. Enable pyRevit Routes

Configure pyRevit to run its HTTP server on localhost:

```powershell
pyrevit configs routes-host "127.0.0.1"
pyrevit configs routes-port 48884
```

Alternative: edit `%APPDATA%\pyRevit\pyRevit_config.ini` directly:

```ini
[routes]
host = 127.0.0.1
port = 48884
```

Restart Revit after changing this.

### 3. Deploy the pyRevit extension

Copy `camper-mcp.extension/` into pyRevit's extensions directory:

```powershell
xcopy /E /I "camper-mcp.extension" "%APPDATA%\pyRevit\Extensions\camper-mcp.extension"
```

Find your extensions path with `pyrevit extensions paths` if the default doesn't work.

Restart Revit (or reload pyRevit from the ribbon).

### 4. Verify the extension loaded

With Revit running and a document open:

```powershell
curl http://127.0.0.1:48884/camper-mcp/health
```

Expected response:

```json
{
  "status": "ok",
  "extension": "camper-mcp",
  "revit_version": "2026",
  "doc_title": "YourProject.rvt",
  "api_version": "1.0.0"
}
```

If you get `RouteHandlerNotDefinedException`, check the pyRevit runtime log for startup errors:

```powershell
Get-ChildItem "$env:APPDATA\pyRevit" -Recurse -Filter "*runtime.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 1 | Get-Content
```

### 5. Configure Claude Code

Register the MCP server at the **user level** so it's available from any project directory. Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "revit": {
      "command": "python",
      "args": ["C:/absolute/path/to/revit-mcp-server/server.py"],
      "env": {
        "REVIT_ROUTES_HOST": "127.0.0.1",
        "REVIT_ROUTES_PORT": "48884"
      }
    }
  }
}
```

Replace `C:/absolute/path/to/` with the actual path to where you cloned this repo. The MCP server must be registered at the user level (not project level) so Claude Code can connect to Revit regardless of which directory it's launched from.

## Usage

Once set up, Claude Code has access to 31 tools for interacting with Revit. No special commands needed — just describe what you want to do with the model.

### Available tools

| Tool | Description |
|------|-------------|
| `revit_health` | Check connection to Revit |
| `revit_execute` | Run arbitrary Python code inside Revit |
| `revit_model_summary` | High-level overview of the entire model |
| `revit_list_elements` | List elements by category |
| `revit_get_element` | Get full detail for a single element |
| `revit_set_parameters` | Set parameters on an element |
| `revit_list_panels` | List all panels (phases) with part counts |
| `revit_get_panel` | Get all parts on a panel |
| `revit_place_part` | Place a part family instance on a panel |
| `revit_batch_place_parts` | Place multiple parts in one transaction |
| `revit_create_panel` | Create a new panel (phase + reference plane) |
| `revit_generate_cutlist` | Generate cut lists grouped/sorted by family, thickness, or panel |
| `revit_list_planes` | List named reference planes |
| `revit_list_types` | List family types for a category |
| `revit_list_levels` | List all levels |
| `revit_get_schedules` | List schedules |
| `revit_export_schedule` | Export schedule as structured data |
| `revit_create_view` | Create floor plans, sections, elevations, 3D views |
| `revit_delete_elements` | Delete elements |
| `revit_get_materials` | Material quantities for an element |
| `revit_convert_units` | Convert between feet, inches, mm, meters |
| `revit_spatial_query` | Find elements by bounding box or proximity |
| `revit_element_connections` | Get joined/hosted/touching elements |
| `revit_model_bom` | Generate bill of materials |
| `revit_parameter_schema` | List all shared/project parameters with sample values |
| `revit_family_info` | Loaded families with types, parameters, hosting behavior |
| `revit_assembly_detail` | Assembly contents, views, and sheets |
| `revit_view_contents` | Elements visible in a view |
| `revit_sheet_index` | All sheets with placed views |
| `revit_warnings` | Active model warnings |
| `revit_session_stats` | Tool call counts and response times for this session |

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REVIT_ROUTES_HOST` | `127.0.0.1` | pyRevit Routes server host |
| `REVIT_ROUTES_PORT` | `48884` | pyRevit Routes server port |
| `REVIT_REQUEST_TIMEOUT` | `60` | HTTP request timeout (seconds) |
| `REVIT_CODE_TIMEOUT` | `30` | Default code execution timeout (seconds) |

## Troubleshooting

**"Cannot connect to Revit"** — Revit isn't running, or Routes isn't enabled. Check that pyRevit is loaded (look for the pyRevit tab in Revit's ribbon) and that Routes is configured on the expected port.

**`RouteHandlerNotDefinedException`** — The extension's startup script failed. Check the runtime log at `%APPDATA%\pyRevit\<year>\pyRevit_<year>_<pid>_runtime.log`.

**Port conflict** — If multiple Revit instances are open, each claims the next available port (48884, 48885, ...). Set `REVIT_ROUTES_PORT` to match the instance you want.

**Slow pyRevit reload** — Reloading takes 30+ seconds and Revit will appear frozen. This is normal. Restarting Revit entirely is sometimes faster.

## Development

### Running tests

The MCP server has conformance tests that run without Revit:

```powershell
pip install pytest respx
python -m pytest tests/ -v
```

### Extension development notes

All code in `camper-mcp.extension/` runs under **IronPython 2.7** inside Revit's process:

- Every `.py` file must start with `# -*- coding: utf-8 -*-`
- No f-strings, no Python 3 syntax — use `.format()` for string formatting
- Import Revit API via `from pyrevit.api import DB, UI` (not `from Autodesk.Revit import DB`)
- After modifying extension files, redeploy to `%APPDATA%\pyRevit\Extensions\` and reload pyRevit or restart Revit

The MCP server (`revit-mcp-server/`) runs under CPython 3.10+ and can use modern Python.

## Spec

Full specification: [docs/specs/2026-04-10-revit-mcp-bridge.md](docs/specs/2026-04-10-revit-mcp-bridge.md)
