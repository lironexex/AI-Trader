# AI-Trader Startup Script
# Launches backend API, background worker, and frontend in parallel

param(
    [switch]$NoFrontend,
    [switch]$NoWorker
)

$ErrorActionPreference = "Stop"

# Get the script directory
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "Starting AI-Trader..." -ForegroundColor Green
Write-Host "Working directory: $ScriptDir" -ForegroundColor Yellow

# Check if virtual environment exists
if (-Not (Test-Path ".\.venv")) {
    Write-Host "Error: Virtual environment not found. Run setup first." -ForegroundColor Red
    exit 1
}

# Activate virtual environment
Write-Host "Activating virtual environment..." -ForegroundColor Cyan
& ".\.venv\Scripts\Activate.ps1"

# Function to start a process and keep it running
function Start-And-Track {
    param(
        [string]$Name,
        [string]$Command,
        [string]$WorkingDir = $ScriptDir
    )

    Write-Host "Starting $Name..." -ForegroundColor Cyan

    $job = Start-Job -ScriptBlock {
        param($cmd, $wd)
        Set-Location $wd
        Invoke-Expression $cmd
    } -ArgumentList $Command, $WorkingDir

    return $job
}

$jobs = @()

# Start backend API
$backendCommand = "python service/server/main.py"
$jobs += Start-And-Track -Name "Backend API" -Command $backendCommand

# Start background worker (unless disabled)
if (-Not $NoWorker) {
    $workerCommand = "python service/server/worker.py"
    $jobs += Start-And-Track -Name "Background Worker" -Command $workerCommand
} else {
    Write-Host "Skipping background worker..." -ForegroundColor Yellow
}

# Start frontend (unless disabled)
if (-Not $NoFrontend) {
    $frontendCommand = "npm run dev"
    $frontendDir = Join-Path $ScriptDir "service\frontend"
    $jobs += Start-And-Track -Name "Frontend" -Command $frontendCommand -WorkingDir $frontendDir
} else {
    Write-Host "Skipping frontend..." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "All services started!" -ForegroundColor Green
Write-Host "Backend API: http://localhost:8000" -ForegroundColor White
Write-Host "Frontend: http://localhost:5173" -ForegroundColor White
Write-Host ""
Write-Host "Press Ctrl+C to stop all services" -ForegroundColor Yellow

# Wait for all jobs to complete (they won't, unless there's an error)
try {
    while ($true) {
        Start-Sleep -Seconds 1

        # Check if any jobs have completed (indicating an error)
        foreach ($job in $jobs) {
            if ($job.State -eq "Completed") {
                Write-Host "Job $($job.Name) completed unexpectedly. Checking output..." -ForegroundColor Red

                # Get job output
                $output = Receive-Job $job
                Write-Host "Job output:" -ForegroundColor Red
                Write-Host $output -ForegroundColor Red

                # Stop all jobs
                Write-Host "Stopping all services..." -ForegroundColor Yellow
                $jobs | Stop-Job
                $jobs | Remove-Job
                exit 1
            }
        }
    }
} catch {
    Write-Host "Stopping all services..." -ForegroundColor Yellow
    $jobs | Stop-Job
    $jobs | Remove-Job
    exit 0
}