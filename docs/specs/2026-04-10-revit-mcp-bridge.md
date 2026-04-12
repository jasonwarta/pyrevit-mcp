# Revit MCP Bridge Service Specification

Status: Draft v1

Purpose: Enable Claude Code to interact with Autodesk Revit in real-time by bridging MCP (Model Context Protocol) to pyRevit's built-in HTTP Routes server.

## 1. Problem Statement

The Revit MCP Bridge is a two-component system that allows an AI assistant (Claude Code) to create, query, and modify Revit models through a live bidirectional connection. It consists of a pyRevit extension that exposes the Revit API over HTTP, and an MCP server that translates between Claude Code's tool-call protocol and that HTTP API.

Problems it solves:

- **Manual scripting loop**: Without a live connection, the user must copy-paste scripts between Claude Code and Revit, manually relay output back, and repeat — slow and error-prone.
- **No programmatic access to Revit from external processes**: Revit's API is only accessible from within its process. pyRevit Routes bridges this gap by running an HTTP server inside Revit.
- **Repetitive camper model creation**: Building structurally similar camper models with varying specs requires re-doing the same Revit operations. Automation through a live connection enables parametric, repeatable model generation.
- **Production document generation overhead**: Cut lists, BOMs, and schedules are generated manually from finished models. A live connection allows Claude Code to drive schedule creation and data extraction programmatically.

### 1.1 Important Boundary

This system is NOT responsible for:

- Revit licensing, installation, or updates.
- Replacing Revit's GUI for design exploration or visual review — the user still works in Revit visually.
- Managing Revit family libraries — it references families that already exist in the project or loaded libraries.
- Network access or multi-user Revit collaboration (worksharing). This system operates on a single local Revit instance.
- Persisting state between Revit sessions — each session starts fresh.

## 2. Goals and Non-Goals

### 2.1 Goals

- Install pyRevit on the user's machine with Routes capability enabled.
- Deploy a pyRevit extension that exposes a generic code-execution endpoint plus convenience endpoints for common Revit operations.
- Build an MCP server (Python, stdio transport) that Claude Code connects to and that proxies tool calls to pyRevit Routes over HTTP.
- Achieve round-trip latency under 5 seconds for simple operations (create a wall, query an element).
- Return structured, parseable JSON responses for all operations, including errors.
- Support the full Revit API surface through a generic `execute_code` tool without requiring per-method wrappers.
- Provide typed convenience tools for the 20-30 most common operations (element CRUD, parameter get/set, family placement, view/schedule management, model discovery).
- Support model discovery — enable Claude Code to reverse-engineer an existing Revit model's structure, relationships, materials, and parameter schemas without prior knowledge of the project.
- Run entirely on localhost with no external network dependencies.

### 2.2 Non-Goals

- Supporting multiple simultaneous Revit instances from a single MCP server. One MCP server connects to one Revit instance.
- Providing a GUI or web dashboard. The interface is Claude Code's terminal.
- Implementing undo/redo management. The user uses Revit's native undo stack.
- Supporting Revit versions older than 2022. Revit 2026+ requires pyRevit 6.x (netcore/.NET 8). Revit 2025 and older can use pyRevit 5.x or 6.x (netfx/.NET Framework). pyRevit 5.x will NOT load on Revit 2026+.
- Implementing authentication or access control on the HTTP endpoints. The server binds to localhost only.
- Streaming partial results for long-running operations (future extension).

## 3. System Overview

### 3.1 Main Components

1. `pyRevit` — The extension framework that runs inside Revit's process. Provides the Routes HTTP server infrastructure and IronPython/CPython scripting runtime.

