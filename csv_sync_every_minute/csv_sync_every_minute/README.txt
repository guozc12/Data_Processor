CSVDateSync - Windows background CSV copy (1-minute interval)

Source:      \\store03.tue.nl\ap_kat1_raw_lab_data\Strontium\Images
Destination: D:\Data_Processor\data
Date filter: 2026-10-10 through the Windows computer's current local date
Filter:      *.csv only; original YYYY-MM-DD\scan... folders preserved

INSTALL / UPGRADE
1) Extract this ZIP to a regular folder (do not run directly inside the ZIP).
2) Double-click install_csv_sync.bat as the same Windows user who can access the share.
3) The installer saves the worker under %LOCALAPPDATA%\CSVDateSync and installs/replaces
   the CSVDateSync scheduled task for that user.
4) The first sync starts immediately; after that it triggers EVERY MINUTE.
   It also triggers at Windows sign-in (after a reboot, for example).

BACKGROUND / NETWORK NOTES
- The task runs without visible CMD / PowerShell windows.
- It works while the user is signed in, even with the screen locked.
- It does NOT run while the PC is shut down or asleep.
- It does NOT run before sign-in. UNC access uses the signed-in user's credentials.
- A corporate network or VPN connection may be required to reach store03.tue.nl.
- If a previous run is still active, the next run is skipped (no concurrent copies).
- Since the entire date range is rechecked, a large number of image-heavy folders
  can make each pass take more than 1 minute; it will not necessarily finish every minute.
- The script never deletes source or destination data.

CHECK
- Windows Task Scheduler: task name CSVDateSync (history / Last Run Result).
- Log: %LOCALAPPDATA%\CSVDateSync\sync.log
- To test manually and show latest log output: double-click run_once.bat.
- To remove the scheduled task: double-click uninstall_csv_sync.bat.

No API tokens or passwords are stored in this package.
