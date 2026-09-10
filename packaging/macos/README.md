# macOS app launcher

Build a native app for an existing source checkout:

```bash
./Start-SystemBuilder.command
# After the first-run environment setup, stop the server and build:
.venv/bin/python packaging/build_macos_app.py
```

Requires Xcode Command Line Tools (`swiftc`, `codesign`, `sips`, `iconutil`).
The build defaults to `~/Applications/LTspice System Builder.app`; use
`--output /another/path/LSB.app` to select a different location. Existing output
is never overwritten. The executable targets the Mac architecture doing the build.

Open the app from Finder or drag it into the Dock. On first launch, allow macOS
access to Documents so it can read the source installation and project workspace.
The app starts the local server and opens your default browser without a Terminal
window. Click its Dock icon again to reopen the GUI. Quit the app to stop its
server; finish simulations before quitting.

Workspace: `~/Documents/LTspice/projects`. Existing projects are preserved; missing
RC and three-opamp starters are added by the shared server startup.
Logs: `~/Library/Logs/LTspice System Builder/launcher.log`.

This is a locally signed launcher, not a standalone Python bundle or a notarized
release. It records the checkout location and uses that checkout's `.venv`; moving
or deleting the checkout requires rebuilding the app. Install dependencies with
the command launcher again after dependency updates. Source changes are used on
the next app launch.

The generated `LSB.png` is the source icon; the builder creates all required ICNS
sizes using macOS tools. No generated app binaries are committed.

Local verification (September 10, 2026, Apple Silicon): native build and signature
verification passed. After granting Documents access, the app opened Safari on the
correct workspace with both starters. Normal app termination shut down Uvicorn;
launching the Desktop link started a healthy server again, and reopening the
running app opened its GUI. The Desktop link and one persistent Dock entry were
verified. This does not qualify Intel builds or a standalone distribution.
