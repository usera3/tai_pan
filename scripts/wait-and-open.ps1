$healthUrl = "http://127.0.0.1:8765/health"
$appUrl = "http://127.0.0.1:8765"

for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 1
        if ($response.StatusCode -eq 200) {
            Start-Process $appUrl
            exit 0
        }
    } catch {
        Start-Sleep -Seconds 1
    }
}

Write-Host "The local service did not start within 60 seconds." -ForegroundColor Red
Write-Host "Check the TMP Link Manager server window for details."
exit 1
