# Owner gate: Desktop computer-use capture

Status: resolved on 2026-08-30 after a full Hermes Desktop quit and reopen. Automated capture can read and operate the packaged app again.

## Observed

- After reopening Desktop on the patched checkout, computer-use captured the full Hermes window and accessibility tree. Accord stayed in the sidebar, loaded the Life profile approval inbox, and showed `0 pending · 40 of 40 shown`.
- Switching to a Life session and back to Accord kept the sidebar entry and reloaded the same approval inbox.
- The first capture before the Desktop restart returned a 1440 by 18 black strip with no accessibility elements. Restarting the existing Life messaging gateway alone did not fix the packaged Desktop capture or reload its profile serve process.
- Retried at 2026-08-29 11:53 ADT after making unclassified post-claim failures uncertain. Window discovery found two Hermes windows and the macOS Screen Recording prompt. Exact capture of the main Hermes window still returned a zero by zero image with no accessibility elements.
- Retried at 2026-08-29 11:00 ADT after adding one-use in-process effect claims. App discovery found the running Hermes process, but it exposed no windows. Exact Hermes capture still returned a zero by zero image with no accessibility elements. Cua Driver appeared installed but not running.
- Retried at 2026-08-29 10:23 ADT after adding the owned mock publication path. Window discovery found Cua Driver, two Hermes windows, and the macOS Screen Recording prompt. Exact capture of the main Hermes window still returned a zero by zero image with no accessibility elements. The running packaged app exposed no Desktop development CDP endpoint on port 9222, so DOM inspection could not replace computer-use.
- Retried at 2026-08-29 09:43 ADT after adding bounded card audit history. Exact Hermes capture still returned a zero by zero image with no accessibility elements.
- Retried at 2026-08-29 09:29 ADT after adding safe pre-dispatch resume recovery. `list_apps` found the running Hermes process, but exact Hermes capture still returned a zero by zero image with no accessibility elements. Cua Driver appeared installed but not running in the app inventory.
- Retried at 2026-08-29 08:20 ADT after adding approval revocation. `list_apps` found the running Hermes process, but exact Hermes capture still returned a zero by zero image with no accessibility elements.
- The earlier 2026-08-29 06:16 ADT retry listed two running Hermes windows and a macOS `Screen Recording` permission window.
- Earlier full-screen capture returned a black image.
- Capturing Google Chrome also returned a zero by zero image, so the failure is not specific to the Accord panel.
- `screencapture` could not create an image from the display.
- `hermes computer-use doctor` reported Cua Driver 0.22.1, an active MCP session, Accessibility access, and Screen Recording access. It did not probe direct ScreenCaptureKit readiness.

## Remaining owner test

Open Hermes Desktop, select Accord, and open Edit configuration. Confirm the safe Life configuration still shows no generic policies and X publication disabled. A fresh safe fixture can then exercise the approval decision flow without a live provider call.
