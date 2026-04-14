-- FTC Report PDF Saver
-- Handles the Chromium print dialog that appears after clicking "Download Report (PDF)"
-- The print dialog already has "Save as PDF" selected — just need to click Save,
-- then handle the OS file save dialog.
--
-- Usage: osascript ftc_save_report.applescript "Robert FTC 1" "/path/to/FTC Reports"
--
-- Pre-condition: Chromium print dialog is open with "Save as PDF" destination.

on run argv
	set reportName to item 1 of argv
	set saveDir to item 2 of argv

	-- The browser is "Google Chrome for Testing" (Playwright's Chromium)
	set appName to "Google Chrome for Testing"

	tell application appName to activate
	delay 1

	tell application "System Events"
		tell process appName
			set frontmost to true
			delay 0.5

			-- Step 1: The Chromium print dialog is already open with "Save as PDF".
			-- Click the blue "Save" button by pressing Enter (it's the default button).
			keystroke return
			delay 2

			-- Step 2: OS-level file save dialog is now open.
			-- Set the filename (with explicit .pdf extension — Chromium's
			-- "Save as PDF" does NOT auto-append it, file saves without extension).
			keystroke "a" using command down
			delay 0.3
			set the clipboard to reportName & ".pdf"
			delay 0.2
			keystroke "v" using command down
			delay 0.5

			-- Step 3: Navigate to save directory using Cmd+Shift+G (Go to folder)
			keystroke "g" using {command down, shift down}
			delay 2

			-- Paste the save directory path (clipboard avoids / and space issues)
			set the clipboard to saveDir
			delay 0.2
			keystroke "v" using command down
			delay 0.5

			-- Press Enter to go to that folder
			keystroke return
			delay 1.5

			-- Step 4: Press Enter to click Save
			keystroke return
			delay 2

			-- Handle "Replace?" dialog if file already exists
			try
				delay 0.5
				if exists sheet 1 of window 1 then
					keystroke return
				end if
			end try

		end tell
	end tell

	return saveDir & "/" & reportName & ".pdf"
end run
