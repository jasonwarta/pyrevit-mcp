# pyRevit Clean Slate: Complete Removal and Fresh Install

## Context

This guide removes ALL traces of pyRevit from a machine so that a fresh install starts clean. Use this if pyRevit has been installed/upgraded/reinstalled multiple times and may have corrupted state.

**IMPORTANT:** Close Revit completely before starting. Do not open Revit until instructed.

---

## Phase 1: Inventory (Run First, Report Back)

Run these in PowerShell and report the output before proceeding. This tells us what we're dealing with.

```powershell
# What pyRevit CLI version is installed?
pyrevit --version

# What clones exist?
pyrevit clones

# What Revit versions is pyRevit attached to?
pyrevit attached

# What extension paths are configured?
pyrevit extensions paths

# What's in the pyRevit appdata folder?
Get-ChildItem "$env:APPDATA\pyRevit" -ErrorAction SilentlyContinue

# What addin files exist for all Revit versions?
Get-ChildItem "$env:APPDATA\Autodesk\Revit\Addins" -Recurse -Filter "pyRevit*" -ErrorAction SilentlyContinue
Get-ChildItem "$env:PROGRAMDATA\Autodesk\Revit\Addins" -Recurse -Filter "pyRevit*" -ErrorAction SilentlyContinue

# Check for pyRevit in program files
Get-ChildItem "C:\Program Files\pyRevit*" -ErrorAction SilentlyContinue
Get-ChildItem "$env:LOCALAPPDATA\Programs\pyRevit*" -ErrorAction SilentlyContinue
Get-ChildItem "$env:LOCALAPPDATA\pyRevit*" -ErrorAction SilentlyContinue

# Check registry for pyRevit uninstall entries
Get-ItemProperty "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*" -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "*pyRevit*" } | Select-Object DisplayName, InstallLocation, UninstallString
Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*" -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "*pyRevit*" } | Select-Object DisplayName, InstallLocation, UninstallString

# Check PATH for pyRevit entries
$env:PATH -split ";" | Where-Object { $_ -like "*pyrevit*" -or $_ -like "*pyRevit*" }
```

---

## Phase 2: Detach and Remove via CLI

If the `pyrevit` command is available, use it to clean up properly before brute-forcing.

```powershell
# Detach from all Revit versions (removes .addin files)
pyrevit detach --all

# Delete all clones
pyrevit clones delete --all

# Verify
pyrevit clones
pyrevit attached
```

If any of these commands fail, note the error and continue to Phase 3.

---

## Phase 3: Uninstall pyRevit CLI

Check Add/Remove Programs (Settings > Apps > Installed Apps) for anything named "pyRevit" or "pyRevit CLI". Uninstall it through there.

If it doesn't appear in Add/Remove Programs, use the registry uninstall string from Phase 1, or skip to Phase 4.

---

## Phase 4: Delete All pyRevit Files

This is the brute-force cleanup. Run in PowerShell as Administrator.

```powershell
# pyRevit config and data
Remove-Item -Recurse -Force "$env:APPDATA\pyRevit" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$env:PROGRAMDATA\pyRevit" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\pyRevit" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$env:USERPROFILE\.pyrevit" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$env:USERPROFILE\pyRevit" -ErrorAction SilentlyContinue

# pyRevit CLI install directories
Remove-Item -Recurse -Force "C:\Program Files\pyRevit CLI" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\Programs\pyRevit CLI" -ErrorAction SilentlyContinue

# ALL pyRevit addin manifests for ALL Revit versions
Get-ChildItem "$env:APPDATA\Autodesk\Revit\Addins" -Recurse -Filter "pyRevit*" -ErrorAction SilentlyContinue | Remove-Item -Force
Get-ChildItem "$env:PROGRAMDATA\Autodesk\Revit\Addins" -Recurse -Filter "pyRevit*" -ErrorAction SilentlyContinue | Remove-Item -Force
```

---

## Phase 5: Verify Clean State

```powershell
# CLI should not be found
pyrevit --version

# No addin files should exist
Get-ChildItem "$env:APPDATA\Autodesk\Revit\Addins" -Recurse -Filter "pyRevit*" -ErrorAction SilentlyContinue
Get-ChildItem "$env:PROGRAMDATA\Autodesk\Revit\Addins" -Recurse -Filter "pyRevit*" -ErrorAction SilentlyContinue

# No pyRevit directories should exist
Test-Path "$env:APPDATA\pyRevit"
Test-Path "$env:PROGRAMDATA\pyRevit"
Test-Path "$env:LOCALAPPDATA\pyRevit"

# All should return False or "command not found"
```

---

## Phase 6: Launch Revit to Confirm

Open Revit. Confirm:
- No pyRevit tab in the ribbon
- No error dialogs on startup
- Revit loads normally

Close Revit.

---

## Phase 7: Fresh Install of pyRevit 6.4.0

Only proceed if Phase 6 was clean.

1. Download the installer:
   - Go to https://github.com/pyrevitlabs/pyRevit/releases/tag/v6.4.0.26100%2B0515
   - Download `pyRevit_6.4.0.26100_signed.exe`

2. Run the installer. Accept defaults.

3. Verify the CLI:
```powershell
pyrevit --version
# Should show 6.4.0
```

4. Launch Revit. You should see a dialog asking about a signed addin. Choose **Always Load**.

5. Confirm the pyRevit tab appears in the ribbon.

6. Open pyRevit Settings (gear icon on pyRevit tab). Under Routes:
   - Enable the routes server
   - After enabling, reload pyRevit when prompted
   - If Windows asks about network access, allow it

7. Verify Routes is working:
```powershell
curl http://127.0.0.1:48884/routes/status
```
Should return JSON with your Revit version and session info.

---

## Troubleshooting

**pyRevit tab doesn't appear after install:**
- Run `pyrevit attached` — if empty, run `pyrevit attach master 2026 --installed`
- Restart Revit

**Revit crashes on startup after install:**
- Check the Revit journal log (most recent `.txt` file in `%LOCALAPPDATA%\Autodesk\Revit\Autodesk Revit 2026\Journals\`)
- Copy the last 100 lines and report back

**Routes status returns connection refused:**
- Verify routes are enabled in pyRevit Settings
- Try `curl http://localhost:48884/routes/status` instead
- Check if a different port was assigned: look in pyRevit Settings for the port number

**Multiple Revit versions installed:**
- pyRevit auto-assigns incrementing ports if multiple Revit instances are open
- First instance gets 48884, second gets 48885, etc.
