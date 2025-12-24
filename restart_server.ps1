
param (
    [switch]$kill,
    [switch]$attached)

# This script restarts the BlingMyDeck web server.

# --- Step 1: Find and stop any existing server process on port 8000 ---
Write-Host "Checking for a running server on port 8000..."
$processId = (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue).OwningProcess

if ($processId) {
    Write-Host "Found running server with Process ID: $processId. Stopping it now."
    Stop-Process -Id $processId -Force
    Write-Host "Server stopped."
} else {
    Write-Host "No running server found."
}

if (-not $kill) {
    # Add a small delay to ensure the port is fully released
    Start-Sleep -Seconds 1

    if ($attached) {
        # --- Step 2: Start a new server instance (attached/foreground mode) ---
        Write-Host "Starting new server on http://0.0.0.0:8000..."
        Write-Host "Server is running in attached mode. Press Ctrl+C to stop."
        Write-Host ""
        
        # Run uvicorn directly (foreground) so errors are visible
        python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
    } else {
        # --- Step 2: Start a new server instance (background mode) ---
        Write-Host "Starting new server on http://0.0.0.0:8000..."
        
        # Use Start-Process to run uvicorn in the background and get the process ID
        $serverProcess = Start-Process python -ArgumentList "-m uvicorn app.main:app --host 0.0.0.0 --port 8000" -PassThru

        Write-Host "Server started in the background with Process ID: $($serverProcess.Id)."
        Write-Host "To stop the server, run: Stop-Process -Id $($serverProcess.Id)"
        Write-Host "To see errors, run with -attached flag: .\restart_server.ps1 -attached"
    }
}
