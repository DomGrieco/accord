# Owner gate: Desktop computer-use capture

Status: blocked on the macOS capture path only. Backend, policy, persistence, and static Desktop contract tests continue to run.

## Observed

- Retried at 2026-08-29 08:20 ADT after adding approval revocation. `list_apps` found the running Hermes process, but exact Hermes capture still returned a zero by zero image with no accessibility elements.
- The earlier 2026-08-29 06:16 ADT retry listed two running Hermes windows and a macOS `Screen Recording` permission window.
- Earlier full-screen capture returned a black image.
- Capturing Google Chrome also returned a zero by zero image, so the failure is not specific to the Human Gate panel.
- `screencapture` could not create an image from the display.
- `hermes computer-use doctor` reported Cua Driver 0.22.1, an active MCP session, Accessibility access, and Screen Recording access. It did not probe direct ScreenCaptureKit readiness.

## Owner action

Run this in Dominic's foreground terminal:

```bash
cua-driver permissions grant
```

Approve or re-approve Screen Recording for CuaDriver if macOS asks. Restart CuaDriver if the command asks for it. Do not restart or relaunch Hermes solely for this gate.

After capture works, open Hermes Desktop, select Human Gate in the sidebar, and test the pending fixture card with computer-use. The fixture text is `Desktop approval panel fixture. No external effect.` It cannot publish or call a live provider.