2. `camper-mcp.extension` — A pyRevit extension (deployed into pyRevit's extensions directory) containing:
   - Route definitions for the HTTP API.
   - Handler functions that execute Revit API operations.
   - Serialization utilities for converting Revit objects to JSON-safe representations.

3. `revit-mcp-server` — A standalone Python process (CPython 3.10+) that:
   - Implements the MCP protocol over stdio.
   - Exposes tools that Claude Code can call.
   - Translates tool calls into HTTP requests to pyRevit Routes.
   - Parses responses and returns structured results to Claude Code.

### 3.2 Data Flow

```
Claude Code  <--stdio/MCP-->  revit-mcp-server  <--HTTP/JSON-->  pyRevit Routes  <--Revit API-->  Revit Model
```

All communication is local (localhost). The MCP server is a stateless proxy — it holds no model state. All state lives in the Revit document.

### 3.3 Abstraction Layers

- **MCP Layer** (`revit-mcp-server`): Protocol translation. Knows MCP tool schemas, knows nothing about Revit internals.
- **HTTP Transport Layer**: JSON over HTTP between the MCP server and pyRevit. Standard request/response.
- **Revit API Layer** (`camper-mcp.extension`): Knows Revit API. Executes operations inside Revit's process on the main thread via `ExternalEvent`. Knows nothing about MCP.

### 3.4 External Dependencies

- **Autodesk Revit 2022-2026** — The host application.
- **pyRevit 6.4.0+** — Extension framework with Routes support. Free, open-source. **Must be 6.x for Revit 2026+** (5.x uses .NET Framework and will not load on Revit 2026's .NET 8 runtime).
- **Python 3.10+** (CPython) — Runtime for the MCP server. Separate from pyRevit's embedded runtime.
- **`mcp` Python package** — The MCP SDK for building servers (`pip install mcp`).
- **`httpx` Python package** — HTTP client for the MCP server to call pyRevit Routes.

## 4. Core Domain Model

### 4.1 Entities

#### 4.1.1 RevitCodePayload

Represents a block of Python code to be executed inside Revit's context.

Fields:
- `code` (string) — Python source code to execute. Must be valid IronPython 2.7 or CPython 3.x depending on pyRevit engine configuration. Required.
- `timeout` (integer) — Maximum execution time in seconds. Default: 30. If exceeded, the operation is aborted and an error is returned.
- `transaction_name` (string) — Name for the Revit transaction that wraps model-modifying operations. Default: `"MCP Operation"`. If null, no transaction is created (read-only operation).

#### 4.1.2 RevitCodeResult

Represents the result of executing a code payload.

Fields:
- `success` (boolean) — Whether execution completed without exception.
- `result` (any) — The return value of the executed code, JSON-serialized. Null if the code returned None or raised an exception.
- `error` (string) — Error message if `success` is false. Null otherwise.
- `traceback` (string) — Python traceback string if an exception occurred. Null otherwise.
- `execution_time_ms` (integer) — Wall-clock execution time in milliseconds.
- `transaction_status` (string) — One of: `"committed"`, `"rolled_back"`, `"no_transaction"`. Indicates what happened with the Revit transaction.

#### 4.1.3 ElementSummary

A lightweight JSON-safe representation of a Revit element, used in convenience tool responses.

Fields:
- `element_id` (integer) — Revit ElementId as integer.
- `category` (string) — Revit category name (e.g., `"Walls"`, `"Floors"`, `"Windows"`).
- `family` (string) — Family name. Null for system families.
- `type` (string) — Type name.
- `name` (string) — Element name (from the `Name` property or the Mark parameter).
- `parameters` (dict) — Key-value map of requested parameter names to their values. Only populated when parameters are explicitly requested.
- `location` (dict) — Simplified location: `{"type": "point", "x": float, "y": float, "z": float}` or `{"type": "curve", "start": {...}, "end": {...}}` or null for area-based elements.

#### 4.1.4 PanelSummary

A JSON representation of a panel (Revit phase) with its parts.

Fields:
- `phase_id` (integer) — Revit ElementId of the phase.
- `phase_name` (string) — Phase name (e.g., "Driver Side Front").
- `reference_plane` (dict) — `{"name": string, "element_id": integer}` or null if no associated plane.
- `part_count` (integer) — Number of family instances in this phase.
- `parts_by_family` (dict) — Map of family name to count (e.g., `{"Rail_1.5": 2, "Stud_1.5": 8}`).

#### 4.1.5 PartInstance

A JSON representation of a single part (custom family instance) on a panel.

Fields:
- `element_id` (integer) — Revit ElementId.
- `family` (string) — Family name (e.g., "Rail_1.5", "Stud_1.5", "Sheathing_1in").
- `type` (string) — Type name within the family (e.g., "Standard").
- `parameters` (dict) — All instance parameters as key-value pairs. Always includes `Width`, `Length`, `Offset`. May include additional family-specific parameters.
- `location` (dict) — Insertion point as `{"x": float, "y": float, "z": float}` in feet.
- `rotation_degrees` (float) — Z-axis rotation at insertion point.

#### 4.1.6 ToolResponse

The standard MCP tool response wrapper.

Fields:
- `content` (list) — List of content blocks per MCP spec. Each block has `type` (always `"text"`) and `text` (JSON string of the result payload).
- `isError` (boolean) — True if the tool call failed.

### 4.2 Normalization Rules

- **Element IDs**: Always transmitted as integers. The pyRevit extension converts between `DB.ElementId` and `int` at the boundary.
- **Units — coordinates**: All XYZ location values are in **feet** (Revit's internal unit).
- **Units — part parameters**: Part family parameters (Width, Length, Offset) are in **inches**, matching how the families store them and how technicians read cut lists. The MCP server provides a `convert_units` tool for cross-unit conversion. Conversion formula: `feet = inches / 12`, `feet = mm / 304.8`, `feet = meters / 0.3048`.
- **Parameter names**: Matched case-insensitively with whitespace trimmed. The extension normalizes `parameter_name.strip().lower()` before lookup.
- **Family + Type names**: Matched as `"FamilyName : TypeName"` with colon separator, trimmed. Comparison is case-insensitive.
- **Coordinates**: All XYZ values are in Revit's internal coordinate system (feet, right-hand, Z-up).

## 5. Installation and Setup

### 5.1 pyRevit Installation

#### 5.1.1 Prerequisites

- Windows 10/11 (Revit is Windows-only).
- Autodesk Revit 2022 or newer installed and activated.
- Administrator access for initial installation.
- .NET 8 runtime (ships with Revit 2026+). For Revit 2025 and older: .NET Framework 4.8+ (ships with modern Windows).
- **If upgrading from pyRevit 5.x**: must fully uninstall 5.x before installing 6.x. The two versions use different runtimes and cannot coexist.

#### 5.1.2 Installation Procedure

Install pyRevit using the CLI installer (preferred for repeatability):

```powershell
# Download the pyRevit CLI installer
Invoke-WebRequest -Uri "https://github.com/pyrevitlabs/pyRevit/releases/latest/download/pyRevit_CLI_signed.exe" -OutFile "$env:TEMP\pyRevit_CLI.exe"

# Install pyRevit (current user, default path)
& "$env:TEMP\pyRevit_CLI.exe" install

# Verify installation
pyrevit --version
```

Alternative: download the `.exe` installer from [pyRevit Releases](https://github.com/pyrevitlabs/pyRevit/releases) and run it. Accept defaults.

#### 5.1.3 Verification

After installation and restarting Revit:
- The pyRevit tab should appear in Revit's ribbon.
- Open pyRevit Settings (pyRevit tab → Settings gear icon) and confirm the version is 6.4.0+.
- If upgrading from 5.x, verify the old version is fully removed first (`pyrevit uninstall` or remove via Add/Remove Programs).

### 5.2 Routes Server Configuration

#### 5.2.1 Enable Routes

Routes must be enabled in pyRevit's configuration. There are two methods:

**Method A — pyRevit CLI:**

```powershell
pyrevit configs routes-host "127.0.0.1"
pyrevit configs routes-port 48884
```

**Method B — pyRevit Settings GUI:**

1. Open Revit.
2. pyRevit tab → Settings (gear icon).
3. Navigate to the "Routes" section.
4. Set Host to `127.0.0.1`.
5. Set Port to `48884`.
6. Save and restart Revit.

**Method C — Direct config file edit:**

Edit `%APPDATA%\pyRevit\pyRevit_config.ini`:

```ini
[routes]
host = 127.0.0.1
port = 48884
```

#### 5.2.2 Configuration Fields

| Field | Type | Default | Dynamic Reload | Notes |
|-------|------|---------|----------------|-------|
| `host` | string | `""` (all interfaces) | No — requires Revit restart | Use `127.0.0.1` to restrict to localhost. Empty string binds to `0.0.0.0`. |
| `port` | integer | `48884` | No — requires Revit restart | If multiple Revit instances are open, each auto-increments from this base port. |

#### 5.2.3 Validation

After restarting Revit with Routes enabled, verify the server is running:

```powershell
curl http://127.0.0.1:48884/routes/status
```

Expected response:

```json
{
  "host": "Autodesk Revit 2025",
  "username": "jasonwarta",
  "session_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
}
```

If this fails:
- **Connection refused**: Routes server did not start. Check pyRevit config. Check Revit's output window for pyRevit errors.
- **Wrong port**: If another Revit instance started first, it claimed port 48884, and the second instance is on 48885. Query both.
- **Firewall**: Windows Defender Firewall may block even localhost connections. Add an inbound rule for the port, or temporarily disable for testing.

### 5.3 pyRevit Extension Deployment

#### 5.3.1 Extension Directory Structure

```
camper-mcp.extension/
├── startup.py                  # Registers all API routes on pyRevit load
├── lib/
│   ├── mcp_routes.py           # Route handler functions
│   ├── serializers.py          # Revit object → JSON converters
│   └── transaction_utils.py    # Transaction wrapper utilities
```

#### 5.3.2 Deployment Location

pyRevit loads extensions from its configured extensions directory. Default:

```
%APPDATA%\pyRevit\Extensions\
```

To find the actual path:

```powershell
pyrevit extensions paths
```

Copy the `camper-mcp.extension` folder into that directory. Restart Revit (or reload pyRevit via the ribbon) to activate.

#### 5.3.3 Extension Registration

pyRevit auto-discovers `.extension` folders. No additional registration is needed. On startup, pyRevit executes `startup.py`, which registers all routes with the Routes server.

#### 5.3.4 Verification

After deployment and Revit restart:

```powershell
curl http://127.0.0.1:48884/camper-mcp/health
```

Expected response:

```json
{
  "status": "ok",
  "extension": "camper-mcp",
  "revit_version": "2025",
  "doc_title": "MyProject.rvt",
  "api_version": "1.0.0"
}
```

## 6. pyRevit Extension Specification (`camper-mcp.extension`)

### 6.1 API Name and Base Path

- API name: `camper-mcp`
- All routes are prefixed: `http://<host>:<port>/camper-mcp/...`

### 6.2 Route Definitions

#### 6.2.1 Health Check

```
GET /camper-mcp/health
```

No Revit API context needed. Returns server and document status.

Response (200):
```json
{
  "status": "ok",
  "extension": "camper-mcp",
  "revit_version": "2025",
  "doc_title": "Project1.rvt",
  "api_version": "1.0.0"
}
```

If no document is open, `doc_title` is `null`.

#### 6.2.2 Execute Code (Generic)

```
POST /camper-mcp/execute
```

Requires Revit API context (`uiapp`). This is the primary power tool — it executes arbitrary Python code inside Revit's process with full API access.

Request body:
```json
{
  "code": "result = doc.Title\nreturn result",
  "timeout": 30,
  "transaction_name": null
}
```

The handler wraps the code in a function body. The code block:
- Has access to pre-injected variables: `uiapp`, `uidoc`, `doc`, `DB` (Revit.DB namespace), `UI` (Revit.UI namespace).
- Must assign the return value to a variable named `__result__`, OR use `return` (the handler wraps the code in a function).
- If `transaction_name` is non-null, a `Transaction` is opened before execution and committed after (or rolled back on error).

Response (200):
```json
{
  "success": true,
  "result": "Project1.rvt",
  "error": null,
  "traceback": null,
  "execution_time_ms": 12,
  "transaction_status": "no_transaction"
}
```

Error response (200 — application-level error, not HTTP error):
```json
{
  "success": false,
  "result": null,
  "error": "NameError: name 'foo' is not defined",
  "traceback": "  File \"<mcp-exec>\", line 3, in _execute\n    foo.bar()\n",
  "execution_time_ms": 2,
  "transaction_status": "rolled_back"
}
```

#### 6.2.3 List Elements by Category

```
GET /camper-mcp/elements/<category>
```

Requires Revit API context (`doc`). Returns all elements of a given built-in category.

Path parameter:
- `category` (string) — Revit built-in category name, case-insensitive. Examples: `walls`, `floors`, `windows`, `doors`, `furniture`, `structuralframing`.

Query parameters (via request data, since pyRevit Routes doesn't parse query strings natively — pass as JSON body with GET or use POST):
- `include_parameters` (list of string) — Parameter names to include in the response. Default: empty (no parameters returned).

Response (200):
```json
{
  "count": 3,
  "elements": [
    {
      "element_id": 12345,
      "category": "Walls",
      "family": null,
      "type": "Generic - 6\"",
      "name": "Generic - 6\"",
      "location": {"type": "curve", "start": {"x": 0.0, "y": 0.0, "z": 0.0}, "end": {"x": 16.0, "y": 0.0, "z": 0.0}},
      "parameters": {}
    }
  ]
}
```

#### 6.2.4 Get Element by ID

```
GET /camper-mcp/elements/id/<int:element_id>
```

Requires Revit API context (`doc`). Returns full detail for a single element.

Response (200):
```json
{
  "element_id": 12345,
  "category": "Walls",
  "family": null,
  "type": "Generic - 6\"",
  "name": "Generic - 6\"",
  "location": {"type": "curve", "start": {"x": 0.0, "y": 0.0, "z": 0.0}, "end": {"x": 16.0, "y": 0.0, "z": 0.0}},
  "parameters": {
    "Base Constraint": "Level 1",
    "Top Constraint": "Up to level: Level 2",
    "Unconnected Height": 8.0,
    "Width": 0.5
  }
}
```

Error (404 equivalent):
```json
{
  "success": false,
  "error": "Element 99999 not found"
}
```

#### 6.2.5 Set Element Parameters

```
PUT /camper-mcp/elements/id/<int:element_id>/parameters
```

Requires Revit API context (`doc`). Sets one or more parameters on an element. Automatically wraps in a transaction.

Request body:
```json
{
  "parameters": {
    "Mark": "Wall-A1",
    "Comments": "Exterior shell, driver side"
  }
}
```

Response (200):
```json
{
  "success": true,
  "element_id": 12345,
  "updated": ["Mark", "Comments"],
  "failed": [],
  "transaction_status": "committed"
}
```

#### 6.2.6 List Phases (Panels)

```
GET /camper-mcp/panels
```

Requires Revit API context (`doc`). Lists all phases in the project. In this workflow, each phase represents a distinct panel.

Response (200):
```json
{
  "phases": [
    {"name": "Driver Side Front", "element_id": 2001, "part_count": 14},
    {"name": "Driver Side Rear", "element_id": 2002, "part_count": 12},
    {"name": "Passenger Side Front", "element_id": 2003, "part_count": 14},
    {"name": "Passenger Side Rear", "element_id": 2004, "part_count": 12},
    {"name": "Front Bulkhead", "element_id": 2005, "part_count": 8},
    {"name": "Rear Bulkhead", "element_id": 2006, "part_count": 10},
    {"name": "Roof - Front", "element_id": 2007, "part_count": 16},
    {"name": "Roof - Rear", "element_id": 2008, "part_count": 16},
    {"name": "Floor", "element_id": 2009, "part_count": 22},
    {"name": "Floor - Rear Extension", "element_id": 2010, "part_count": 8},
    {"name": "Bathroom Interior", "element_id": 2011, "part_count": 6},
    {"name": "Galley Cabinet Left", "element_id": 2012, "part_count": 9},
    {"name": "Galley Cabinet Right", "element_id": 2013, "part_count": 9}
  ]
}
```

`part_count` is the number of family instances assigned to that phase.

#### 6.2.7 Get Panel Detail

```
GET /camper-mcp/panels/<int:phase_id>
```

Requires Revit API context (`doc`). Returns all parts on a panel (all family instances in the given phase), the reference plane they're placed on, and their parameters.

Response (200):
```json
{
  "phase_id": 2001,
  "phase_name": "Driver Side Front",
  "reference_plane": {"name": "Driver Side Front Plane", "element_id": 3001},
  "part_count": 14,
  "parts": [
    {
      "element_id": 10001,
      "family": "Rail_1.5",
      "type": "Standard",
      "parameters": {
        "Width": 1.5,
        "Length": 96.0,
        "Offset": 0.0
      },
      "location": {"x": 0.0, "y": 0.0, "z": 0.0},
      "rotation_degrees": 0.0
    },
    {
      "element_id": 10002,
      "family": "Rail_1.5",
      "type": "Standard",
      "parameters": {
        "Width": 1.5,
        "Length": 96.0,
        "Offset": 0.0
      },
      "location": {"x": 0.0, "y": 0.0, "z": 24.0},
      "rotation_degrees": 0.0
    },
    {
      "element_id": 10003,
      "family": "Stud_1.5",
      "type": "Standard",
      "parameters": {
        "Width": 1.5,
        "Length": 22.5,
        "Offset": 0.0
      },
      "location": {"x": 0.0, "y": 0.0, "z": 0.75},
      "rotation_degrees": 90.0
    }
  ],
  "parts_by_family": {
    "Rail_1.5": 2,
    "Stud_1.5": 8,
    "Sheathing_1in": 2,
    "Insulation_Panel": 2
  }
}
```

All dimension parameters (Width, Length, Offset) are in **inches** in the response, since that's how the part families store them. The `location` coordinates remain in feet (Revit internal).

#### 6.2.8 Place Part on Panel

```
POST /camper-mcp/panels/<int:phase_id>/parts
```

Requires Revit API context (`doc`). Places a part family instance on a panel (assigns it to the given phase, on the panel's reference plane).

Request body:
```json
{
  "family_name": "Stud_1.5",
  "type_name": "Standard",
  "location": {"x": 2.0, "y": 0.0, "z": 0.75},
  "rotation_degrees": 90.0,
  "parameters": {
    "Length": 22.5,
    "Offset": 0.0
  }
}
```

`location` is in feet (Revit internal coordinates). `parameters` are set after placement — values in the family's native units (typically inches for dimensions).

The handler:
1. Finds the family and type (case-insensitive match).
2. Finds the reference plane associated with the panel's phase.
3. Places the instance on that plane at the given location.
4. Assigns the instance to the specified phase.
5. Sets the provided parameters.

Response (200):
```json
{
  "success": true,
  "element_id": 10015,
  "family": "Stud_1.5",
  "type": "Standard",
  "phase": "Driver Side Front",
  "parameters": {
    "Width": 1.5,
    "Length": 22.5,
    "Offset": 0.0
  },
  "transaction_status": "committed"
}
```

Error — family not found:
```json
{
  "success": false,
  "error": "Family 'Stud_1.5x' not found. Available part families: ['Rail_1.5', 'Rail_1in', 'Stud_1.5', 'Stud_1in', 'Sheathing_1in', 'Sheathing_0.5in', 'Insulation_Panel', 'Blocking_1.5', ...]",
  "suggestion": "Did you mean 'Stud_1.5'?"
}
```

#### 6.2.9 Batch Place Parts

```
POST /camper-mcp/panels/<int:phase_id>/parts/batch
```

Requires Revit API context (`doc`). Places multiple parts on a panel in a single transaction. Critical for performance — placing 14 parts one at a time through 14 HTTP round-trips is slow; batching does it in one.

Request body:
```json
{
  "parts": [
    {
      "family_name": "Rail_1.5",
      "type_name": "Standard",
      "location": {"x": 0.0, "y": 0.0, "z": 0.0},
      "rotation_degrees": 0.0,
      "parameters": {"Length": 96.0}
    },
    {
      "family_name": "Rail_1.5",
      "type_name": "Standard",
      "location": {"x": 0.0, "y": 0.0, "z": 24.0},
      "rotation_degrees": 0.0,
      "parameters": {"Length": 96.0}
    },
    {
      "family_name": "Stud_1.5",
      "type_name": "Standard",
      "location": {"x": 0.0, "y": 0.0, "z": 0.75},
      "rotation_degrees": 90.0,
      "parameters": {"Length": 22.5}
    }
  ]
}
```

Response (200):
```json
{
  "success": true,
  "placed": [
    {"index": 0, "element_id": 10020, "family": "Rail_1.5"},
    {"index": 1, "element_id": 10021, "family": "Rail_1.5"},
    {"index": 2, "element_id": 10022, "family": "Stud_1.5"}
  ],
  "failed": [],
  "transaction_status": "committed"
}
```

If any placement fails, the entire batch is rolled back (atomic). The `failed` array includes the index and error for each failure.

#### 6.2.10 Create Panel (Phase + Reference Plane)

```
POST /camper-mcp/panels
```

Requires Revit API context (`doc`). Creates a new phase and optionally a reference plane for it.

Request body:
```json
{
  "name": "Passenger Side Middle",
  "create_reference_plane": true,
  "plane_origin": {"x": 0.0, "y": -3.5, "z": 0.0},
  "plane_direction": {"x": 0.0, "y": 1.0, "z": 0.0},
  "plane_name": "Passenger Side Middle Plane"
}
```

If `create_reference_plane` is false, only the phase is created. The parts can later be associated with an existing reference plane.

Response (200):
```json
{
  "success": true,
  "phase_id": 2014,
  "phase_name": "Passenger Side Middle",
  "reference_plane_id": 3010,
  "reference_plane_name": "Passenger Side Middle Plane",
  "transaction_status": "committed"
}
```

#### 6.2.11 Generate Cut List

```
POST /camper-mcp/cutlist
```

Requires Revit API context (`doc`). Generates a cut list from parts, grouped by part type/thickness and sorted by length. This is the core production output.

Request body:
```json
{
  "scope": "all",
  "group_by": "family",
  "sort_by": "length_desc"
}
```

Alternative scopes:
```json
{"scope": "panel", "phase_id": 2001}
```
```json
{"scope": "panels", "phase_ids": [2001, 2002, 2003, 2004]}
```

`group_by` options:
- `family` — Groups by family name (e.g., all Rail_1.5 together). Default.
- `thickness` — Groups by the Width parameter value (e.g., all 1" parts, all 1.5" parts).
- `family_and_panel` — Groups by family, then sub-groups by panel.

`sort_by` options:
- `length_desc` — Longest first within each group. Default.
- `length_asc` — Shortest first.
- `panel` — Sorted by panel name, then by length within each panel.

Response (200) — `group_by: "thickness"`, `sort_by: "length_desc"`:
```json
{
  "scope": "all",
  "total_parts": 156,
  "groups": [
    {
      "group_key": "1.5\"",
      "group_label": "1.5 inch parts",
      "part_count": 94,
      "parts": [
        {"family": "Rail_1.5", "length": 96.0, "quantity": 8, "panels": ["Driver Side Front", "Driver Side Rear", "Passenger Side Front", "Passenger Side Rear"]},
        {"family": "Rail_1.5", "length": 72.0, "quantity": 4, "panels": ["Front Bulkhead", "Rear Bulkhead"]},
        {"family": "Stud_1.5", "length": 22.5, "quantity": 32, "panels": ["Driver Side Front", "Driver Side Rear", "Passenger Side Front", "Passenger Side Rear"]},
        {"family": "Stud_1.5", "length": 18.0, "quantity": 16, "panels": ["Front Bulkhead", "Rear Bulkhead"]},
        {"family": "Blocking_1.5", "length": 14.5, "quantity": 12, "panels": ["Driver Side Front", "Passenger Side Front"]},
        {"family": "Blocking_1.5", "length": 10.0, "quantity": 22, "panels": ["Floor", "Floor - Rear Extension"]}
      ]
    },
    {
      "group_key": "1\"",
      "group_label": "1 inch parts",
      "part_count": 42,
      "parts": [
        {"family": "Sheathing_1in", "length": 96.0, "quantity": 20, "panels": ["Driver Side Front", "Driver Side Rear", "Passenger Side Front", "Passenger Side Rear", "Roof - Front", "Roof - Rear"]},
        {"family": "Sheathing_1in", "length": 72.0, "quantity": 8, "panels": ["Front Bulkhead", "Rear Bulkhead"]},
        {"family": "Sheathing_1in", "length": 48.0, "quantity": 14, "panels": ["Floor", "Floor - Rear Extension"]}
      ]
    }
  ]
}
```

Response (200) — `group_by: "family_and_panel"`:
```json
{
  "scope": "panel",
  "phase_id": 2001,
  "phase_name": "Driver Side Front",
  "total_parts": 14,
  "groups": [
    {
      "group_key": "Rail_1.5",
      "part_count": 2,
      "parts": [
        {"length": 96.0, "quantity": 2, "element_ids": [10001, 10002]}
      ]
    },
    {
      "group_key": "Stud_1.5",
      "part_count": 8,
      "parts": [
        {"length": 22.5, "quantity": 8, "element_ids": [10003, 10004, 10005, 10006, 10007, 10008, 10009, 10010]}
      ]
    },
    {
      "group_key": "Sheathing_1in",
      "part_count": 2,
      "parts": [
        {"length": 96.0, "quantity": 2, "element_ids": [10011, 10012]}
      ]
    },
    {
      "group_key": "Insulation_Panel",
      "part_count": 2,
      "parts": [
        {"length": 96.0, "quantity": 2, "element_ids": [10013, 10014]}
      ]
    }
  ]
}
```

#### 6.2.12 List Reference Planes

```
GET /camper-mcp/planes
```

Requires Revit API context (`doc`). Lists all named reference planes.

Response (200):
```json
{
  "planes": [
    {
      "element_id": 3001,
      "name": "Driver Side Front Plane",
      "origin": {"x": 0.0, "y": 3.5, "z": 0.0},
      "direction": {"x": 0.0, "y": -1.0, "z": 0.0}
    },
    {
      "element_id": 3002,
      "name": "Passenger Side Front Plane",
      "origin": {"x": 0.0, "y": -3.5, "z": 0.0},
      "direction": {"x": 0.0, "y": 1.0, "z": 0.0}
    }
  ]
}
```

#### 6.2.13 List Available Types

```
GET /camper-mcp/types/<category>
```

Requires Revit API context (`doc`). Lists all loaded family types for a category.

Response (200):
```json
{
  "category": "Walls",
  "types": [
    {"family": null, "type": "Generic - 6\"", "type_id": 678},
    {"family": null, "type": "Exterior - Brick on CMU", "type_id": 679}
  ]
}
```

For system families (walls, floors, roofs), `family` is null.

#### 6.2.14 List Levels

```
GET /camper-mcp/levels
```

Requires Revit API context (`doc`).

Response (200):
```json
{
  "levels": [
    {"name": "Level 1", "elevation_ft": 0.0, "element_id": 100},
    {"name": "Level 2", "elevation_ft": 10.0, "element_id": 101}
  ]
}
```

#### 6.2.15 Get/Export Schedules

```
GET /camper-mcp/schedules
```

Lists all schedules in the project.

```
POST /camper-mcp/schedules/export/<int:schedule_id>
```

Exports a schedule's data as a structured table.

Response (200):
```json
{
  "schedule_name": "1.5 inch Cut List",
  "headers": ["Family", "Length", "Count", "Panel"],
  "rows": [
    ["Rail_1.5", "96\"", "8", "Driver Side Front"],
    ["Rail_1.5", "72\"", "4", "Front Bulkhead"],
    ["Stud_1.5", "22.5\"", "32", "Driver Side Front"],
    ["Stud_1.5", "18\"", "16", "Front Bulkhead"]
  ]
}
```

#### 6.2.16 Create View

```
POST /camper-mcp/create/view
```

Request body:
```json
{
  "view_type": "floor_plan",
  "level": "Level 1",
  "name": "Camper - Floor Plan",
  "scale": 48,
  "phase_id": 2001
}
```

Supported `view_type` values: `floor_plan`, `ceiling_plan`, `section`, `elevation`, `3d`.

`phase_id` is optional. If provided, the view's phase filter is set to show only elements in that phase — effectively showing a single panel.

For `section` and `elevation`, additional fields `direction` and `target_element_id` define the cut plane.

#### 6.2.17 Delete Elements

```
DELETE /camper-mcp/elements
```

Request body:
```json
{
  "element_ids": [12345, 12346, 12347]
}
```

Response (200):
```json
{
  "success": true,
  "deleted": [12345, 12346, 12347],
  "failed": [],
  "transaction_status": "committed"
}
```

#### 6.2.18 Get Material Quantities

```
GET /camper-mcp/materials/<int:element_id>
```

Returns material takeoff data for a single element.

Response (200):
```json
{
  "element_id": 12345,
  "materials": [
    {"name": "Plywood", "area_sqft": 104.0, "volume_cuft": 4.33},
    {"name": "Insulation - Rigid", "area_sqft": 104.0, "volume_cuft": 8.67}
  ]
}
```

### 6.3 Model Discovery Routes

These routes enable Claude Code to understand an existing model's structure — how it's built, what's connected to what, what parameters and families are in use — without prior knowledge of the project.

#### 6.3.1 Model Summary

```
GET /camper-mcp/discovery/summary
```

Requires Revit API context (`doc`). Returns a high-level overview of the entire model.

Response (200):
```json
{
  "doc_title": "Camper_Model_16ft.rvt",
  "file_path": "C:\\Projects\\Camper_Model_16ft.rvt",
  "categories": [
    {"name": "Generic Models", "count": 156},
    {"name": "Reference Planes", "count": 15}
  ],
  "phases_as_panels": [
    {"name": "Driver Side Front", "element_id": 2001, "part_count": 14},
    {"name": "Driver Side Rear", "element_id": 2002, "part_count": 12},
    {"name": "Passenger Side Front", "element_id": 2003, "part_count": 14},
    {"name": "Passenger Side Rear", "element_id": 2004, "part_count": 12},
    {"name": "Front Bulkhead", "element_id": 2005, "part_count": 8},
    {"name": "Rear Bulkhead", "element_id": 2006, "part_count": 10},
    {"name": "Roof - Front", "element_id": 2007, "part_count": 16},
    {"name": "Roof - Rear", "element_id": 2008, "part_count": 16},
    {"name": "Floor", "element_id": 2009, "part_count": 22},
    {"name": "Floor - Rear Extension", "element_id": 2010, "part_count": 8},
    {"name": "Bathroom Interior", "element_id": 2011, "part_count": 6},
    {"name": "Galley Cabinet Left", "element_id": 2012, "part_count": 9},
    {"name": "Galley Cabinet Right", "element_id": 2013, "part_count": 9}
  ],
  "levels": [
    {"name": "Level 1", "elevation_ft": 0.0}
  ],
  "assemblies": [],
  "groups": [],
  "views_count": 14,
  "sheets_count": 6,
  "schedules_count": 4,
  "warnings_count": 3
}
```

#### 6.3.2 Spatial Query

```
POST /camper-mcp/discovery/spatial
```

Requires Revit API context (`doc`). Returns elements within a bounding box or within a radius of a point.

Request body (bounding box mode):
```json
{
  "mode": "bounding_box",
  "min": {"x": 0.0, "y": 0.0, "z": 0.0},
  "max": {"x": 8.0, "y": 7.0, "z": 8.0},
  "categories": ["walls", "doors", "windows", "furniture"]
}
```

Request body (proximity mode):
```json
{
  "mode": "proximity",
  "center": {"x": 5.0, "y": 3.5, "z": 4.0},
  "radius_ft": 3.0,
  "categories": null
}
```

`categories` is optional — if null/omitted, all categories are searched. If provided, only elements in those categories are returned.

Response (200):
```json
{
  "mode": "bounding_box",
  "count": 7,
  "elements": [
    {
      "element_id": 12345,
      "category": "Walls",
      "family": null,
      "type": "Camper_Ext_2x3",
      "name": "Camper_Ext_2x3",
      "location": {"type": "curve", "start": {"x": 0.0, "y": 0.0, "z": 0.0}, "end": {"x": 0.0, "y": 7.0, "z": 0.0}},
      "bounding_box": {"min": {"x": -0.125, "y": 0.0, "z": 0.0}, "max": {"x": 0.125, "y": 7.0, "z": 6.5}}
    }
  ]
}
```

#### 6.3.3 Element Connections

```
GET /camper-mcp/discovery/connections/<int:element_id>
```

Requires Revit API context (`doc`). Returns all elements that have a spatial or logical relationship with the given element.

Response (200):
```json
{
  "element_id": 12345,
  "category": "Walls",
  "connections": {
    "joined_to": [
      {"element_id": 12346, "category": "Walls", "type": "Camper_Ext_2x3", "join_type": "butt"},
      {"element_id": 12347, "category": "Walls", "type": "Camper_Ext_2x3", "join_type": "miter"}
    ],
    "hosted_elements": [
      {"element_id": 12400, "category": "Windows", "family": "Camper_Awning_Window", "type": "24x16"}
    ],
    "host": null,
    "touching": [
      {"element_id": 12500, "category": "Floors", "type": "Camper_Floor_Composite"}
    ],
    "cut_by": [],
    "assembly": {"name": "Wall Assembly - Driver Side", "element_id": 50001}
  }
}
```

Relationship types:
- `joined_to` — Wall joins (Revit's `JoinGeometryUtils`). Reports join type (butt, miter).
- `hosted_elements` — Elements hosted on this element (doors in walls, fixtures on floors/ceilings).
- `host` — The element this element is hosted on (null if not a hosted family).
- `touching` — Elements whose geometry intersects or touches this element's bounding box (excludes joined and hosted, to avoid duplication).
- `cut_by` — Elements that cut this element (openings, voids).
- `assembly` — The assembly this element belongs to, if any.

#### 6.3.4 Model BOM (Bill of Materials)

```
POST /camper-mcp/discovery/bom
```

Requires Revit API context (`doc`). Generates a full bill of materials for the model or a subset.

Request body:
```json
{
  "scope": "all",
  "group_by": "category_and_type",
  "include_materials": true
}
```

Alternative scopes:
```json
{
  "scope": "assembly",
  "assembly_id": 50001,
  "group_by": "category_and_type",
  "include_materials": true
}
```
```json
{
  "scope": "elements",
  "element_ids": [12345, 12346, 12347],
  "group_by": "material",
  "include_materials": true
}
```

`group_by` options:
- `category_and_type` — Groups by Revit category then type name.
- `material` — Groups by material, summing quantities across all elements.
- `family` — Groups by family name.

Response (200) — `group_by: "category_and_type"`:
```json
{
  "scope": "all",
  "group_by": "category_and_type",
  "groups": [
    {
      "category": "Walls",
      "type": "Camper_Ext_2x3",
      "count": 4,
      "total_length_ft": 46.0,
      "total_area_sqft": 299.0,
      "materials": [
        {"name": "Plywood - Exterior 1/4\"", "total_area_sqft": 299.0, "total_volume_cuft": 6.23},
        {"name": "Framing - SPF 2x3", "total_volume_cuft": 12.5},
        {"name": "Insulation - Rigid 1.5\"", "total_area_sqft": 299.0, "total_volume_cuft": 37.38}
      ]
    },
    {
      "category": "Structural Framing",
      "type": "SPF 2x3",
      "count": 47,
      "total_length_ft": 234.5,
      "materials": [
        {"name": "Framing - SPF 2x3", "total_volume_cuft": 48.85}
      ]
    }
  ],
  "material_totals": [
    {"name": "Plywood - Exterior 1/4\"", "total_area_sqft": 598.0, "total_volume_cuft": 12.46},
    {"name": "Framing - SPF 2x3", "total_volume_cuft": 61.35},
    {"name": "Insulation - Rigid 1.5\"", "total_area_sqft": 598.0, "total_volume_cuft": 74.75}
  ]
}
```

Response (200) — `group_by: "material"`:
```json
{
  "scope": "all",
  "group_by": "material",
  "materials": [
    {
      "name": "Plywood - Exterior 1/4\"",
      "total_area_sqft": 598.0,
      "total_volume_cuft": 12.46,
      "used_in": [
        {"category": "Walls", "type": "Camper_Ext_2x3", "count": 4},
        {"category": "Roofs", "type": "Camper_Roof_Composite", "count": 1}
      ]
    }
  ]
}
```

#### 6.3.5 Parameter Schema

```
GET /camper-mcp/discovery/parameters
```

Requires Revit API context (`doc`). Returns all shared parameters and project parameters defined in the model, including which categories they apply to.

Response (200):
```json
{
  "shared_parameters": [
    {
      "name": "Camper_Zone",
      "guid": "a1b2c3d4-...",
      "group": "Camper Data",
      "type": "Text",
      "categories": ["Walls", "Floors", "Roofs", "Furniture"],
      "is_instance": true,
      "sample_values": ["Galley", "Sleeping", "Cab-over", "Wet Bath"]
    },
    {
      "name": "Cut_Length",
      "guid": "e5f6g7h8-...",
      "group": "Fabrication",
      "type": "Length",
      "categories": ["Structural Framing"],
      "is_instance": true,
      "sample_values": ["6'-0\"", "3'-6\"", "7'-0\""]
    }
  ],
  "project_parameters": [
    {
      "name": "Model_Number",
      "type": "Text",
      "categories": ["Project Information"],
      "is_instance": true,
      "value": "TC-16-2026"
    }
  ],
  "builtin_parameter_usage": {
    "Mark": {"categories_using": ["Walls", "Doors", "Windows", "Structural Framing"], "sample_values": ["W-01", "D-01", "WIN-01", "SF-01"]},
    "Comments": {"categories_using": ["Walls", "Furniture"], "sample_values": ["Driver side", "Passenger side"]}
  }
}
```

`sample_values` returns up to 10 distinct values found in the model, to help Claude Code understand what the parameter is used for without needing to query every element.

#### 6.3.6 Family Info

```
GET /camper-mcp/discovery/families
```

Requires Revit API context (`doc`). Returns all loaded families with their types, parameters, and hosting behavior.

Optional query parameter (via request data):
- `category` (string) — Filter to a specific category.

Response (200):
```json
{
  "families": [
    {
      "name": "Camper_Awning_Window",
      "category": "Windows",
      "is_system_family": false,
      "hosting_behavior": "wall",
      "types": [
        {
          "name": "24x16",
          "type_id": 700,
          "type_parameters": {
            "Width": 2.0,
            "Height": 1.333,
            "Default Sill Height": 3.0
          }
        },
        {
          "name": "36x24",
          "type_id": 701,
          "type_parameters": {
            "Width": 3.0,
            "Height": 2.0,
            "Default Sill Height": 3.0
          }
        }
      ],
      "instance_parameters": ["Sill Height", "Head Height", "Comments", "Mark"],
      "placement_count": 4
    },
    {
      "name": "Walls",
      "category": "Walls",
      "is_system_family": true,
      "hosting_behavior": null,
      "types": [
        {
          "name": "Camper_Ext_2x3",
          "type_id": 678,
          "type_parameters": {
            "Width": 0.25,
            "Function": "Exterior"
          }
        }
      ],
      "instance_parameters": ["Base Constraint", "Top Constraint", "Unconnected Height", "Mark", "Comments", "Camper_Zone"],
      "placement_count": 4
    }
  ]
}
```

`hosting_behavior` values: `"wall"`, `"floor"`, `"ceiling"`, `"face"`, `"standalone"`, `null` (system families).

`instance_parameters` lists the parameter names available on placed instances (not their values — use `revit_get_element` for that).

`placement_count` is how many instances of this family exist in the model.

#### 6.3.7 Assembly Detail

```
GET /camper-mcp/discovery/assembly/<int:assembly_id>
```

Requires Revit API context (`doc`). Returns the full contents of an assembly.

Response (200):
```json
{
  "assembly_id": 50001,
  "name": "Wall Assembly - Driver Side",
  "naming_category": "Walls",
  "members": [
    {
      "element_id": 12345,
      "category": "Walls",
      "type": "Camper_Ext_2x3",
      "role": "primary",
      "location": {"type": "curve", "start": {"x": 0.0, "y": 0.0, "z": 0.0}, "end": {"x": 16.0, "y": 0.0, "z": 0.0}},
      "parameters": {"Mark": "W-01", "Camper_Zone": "Exterior Shell"}
    },
    {
      "element_id": 12500,
      "category": "Structural Framing",
      "type": "SPF 2x3",
      "role": "member",
      "location": {"type": "curve", "start": {"x": 0.0, "y": 0.0, "z": 0.0}, "end": {"x": 0.0, "y": 0.0, "z": 6.5}},
      "parameters": {"Mark": "SF-01", "Cut_Length": "6'-6\""}
    }
  ],
  "assembly_views": [
    {"view_id": 80001, "name": "Wall Assembly - Driver Side - Detail", "view_type": "Section"}
  ],
  "assembly_sheets": [
    {"sheet_id": 90001, "sheet_number": "A-101", "sheet_name": "Driver Side Wall Assembly"}
  ]
}
```

#### 6.3.8 View Contents

```
GET /camper-mcp/discovery/view/<int:view_id>
```

Requires Revit API context (`doc`). Returns metadata about a view and the elements visible in it.

Response (200):
```json
{
  "view_id": 80001,
  "name": "Camper - Floor Plan",
  "view_type": "FloorPlan",
  "level": "Floor",
  "scale": 48,
  "crop_box": {"min": {"x": -2.0, "y": -2.0, "z": 0.0}, "max": {"x": 18.0, "y": 9.0, "z": 10.0}},
  "visible_categories": ["Walls", "Doors", "Windows", "Floors", "Furniture", "Plumbing Fixtures"],
  "visible_element_count": 28,
  "visible_elements_by_category": {
    "Walls": [12345, 12346, 12347, 12348],
    "Doors": [12400],
    "Windows": [12401, 12402, 12403, 12404],
    "Floors": [12500, 12501],
    "Furniture": [12600, 12601, 12602, 12603, 12604, 12605],
    "Plumbing Fixtures": [12700, 12701, 12702]
  },
  "annotations": {
    "dimensions": 14,
    "text_notes": 3,
    "tags": 22
  },
  "on_sheets": [
    {"sheet_id": 90001, "sheet_number": "A-100", "sheet_name": "Floor Plan"}
  ]
}
```

#### 6.3.9 Sheet Index

```
GET /camper-mcp/discovery/sheets
```

Requires Revit API context (`doc`). Returns all sheets and the views placed on them.

Response (200):
```json
{
  "sheets": [
    {
      "sheet_id": 90001,
      "sheet_number": "A-100",
      "sheet_name": "Floor Plan",
      "title_block": "Custom Camper Titleblock",
      "views_on_sheet": [
        {"view_id": 80001, "name": "Camper - Floor Plan", "view_type": "FloorPlan"},
        {"view_id": 80010, "name": "Wall Schedule", "view_type": "Schedule"}
      ],
      "revision": "A"
    },
    {
      "sheet_id": 90002,
      "sheet_number": "A-101",
      "sheet_name": "Driver Side Wall Assembly",
      "title_block": "Custom Camper Titleblock",
      "views_on_sheet": [
        {"view_id": 80002, "name": "Wall Assembly - Driver Side - Detail", "view_type": "Section"},
        {"view_id": 80011, "name": "Driver Side Cut List", "view_type": "Schedule"}
      ],
      "revision": "A"
    }
  ]
}
```

#### 6.3.10 Warnings

```
GET /camper-mcp/discovery/warnings
```

Requires Revit API context (`doc`). Returns all active model warnings (geometry conflicts, constraint issues, etc.).

Response (200):
```json
{
  "count": 3,
  "warnings": [
    {
      "description": "Walls are slightly off axis and may cause inaccuracies.",
      "severity": "Warning",
      "element_ids": [12345],
      "additional_element_ids": []
    },
    {
      "description": "Room is not in a properly enclosed region.",
      "severity": "Error",
      "element_ids": [13000],
      "additional_element_ids": [12346, 12347]
    }
  ]
}
```

### 6.4 Error Handling

All routes return HTTP 200 for application-level responses (including errors). The `success` field distinguishes success from failure. This design avoids conflating HTTP transport errors with Revit operation errors.

HTTP-level errors (returned by the pyRevit Routes framework itself):

| Status | Cause | Recovery |
|--------|-------|----------|
| 404 | API name or route path not found | Check extension is loaded, URL is correct |
| 500 | Unhandled exception in Routes framework | Check pyRevit logs, restart Revit |
| `ExternalEventRequest.Denied` | Revit is in a state that cannot accept external events (e.g., mid-transaction, modal dialog open) | Retry after a short delay, or ask user to close any open dialogs |
| `ExternalEventRequest.TimedOut` | Revit did not pick up the event in time | Retry; may indicate Revit is hung |

Application-level errors (returned in JSON body):

| Error Class | `success` | `error` contains | Recovery |
|-------------|-----------|-------------------|----------|
| Code syntax error | false | SyntaxError message | Fix the code |
| Runtime exception | false | Exception message + traceback | Fix the code or check assumptions |
| Element not found | false | "Element {id} not found" | Verify element ID exists |
| Type not found | false | "Type '{name}' not found. Available: [...]" | Use a listed type name |
| Transaction failure | false | Revit transaction error | Check for constraint violations, open transactions |
| Timeout | false | "Execution timed out after {n} seconds" | Simplify the operation or increase timeout |

### 6.5 Thread Safety and Execution Model

This is critical to understand:

1. pyRevit Routes runs an HTTP server on a **background thread** (Python `threading`).
2. The Revit API is **not thread-safe** — it can only be called from Revit's main UI thread.
3. pyRevit bridges this gap using `UI.ExternalEvent`. When a route handler declares `uiapp`, `uidoc`, or `doc` in its function signature, pyRevit:
   - Queues the handler via `ExternalEvent.Raise()`.
   - Blocks the HTTP thread until Revit's main thread picks it up and executes it.
   - Returns the result to the HTTP thread, which sends the HTTP response.
4. This means: **only one Revit API operation can execute at a time**. Concurrent HTTP requests that need Revit API context are serialized by the ExternalEvent mechanism.
5. Routes that do NOT declare `uiapp`/`uidoc`/`doc` run directly on the HTTP thread without waiting for the main thread. Use this for health checks, cached data, or non-Revit operations.

### 6.6 Transaction Management

Model-modifying operations must be wrapped in a Revit `Transaction`. The extension handles this automatically:

- **Convenience routes** (create wall, set parameters, delete elements): Transaction is opened and committed (or rolled back on error) internally by the handler.
- **Execute code route**: If `transaction_name` is provided in the payload, the handler opens a transaction with that name before executing the code, and commits after. If the code raises an exception, the transaction is rolled back. If `transaction_name` is null, no transaction is created — the code is expected to manage its own transactions if needed, or be read-only.

Transaction names appear in Revit's undo history, so the user can undo any MCP operation from Revit's UI.

### 6.7 Serialization

Revit API objects cannot be directly serialized to JSON. The extension includes a serialization layer (`serializers.py`) that converts common Revit types:

| Revit Type | JSON Representation |
|------------|-------------------|
| `ElementId` | integer |
| `XYZ` | `{"x": float, "y": float, "z": float}` |
| `Line` | `{"start": XYZ, "end": XYZ}` |
| `Parameter` | value as string, int, float, or ElementId depending on `StorageType` |
| `Element` | `ElementSummary` dict (see Section 4.1.3) |
| `BoundingBoxXYZ` | `{"min": XYZ, "max": XYZ}` |
| `Transform` | `{"origin": XYZ, "basis_x": XYZ, "basis_y": XYZ, "basis_z": XYZ}` |

For the `execute` endpoint, the code's return value is serialized by attempting `json.dumps()`. If that fails, the serializer attempts to convert known Revit types. If that also fails, `str()` is used as a final fallback, with a warning in the response.

## 7. MCP Server Specification (`revit-mcp-server`)

### 7.1 Transport

- **Protocol**: MCP over stdio (stdin/stdout).
- **Encoding**: JSON-RPC 2.0 messages, newline-delimited.
- **Claude Code configuration**: Added to `.mcp.json` or Claude Code settings.

### 7.2 Server Registration

In the project's `.mcp.json`:

```json
{
  "mcpServers": {
    "revit": {
      "command": "python",
      "args": ["path/to/revit-mcp-server/server.py"],
      "env": {
        "REVIT_ROUTES_HOST": "127.0.0.1",
        "REVIT_ROUTES_PORT": "48884"
      }
    }
  }
}
```

Or in Claude Code global settings at `~/.claude/settings.json`.

### 7.3 Configuration

| Environment Variable | Type | Default | Description |
|---------------------|------|---------|-------------|
| `REVIT_ROUTES_HOST` | string | `127.0.0.1` | pyRevit Routes server host |
| `REVIT_ROUTES_PORT` | integer | `48884` | pyRevit Routes server port |
| `REVIT_REQUEST_TIMEOUT` | integer | `60` | HTTP request timeout in seconds |
| `REVIT_CODE_TIMEOUT` | integer | `30` | Default timeout for code execution |

### 7.4 Tool Definitions

#### 7.4.1 `revit_health`

Check connection to Revit and get session info.

Parameters: none.

Returns: Health status, Revit version, open document name.

#### 7.4.2 `revit_execute`

Execute arbitrary Python code inside Revit's process.

Parameters:
- `code` (string, required) — Python code to execute.
- `timeout` (integer, optional) — Execution timeout in seconds. Default: 30.
- `transaction_name` (string, optional) — If provided, wraps execution in a named Revit transaction.

Returns: `RevitCodeResult`.

This is the escape hatch that gives full API access. The MCP server sends the code to `POST /camper-mcp/execute`.

#### 7.4.3 `revit_list_elements`

List elements by category.

Parameters:
- `category` (string, required) — Category name (e.g., `"walls"`, `"doors"`).
- `include_parameters` (list of string, optional) — Parameter names to include.

Returns: List of `ElementSummary`.

#### 7.4.4 `revit_get_element`

Get detailed info for a single element.

Parameters:
- `element_id` (integer, required) — Revit ElementId.

Returns: Full `ElementSummary` with all parameters.

#### 7.4.5 `revit_set_parameters`

Set parameters on an element.

Parameters:
- `element_id` (integer, required) — Target element.
- `parameters` (dict, required) — Map of parameter name → value.

Returns: Success status, list of updated/failed parameters.

#### 7.4.6 `revit_list_panels`

List all panels (phases) in the project with part counts.

Parameters: none.

Returns: List of phases with names, IDs, and part counts.

#### 7.4.7 `revit_get_panel`

Get all parts on a panel with their families, parameters, and positions.

Parameters:
- `phase_id` (integer, required) — Phase ElementId representing the panel.

Returns: Panel name, reference plane info, full part list with parameters and locations, parts-by-family summary.

#### 7.4.8 `revit_place_part`

Place a single part family instance on a panel.

Parameters:
- `phase_id` (integer, required) — Target panel (phase).
- `family_name` (string, required) — Part family name.
- `type_name` (string, required) — Type name.
- `x`, `y`, `z` (float, required) — Location in feet.
- `rotation_degrees` (float, optional) — Z-axis rotation. Default: 0.
- `parameters` (dict, optional) — Parameter values to set after placement (e.g., `{"Length": 22.5, "Offset": 0.0}`).

Returns: Created element ID, family, type, phase, final parameter values.

#### 7.4.9 `revit_batch_place_parts`

Place multiple parts on a panel in a single atomic transaction.

Parameters:
- `phase_id` (integer, required) — Target panel (phase).
- `parts` (list, required) — Array of part definitions, each with `family_name`, `type_name`, `x`, `y`, `z`, optional `rotation_degrees`, optional `parameters`.

Returns: List of placed parts with element IDs, or full rollback with per-part errors.

#### 7.4.10 `revit_create_panel`

Create a new panel (phase + optional reference plane).

Parameters:
- `name` (string, required) — Panel/phase name.
- `create_reference_plane` (boolean, optional) — Default: true.
- `plane_origin_x`, `plane_origin_y`, `plane_origin_z` (float, required if creating plane) — Plane origin in feet.
- `plane_direction_x`, `plane_direction_y`, `plane_direction_z` (float, required if creating plane) — Plane normal direction.
- `plane_name` (string, optional) — Reference plane name. Defaults to panel name + " Plane".

Returns: Phase ID, reference plane ID.

#### 7.4.11 `revit_generate_cutlist`

Generate a cut list from parts, grouped by type/thickness and sorted by length.

Parameters:
- `scope` (string, required) — `"all"`, `"panel"`, or `"panels"`.
- `phase_id` (integer, required if scope is `"panel"`) — Single panel.
- `phase_ids` (list of integer, required if scope is `"panels"`) — Multiple panels.
- `group_by` (string, optional) — `"family"` (default), `"thickness"`, or `"family_and_panel"`.
- `sort_by` (string, optional) — `"length_desc"` (default), `"length_asc"`, or `"panel"`.

Returns: Grouped and sorted parts with quantities, lengths, and panel associations.

#### 7.4.12 `revit_list_planes`

List all named reference planes.

Parameters: none.

Returns: List of planes with names, origins, and directions.

#### 7.4.13 `revit_list_types`

List available family types for a category.

Parameters:
- `category` (string, required) — Category name.

Returns: List of family/type names and IDs.

#### 7.4.14 `revit_list_levels`

List all levels in the project.

Parameters: none.

Returns: List of level names, elevations, and IDs.

#### 7.4.15 `revit_get_schedules`

List all schedules in the project.

Parameters: none.

Returns: List of schedule names and IDs.

#### 7.4.16 `revit_export_schedule`

Export a schedule as structured data.

Parameters:
- `schedule_id` (integer, required) — Schedule ElementId.

Returns: Headers and rows as arrays.

#### 7.4.17 `revit_create_view`

Create a new view.

Parameters:
- `view_type` (string, required) — One of: `floor_plan`, `ceiling_plan`, `section`, `elevation`, `3d`.
- `level` (string, required for plan views) — Level name.
- `name` (string, required) — View name.
- `scale` (integer, optional) — View scale denominator. Default: 48.
- `phase_id` (integer, optional) — If provided, view's phase filter shows only this panel's elements.

Returns: Created view ID and name.

#### 7.4.18 `revit_delete_elements`

Delete one or more elements.

Parameters:
- `element_ids` (list of integer, required) — Elements to delete.

Returns: Lists of deleted and failed IDs.

#### 7.4.19 `revit_get_materials`

Get material quantities for an element.

Parameters:
- `element_id` (integer, required) — Target element.

Returns: List of materials with areas and volumes.

#### 7.4.20 `revit_convert_units`

Convert between unit systems (convenience, no Revit API call needed).

Parameters:
- `value` (float, required) — Numeric value to convert.
- `from_unit` (string, required) — One of: `feet`, `inches`, `mm`, `meters`.
- `to_unit` (string, required) — One of: `feet`, `inches`, `mm`, `meters`.

Returns: Converted value.

#### 7.4.21 `revit_model_summary`

Get a high-level overview of the entire model — categories, element counts, levels, assemblies, groups, views, sheets.

Parameters: none.

Returns: Category breakdown with counts, levels, assemblies, groups, view/sheet/schedule counts, warning count.

This is the recommended first tool to call when exploring an unfamiliar model.

#### 7.4.22 `revit_spatial_query`

Find elements within a bounding box or radius of a point.

Parameters:
- `mode` (string, required) — `"bounding_box"` or `"proximity"`.
- `min_x`, `min_y`, `min_z` (float, required for bounding_box) — Minimum corner in feet.
- `max_x`, `max_y`, `max_z` (float, required for bounding_box) — Maximum corner in feet.
- `center_x`, `center_y`, `center_z` (float, required for proximity) — Center point in feet.
- `radius_ft` (float, required for proximity) — Search radius in feet.
- `categories` (list of string, optional) — Filter to specific categories. Default: all.

Returns: List of elements with locations and bounding boxes.

#### 7.4.23 `revit_element_connections`

Get all elements connected to, hosted on, hosting, or touching a given element.

Parameters:
- `element_id` (integer, required) — The element to query relationships for.

Returns: Categorized connection lists — joined_to, hosted_elements, host, touching, cut_by, assembly membership.

#### 7.4.24 `revit_model_bom`

Generate a bill of materials for the full model, an assembly, or a set of elements.

Parameters:
- `scope` (string, required) — `"all"`, `"assembly"`, or `"elements"`.
- `assembly_id` (integer, required if scope is `"assembly"`) — Assembly ElementId.
- `element_ids` (list of integer, required if scope is `"elements"`) — Element IDs to include.
- `group_by` (string, optional) — `"category_and_type"` (default), `"material"`, or `"family"`.
- `include_materials` (boolean, optional) — Include material breakdown per group. Default: true.

Returns: Grouped element/material quantities with counts, lengths, areas, volumes.

#### 7.4.25 `revit_parameter_schema`

Get all shared parameters, project parameters, and commonly used built-in parameters with sample values.

Parameters: none.

Returns: Lists of shared parameters (with GUIDs, groups, types, applicable categories, sample values), project parameters, and built-in parameter usage.

#### 7.4.26 `revit_family_info`

Get detailed information about all loaded families — types, parameters, hosting behavior, placement counts.

Parameters:
- `category` (string, optional) — Filter to a specific category. Default: all categories.

Returns: List of families with their types, type parameters, instance parameters, hosting behavior, and how many are placed.

#### 7.4.27 `revit_assembly_detail`

Get the full contents of an assembly — all members with their properties, associated views, and sheets.

Parameters:
- `assembly_id` (integer, required) — Assembly ElementId.

Returns: Assembly metadata, member list with categories/types/locations/parameters, associated views and sheets.

#### 7.4.28 `revit_view_contents`

Get metadata about a view and the elements visible in it.

Parameters:
- `view_id` (integer, required) — View ElementId.

Returns: View properties (type, level, scale, crop), visible elements grouped by category, annotation counts, sheet placements.

#### 7.4.29 `revit_sheet_index`

Get all sheets and the views placed on each.

Parameters: none.

Returns: List of sheets with numbers, names, title blocks, views, and revisions.

#### 7.4.30 `revit_warnings`

Get all active model warnings.

Parameters: none.

Returns: List of warnings with descriptions, severities, and affected element IDs.

### 7.5 Error Propagation

The MCP server translates errors from the pyRevit Routes layer:

| HTTP/Routes Error | MCP Tool Response |
|-------------------|-------------------|
| Connection refused | `isError: true`, message: "Cannot connect to Revit. Is Revit running with pyRevit Routes enabled on port {port}?" |
| HTTP timeout | `isError: true`, message: "Revit did not respond within {timeout}s. It may be busy or hung." |
| Application error (`success: false`) | `isError: true`, message: error + traceback from Revit |
| Application success (`success: true`) | `isError: false`, structured result |

### 7.6 Connection Lifecycle

1. MCP server starts when Claude Code launches it (on first tool call or session start).
2. Server reads `REVIT_ROUTES_HOST` and `REVIT_ROUTES_PORT` from environment.
3. On first tool call, server sends `GET /camper-mcp/health` to verify connectivity.
4. If health check fails, the tool returns an error telling the user to start Revit / enable Routes.
5. Server stays running for the duration of the Claude Code session.
6. Server has no persistent state — every tool call is an independent HTTP request.

## 8. Observability

### 8.1 Logging — pyRevit Extension

pyRevit has a built-in logger (`pyrevit.coreutils.logger.get_logger`). The extension logs:

- `INFO`: Route registered, request received (method, path), response sent (status, execution time).
- `WARNING`: Parameter not found on element, type name fuzzy-matched.
- `ERROR`: Unhandled exception in handler, transaction rollback, timeout.

Logs appear in pyRevit's output window inside Revit. No external log sink is required.

Format: `[camper-mcp] {level}: {message} | {context}`

### 8.2 Logging — MCP Server

The MCP server logs to stderr (which Claude Code captures):

- `INFO`: Server started, tool called (name, parameters summary), response returned (success/error, elapsed time).
- `ERROR`: Connection failed, HTTP error, unexpected response format.

Format: `[revit-mcp] {timestamp} {level}: {message}`

### 8.3 Metrics

The MCP server tracks per-session (in-memory, not persisted):

- Total tool calls by tool name.
- Success/error count by tool name.
- Average response time by tool name.
- Total code executions and transaction commits/rollbacks.

These are available via a `revit_session_stats` tool (not listed above — informational, not operational).

## 9. Failure Model

### 9.1 Failure Classes

1. **Connection failure** — Revit is not running, Routes not enabled, wrong port.
2. **Revit busy** — ExternalEvent denied or timed out because Revit is in a modal state (dialog box open, mid-edit, rendering).
3. **Code execution failure** — Syntax error, runtime exception in user-provided code.
4. **Transaction failure** — Revit rejects a model change (constraint violation, read-only document, element locked by worksharing).
5. **Timeout** — Code takes longer than the configured timeout.
6. **Serialization failure** — A Revit object cannot be converted to JSON.

### 9.2 Recovery Behavior

| Failure Class | System Behavior | User Impact |
|---------------|----------------|-------------|
| Connection failure | MCP tool returns error with diagnostic message | Claude Code reports the issue and suggests remediation |
| Revit busy | MCP tool returns error suggesting user close dialogs | Non-destructive; user closes dialog and retries |
| Code execution failure | Transaction rolled back (if any), error + traceback returned | Claude Code sees the error and can fix the code |
| Transaction failure | Transaction rolled back, error returned | No model changes made; Claude Code adjusts approach |
| Timeout | Operation aborted, transaction rolled back if open | No partial model changes; operation is atomic |
| Serialization failure | Fallback to `str()` representation, warning included | Claude Code gets degraded but usable output |

### 9.3 Restart Recovery

- **Revit restarts**: MCP server's next HTTP request will fail with connection refused. It reports the error. User restarts Revit, pyRevit reloads, Routes re-activates. No data is lost (Revit saves are independent).
- **MCP server restarts**: Claude Code relaunches it on next tool call. No state to recover — it's stateless.
- **pyRevit reloads** (via ribbon button): Routes server restarts on the same or next available port. If port changes, user must update the MCP server config or restart it with the new port. The extension's `startup.py` re-registers all routes.

### 9.4 Operator Intervention Points

- **Port conflict**: Change `REVIT_ROUTES_PORT` env var and pyRevit config to match.
- **Extension not loading**: Check pyRevit output window for `startup.py` errors. Verify extension is in the correct directory.
- **Revit hung**: The user can force-close Revit. No MCP server cleanup needed.
- **Disable MCP access**: Remove the `camper-mcp.extension` folder from pyRevit extensions. Restart Revit.

## 10. Security

### 10.1 Trust Boundary

- The pyRevit Routes server binds to `127.0.0.1` (localhost only). No remote access.
- The `execute` endpoint runs **arbitrary code** inside Revit's process. This is intentional — it's the power tool. Any code Claude generates runs with the full privileges of the Revit process (which is the user's session).
- There is no authentication on the HTTP endpoints. Any process on the local machine can call them. This is acceptable because:
  - The server only binds to localhost.
  - The user's machine is the trust boundary.
  - Revit itself has no authentication model.

### 10.2 Filesystem Safety

- The `execute` endpoint can access the filesystem (Python has full OS access in Revit's process). This is no different from running any pyRevit script.
- The extension itself does not write to the filesystem except through Revit's save mechanisms.
- The MCP server (separate process) only reads environment variables and makes HTTP requests to localhost.

### 10.3 Input Validation

- The extension validates that `transaction_name`, if provided, is a non-empty string.
- Element IDs are validated as positive integers before calling `doc.GetElement()`.
- Category names are validated against `DB.BuiltInCategory` enum values.
- Coordinate values are validated as finite floats (no NaN, no Infinity).
- Code payloads are not sanitized (by design — full API access is the goal).

### 10.4 Denial of Service

- A malicious or buggy code payload could hang Revit (infinite loop). The timeout mechanism mitigates this by aborting execution after the configured timeout. However, some Revit API operations are not interruptible — a truly pathological call could still hang.
- Rate limiting is not implemented. The serialized ExternalEvent execution provides natural throttling.

## 11. Reference Algorithms

### 11.1 Execute Code Handler (pyRevit Extension)

```
function handle_execute(uiapp, request):
    code = request.data["code"]
    timeout = request.data.get("timeout", 30)
    txn_name = request.data.get("transaction_name", null)

    doc = uiapp.ActiveUIDocument.Document
    start_time = now()

    # Inject context variables into execution namespace
    namespace = {
        "uiapp": uiapp,
        "uidoc": uiapp.ActiveUIDocument,
        "doc": doc,
        "DB": Autodesk.Revit.DB,
        "UI": Autodesk.Revit.UI,
        "__result__": None
    }

    # Wrap code in a function to support return statements
    wrapped = "def _execute():\n" + indent(code, "    ") + "\n__result__ = _execute()"

    txn = null
    txn_status = "no_transaction"

    try:
        if txn_name is not null:
            txn = Transaction(doc, txn_name)
            txn.Start()
            txn_status = "started"

        exec(wrapped, namespace)

        if txn is not null:
            txn.Commit()
            txn_status = "committed"

        result = serialize(namespace["__result__"])
        elapsed = now() - start_time

        return {
            "success": true,
            "result": result,
            "error": null,
            "traceback": null,
            "execution_time_ms": elapsed,
            "transaction_status": txn_status
        }

    except Exception as e:
        if txn is not null and txn.HasStarted() and not txn.HasEnded():
            txn.RollBack()
            txn_status = "rolled_back"

        elapsed = now() - start_time
        return {
            "success": false,
            "result": null,
            "error": str(e),
            "traceback": format_traceback(e),
            "execution_time_ms": elapsed,
            "transaction_status": txn_status
        }
```

### 11.2 MCP Tool Call Handler (MCP Server)

```
function handle_tool_call(tool_name, arguments):
    # Map tool name to HTTP method + path + body
    method, path, body = build_request(tool_name, arguments)

    # Send HTTP request to pyRevit Routes
    try:
        response = http_request(
            method=method,
            url=f"http://{host}:{port}/camper-mcp{path}",
            json=body,
            timeout=request_timeout
        )
    except ConnectionError:
        return mcp_error("Cannot connect to Revit on port {port}. Is Revit running with pyRevit Routes enabled?")
    except Timeout:
        return mcp_error("Revit did not respond within {timeout}s.")

    # Parse response
    data = response.json()

    if data.get("success") == false:
        return mcp_error(data["error"] + "\n" + (data.get("traceback") or ""))

    return mcp_result(data)
```

### 11.3 Serialization (pyRevit Extension)

```
function serialize(obj):
    if obj is None:
        return null
    if obj is string or number or boolean:
        return obj
    if obj is list or tuple:
        return [serialize(item) for item in obj]
    if obj is dict:
        return {str(k): serialize(v) for k, v in obj.items()}
    if obj is ElementId:
        return obj.IntegerValue
    if obj is XYZ:
        return {"x": round(obj.X, 6), "y": round(obj.Y, 6), "z": round(obj.Z, 6)}
    if obj is Element:
        return serialize_element_summary(obj)
    if obj is BoundingBoxXYZ:
        return {"min": serialize(obj.Min), "max": serialize(obj.Max)}

    # Fallback
    try:
        return json.dumps(obj)
    except:
        return str(obj)
```

## 12. Test and Validation Matrix

### 12.1 Core Conformance (No Revit Required)

These tests validate the MCP server in isolation using a mock HTTP backend:

- MCP server starts and responds to `initialize` handshake.
- All 30 tools are listed in `tools/list` response with correct schemas.
- `revit_health` sends GET to `/camper-mcp/health`.
- `revit_execute` sends POST to `/camper-mcp/execute` with correct body structure.
- `revit_place_part` maps parameters to correct JSON body for panel part placement.
- `revit_generate_cutlist` maps scope/group_by/sort_by to correct request body.
- `revit_convert_units` works without any HTTP call (pure math).
- Connection refused produces correct MCP error response.
- HTTP timeout produces correct MCP error response.
- Application-level error (`success: false`) is propagated as MCP error.
- Environment variables are read and applied correctly.

### 12.2 Extension Conformance (Requires Revit + pyRevit)

These tests must be run manually inside Revit (or with a Revit test harness):

- Extension loads without errors on Revit startup (check pyRevit output window).
- `GET /camper-mcp/health` returns correct JSON with Revit version and document title.
- `POST /camper-mcp/execute` with `code: "return doc.Title"` returns the document title.
- `POST /camper-mcp/execute` with invalid code returns `success: false` with traceback.
- `POST /camper-mcp/execute` with `transaction_name` creates an undoable operation.
- `GET /camper-mcp/panels` lists all phases matching Revit's phase list.
- `GET /camper-mcp/panels/{id}` returns parts matching what's in the phase in Revit.
- `POST /camper-mcp/panels/{id}/parts` places a part visible in the model on the correct reference plane and in the correct phase.
- `POST /camper-mcp/panels/{id}/parts/batch` places multiple parts atomically (all succeed or all roll back).
- `PUT /camper-mcp/elements/id/{id}/parameters` sets a parameter verifiable in Revit properties.
- `DELETE /camper-mcp/elements` removes elements that disappear from the model.
- `POST /camper-mcp/cutlist` with `scope: "panel"` returns parts matching the phase contents, grouped and sorted correctly.
- `POST /camper-mcp/cutlist` with `group_by: "thickness"` groups parts by Width parameter value.
- `GET /camper-mcp/levels` lists all levels matching Revit's level list.
- Creating a part with a non-existent family returns a helpful error with available families.
- Two rapid sequential requests are serialized correctly (no race conditions).
- A request during a modal dialog returns ExternalEvent denied error.
- `GET /camper-mcp/discovery/summary` returns phase/panel counts and part counts matching manual inspection in Revit.
- `POST /camper-mcp/discovery/spatial` with bounding box returns only elements inside the box.
- `GET /camper-mcp/discovery/connections/{id}` for a part returns other parts on the same panel.
- `GET /camper-mcp/discovery/parameters` lists shared parameters matching Revit's Shared Parameters dialog.
- `GET /camper-mcp/discovery/families` returns family count matching Revit's Family Manager.
- `GET /camper-mcp/discovery/assembly/{id}` returns members matching the assembly's member list in Revit.
- `GET /camper-mcp/discovery/view/{id}` returns visible elements matching what's visible in the Revit view.
- `GET /camper-mcp/discovery/sheets` returns sheets matching Revit's Sheet Index.
- `GET /camper-mcp/discovery/warnings` returns warnings matching Revit's Warnings dialog.

### 12.3 Integration (Full Stack)

These tests validate the complete Claude Code → MCP → Routes → Revit pipeline:

- Claude Code can call `revit_health` and receive a response.
- Claude Code can list panels via `revit_list_panels` and get details of a specific panel via `revit_get_panel`.
- Claude Code can place a part on a panel via `revit_place_part` and verify it via `revit_get_panel`.
- Claude Code can batch-place a full panel's worth of parts via `revit_batch_place_parts` in a single transaction.
- Claude Code can execute arbitrary code via `revit_execute` and process the result.
- Claude Code can recover from a connection error (Revit closed) and report it clearly.
- Claude Code can generate a cut list via `revit_generate_cutlist` grouped by thickness, sorted by length, matching what a technician would expect.
- Claude Code can call `revit_model_summary` on an existing camper model and correctly identify all panels and their part counts.
- Claude Code can use `revit_get_panel` to enumerate every part on a panel and understand the panel's construction.
- Claude Code can generate a full BOM via `revit_model_bom` and compare it against an existing Revit schedule.
- Claude Code can use `revit_parameter_schema` + `revit_family_info` to understand the project's custom data model without any prior briefing.

## 13. Implementation Checklist

### 13.1 Definition of Done — Core

- [ ] pyRevit installation documented and verified on target machine.
- [ ] Routes server enabled and responding on configured port.
- [ ] `camper-mcp.extension` deployed and loading on Revit startup.
- [ ] Health endpoint returning valid JSON.
- [ ] Execute endpoint running code in Revit context and returning results.
- [ ] Transaction management working (commit on success, rollback on error).
- [ ] Serialization handling all common Revit types.
- [ ] MCP server starting via stdio and listing all tools.
- [ ] MCP server registered in `.mcp.json` and accessible from Claude Code.
- [ ] All panel tools (list panels, get panel, place part, batch place, create panel, cut list, list planes) operational.
- [ ] All discovery tools (summary, spatial, connections, BOM, parameters, families, assemblies, views, sheets, warnings) operational.
- [ ] Error propagation working end-to-end (Revit error → MCP error → Claude Code).

### 13.2 Definition of Done — Operational Validation

- [ ] Restart Revit and verify extension auto-loads and Routes server starts.
- [ ] Place a part on a panel from Claude Code, verify it appears in Revit in the correct phase and on the correct reference plane.
- [ ] Undo the part placement from Revit's UI (verifies transaction naming).
- [ ] Generate a cut list from Claude Code and verify it matches manual schedule output.
- [ ] Run `revit_model_summary` on an existing camper model and verify output matches manual inspection.
- [ ] Close Revit, verify Claude Code gets a clear connection error.
- [ ] Reopen Revit, verify Claude Code reconnects on next tool call.

### 13.3 Recommended Extensions (Future)

- [ ] TODO: Port auto-discovery — MCP server scans ports to find the active Revit instance instead of relying on a configured port.
- [ ] TODO: File-based socket transport as alternative to HTTP (lower latency, no port conflicts).
- [ ] TODO: Streaming results for long-running operations (e.g., exporting large schedules).
- [ ] TODO: Panel duplication — clone an existing panel (all parts with offset transforms) as a starting point for a similar panel.
- [ ] TODO: Panel mirroring — mirror a panel across an axis (e.g., driver side → passenger side).
- [ ] TODO: Template project with pre-loaded part families and standard phases.
- [ ] TODO: Panel diff — compare two panels and report what's different (added/removed/changed parts).
- [ ] TODO: View screenshot capture — return a rendered image of a view for Claude Code to inspect visually.
