' KeepWSLAlive — launch `wsl.exe ... sleep infinity` with no console window.
' Used by the KeepWSLAlive scheduled task (At log on trigger).
' Keeps the WSL VM resident so the bot stays reachable when no terminal is open.
Set sh = CreateObject("WScript.Shell")
sh.Run "wsl.exe -d Ubuntu -- sleep infinity", 0, False
