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
			-- Poll for print preview sheet (up to 3s — preview can be slow to render)
			repeat 60 times
				if (count of sheets of window 1) > 0 then exit repeat
				delay 0.05
			end repeat
			keystroke return

			-- Step 2: Wait for OS file save dialog to receive focus.
			-- The save dialog is a sheet on window 1. We need it focused before Cmd+A.
			-- Poll for ~2s — the dialog transition isn't instant.
			delay 1.5
			repeat 20 times
				try
					if (count of sheets of window 1) > 0 then exit repeat
				end try
				delay 0.1
			end repeat

			-- Select filename, paste with extension
			-- Small delays needed for clipboard/focus to settle between keystrokes
			keystroke "a" using command down
			delay 0.2
			set the clipboard to reportName & ".pdf"
			delay 0.15
			keystroke "v" using command down
			delay 0.3

			-- Step 3: Cmd+Shift+G opens "Go to folder" sheet
			keystroke "g" using {command down, shift down}
			delay 0.6

			-- Paste the save directory path
			set the clipboard to saveDir
			delay 0.15
			keystroke "v" using command down
			delay 0.3

			-- Enter navigates to folder
			keystroke return
			delay 0.8

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
