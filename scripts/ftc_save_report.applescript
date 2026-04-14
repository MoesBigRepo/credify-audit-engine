-- FTC Report PDF Saver — poll-based, near-instant UI interaction
-- Usage: osascript ftc_save_report.applescript "Robert FTC 1" "/path/to/save/dir"
-- Pre-condition: Chromium print dialog is open with "Save as PDF" destination.

on run argv
	set reportName to item 1 of argv
	set saveDir to item 2 of argv
	set appName to "Google Chrome for Testing"

	set t0 to (current date)

	tell application appName to activate

	tell application "System Events"
		tell process appName
			-- Wait until app is frontmost (near-instant after activate)
			set frontmost to true
			repeat 20 times
				if frontmost then exit repeat
				delay 0.05
			end repeat

			-- Step 1: Print dialog is the current sheet. Press Enter to Save.
			-- Poll for sheet to exist before pressing Enter.
			repeat 30 times
				if (count of sheets of window 1) > 0 then exit repeat
				delay 0.05
			end repeat
			keystroke return

			-- Step 2: Wait for the OS file save dialog to appear.
			-- The sheet changes content; poll until Cmd+A becomes effective (filename field focused).
			-- We detect by waiting for a new sheet or the same sheet with different UI.
			delay 0.3

			-- Select filename, paste with extension
			keystroke "a" using command down
			delay 0.08
			set the clipboard to reportName & ".pdf"
			delay 0.08
			keystroke "v" using command down
			delay 0.12

			-- Step 3: Cmd+Shift+G opens "Go to folder" sheet
			keystroke "g" using {command down, shift down}
			-- Poll for the Go-to-folder sheet to appear (it's a sheet on top of the save dialog)
			delay 0.25

			-- Paste the save directory path
			set the clipboard to saveDir
			delay 0.08
			keystroke "v" using command down
			delay 0.12

			-- Enter navigates to folder — poll for sheet to dismiss, then small settle
			keystroke return
			delay 0.4

			-- Step 4: Enter clicks Save
			keystroke return

			-- Handle "Replace?" dialog if file already exists
			delay 0.25
			try
				if exists sheet 1 of window 1 then
					keystroke return
				end if
			end try

		end tell
	end tell

	set t1 to (current date)
	return ((t1 - t0) as text) & "s | " & saveDir & "/" & reportName & ".pdf"
end run
