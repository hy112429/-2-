param(
    [Parameter(Mandatory = $true)]
    [string]$EnvFile,

    [string]$BaseUrl = 'http://127.0.0.1:8094/api/v1/registry',

    [string]$EvidenceDirectory = (Join-Path $PSScriptRoot 'evidence')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Write-EvidenceJson {
    param([string]$Name, [object]$Value)
    $json = ConvertTo-Json -InputObject $Value -Depth 20
    [System.IO.File]::WriteAllText(
        (Join-Path $EvidenceDirectory $Name),
        $json + [Environment]::NewLine,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Invoke-RegistryHttp {
    param(
        [string]$Uri,
        [ValidateSet('GET', 'POST')][string]$Method = 'GET',
        [AllowNull()][object]$Body = $null,
        [AllowNull()][string]$BearerToken = $null,
        [switch]$NoAuthorization
    )

    $headers = @{}
    if (-not $NoAuthorization -and $null -ne $BearerToken) {
        $headers.Authorization = "Bearer $BearerToken"
    }
    $request = @{
        Uri = $Uri
        Method = $Method
        Headers = $headers
        UseBasicParsing = $true
        TimeoutSec = 30
        ErrorAction = 'Stop'
    }
    if ($null -ne $Body) {
        $request.ContentType = 'application/json'
        $request.Body = ConvertTo-Json -InputObject $Body -Depth 20 -Compress
    }

    try {
        $response = Invoke-WebRequest @request
        return [pscustomobject]@{
            StatusCode = [int]$response.StatusCode
            Content = [string]$response.Content
        }
    } catch {
        $webResponse = $_.Exception.Response
        if ($null -eq $webResponse) { throw }
        if ($webResponse -is [System.Net.Http.HttpResponseMessage]) {
            # PowerShell 7 wraps HTTP failures in HttpResponseMessage; Windows PowerShell uses WebResponse.
            # PowerShell may already have disposed the response body; this smoke check only needs the status.
            $content = ''
            $statusCode = [int]$webResponse.StatusCode
        } else {
            $reader = New-Object System.IO.StreamReader($webResponse.GetResponseStream())
            try { $content = $reader.ReadToEnd() } finally { $reader.Dispose() }
            $statusCode = [int]$webResponse.StatusCode
        }
        return [pscustomobject]@{
            StatusCode = $statusCode
            Content = $content
        }
    }
}

function Assert-HttpStatus {
    param($Response, [int]$Expected, [string]$Label)
    if ($Response.StatusCode -ne $Expected) {
        throw "$Label returned HTTP $($Response.StatusCode); expected $Expected."
    }
}

function Get-JsonContent {
    param($Response, [string]$Label)
    try { return $Response.Content | ConvertFrom-Json -ErrorAction Stop }
    catch { throw "$Label returned invalid JSON." }
}

function Get-ReservationSummary {
    param($Response)
    $parsed = Get-JsonContent $Response 'reservation response'
    $reserved = @($parsed.reserved | ForEach-Object {
        [ordered]@{ productId = $_.productId; state = $_.state }
    })
    $rejected = @($parsed.rejected | ForEach-Object {
        [ordered]@{ productId = $_.productId; reason = $_.reason }
    })
    return [ordered]@{ reserved = $reserved; rejected = $rejected }
}

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw "EnvFile not found: $EnvFile"
}
New-Item -ItemType Directory -Force -Path $EvidenceDirectory | Out-Null

$envValues = @{}
foreach ($line in Get-Content -LiteralPath $EnvFile -Encoding utf8) {
    if ($line -match '^\s*([^#=\s]+)\s*=\s*(.*?)\s*$') {
        $envValues[$matches[1]] = $matches[2]
    }
}
$token = [string]$envValues['REGISTRY_API_TOKEN']
if ([string]::IsNullOrWhiteSpace($token) -or $token -like 'replace-*') {
    throw 'REGISTRY_API_TOKEN is missing or still a placeholder in EnvFile.'
}

$nonce = [guid]::NewGuid().ToString('N').Substring(0, 12)
$healthResponse = Invoke-RegistryHttp "$BaseUrl/health"
Assert-HttpStatus $healthResponse 200 'health'
$health = Get-JsonContent $healthResponse 'health'
if ($health.ok -ne $true) { throw 'health did not return ok: true.' }
Write-EvidenceJson 'smoke-health.json' $health

$statusResponse = Invoke-RegistryHttp "$BaseUrl/status" -BearerToken $token
Assert-HttpStatus $statusResponse 200 'authenticated status'
$status = Get-JsonContent $statusResponse 'status'
if ($status.ok -ne $true) { throw 'status did not return ok: true.' }
Write-EvidenceJson 'smoke-status.json' $status

$missingAuth = Invoke-RegistryHttp "$BaseUrl/status" -NoAuthorization
$wrongAuth = Invoke-RegistryHttp "$BaseUrl/status" -BearerToken "invalid-$nonce"
Assert-HttpStatus $missingAuth 401 'status without Bearer'
Assert-HttpStatus $wrongAuth 401 'status with invalid Bearer'
$unauthText = @(
    "missing_bearer_status=$($missingAuth.StatusCode)"
    "invalid_bearer_status=$($wrongAuth.StatusCode)"
) -join [Environment]::NewLine
[System.IO.File]::WriteAllText(
    (Join-Path $EvidenceDirectory 'smoke-unauth.txt'),
    $unauthText + [Environment]::NewLine,
    (New-Object System.Text.UTF8Encoding($false))
)

$conflictProduct = "smoke-p28-conflict-$nonce"
$conflictUrl = "$BaseUrl/reservations"
$requestIdA = [guid]::NewGuid().ToString()
$requestIdB = [guid]::NewGuid().ToString()
$jobScript = {
    param($Uri, $Token, $RequestId, $AccountKey, $CategoryId, $ProductId)
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    $headers = @{ Authorization = "Bearer $Token" }
    $body = @{
        requestId = $RequestId
        accountKey = $AccountKey
        items = @(@{ productId = $ProductId; leafCategoryId = $CategoryId; title = 'isolated smoke fixture' })
    } | ConvertTo-Json -Depth 10 -Compress
    $response = Invoke-WebRequest -Uri $Uri -Method POST -Headers $headers -ContentType 'application/json' -Body $body -UseBasicParsing -TimeoutSec 30 -ErrorAction Stop
    [pscustomobject]@{ StatusCode = [int]$response.StatusCode; Content = [string]$response.Content }
}
$jobA = Start-Job -ScriptBlock $jobScript -ArgumentList $conflictUrl, $token, $requestIdA, "smoke-p28-account-a-$nonce", "smoke-p28-category-a-$nonce", $conflictProduct
$jobB = Start-Job -ScriptBlock $jobScript -ArgumentList $conflictUrl, $token, $requestIdB, "smoke-p28-account-b-$nonce", "smoke-p28-category-b-$nonce", $conflictProduct
try {
    $concurrentResponses = @(
        Receive-Job -Job $jobA -Wait -ErrorAction Stop
        Receive-Job -Job $jobB -Wait -ErrorAction Stop
    )
} finally {
    Remove-Job -Job @($jobA, $jobB) -Force -ErrorAction SilentlyContinue
}
$conflictOutcomes = @()
foreach ($response in $concurrentResponses) {
    Assert-HttpStatus $response 200 'concurrent reservation'
    $summary = Get-ReservationSummary ([pscustomobject]@{ StatusCode = $response.StatusCode; Content = $response.Content })
    $conflictOutcomes += $summary
}
$successCount = @($conflictOutcomes | Where-Object { @($_.reserved).Count -eq 1 }).Count
$productConflicts = @($conflictOutcomes | ForEach-Object { $_.rejected } | Where-Object { $_.reason -eq 'PRODUCT_ALREADY_REGISTERED' }).Count
if ($successCount -ne 1 -or $productConflicts -ne 1) {
    throw "Concurrent reservation expected exactly one success and one PRODUCT_ALREADY_REGISTERED; got success=$successCount conflict=$productConflicts."
}
Write-EvidenceJson 'smoke-reserve-conflict.json' ([ordered]@{
    productId = $conflictProduct
    outcomes = $conflictOutcomes
    successCount = $successCount
    productAlreadyRegisteredCount = $productConflicts
})

$releaseProduct = "smoke-p28-release-$nonce"
$releaseAccount = "smoke-p28-release-account-$nonce"
$releaseCategory = "smoke-p28-release-category-$nonce"
$firstReserve = Invoke-RegistryHttp "$BaseUrl/reservations" -Method POST -BearerToken $token -Body @{
    requestId = [guid]::NewGuid().ToString()
    accountKey = $releaseAccount
    items = @(@{ productId = $releaseProduct; leafCategoryId = $releaseCategory; title = 'isolated smoke fixture' })
}
Assert-HttpStatus $firstReserve 200 'initial release-flow reservation'
$firstParsed = Get-JsonContent $firstReserve 'initial release-flow reservation'
if (@($firstParsed.reserved).Count -ne 1) { throw 'Initial release-flow reservation failed.' }
$releaseToken = [string]$firstParsed.reserved[0].token
$beginFirst = Invoke-RegistryHttp "$BaseUrl/reservations/$releaseToken/begin" -Method POST -BearerToken $token
Assert-HttpStatus $beginFirst 200 'release-flow begin'
$finishFirst = Invoke-RegistryHttp "$BaseUrl/reservations/$releaseToken/finish" -Method POST -BearerToken $token -Body @{ outcome = 'NOT_ADDED' }
Assert-HttpStatus $finishFirst 200 'NOT_ADDED finish'
$finishFirstJson = Get-JsonContent $finishFirst 'NOT_ADDED finish'
if ($finishFirstJson.state -ne 'RELEASED') { throw 'NOT_ADDED did not release the reservation.' }
$retryReserve = Invoke-RegistryHttp "$BaseUrl/reservations" -Method POST -BearerToken $token -Body @{
    requestId = [guid]::NewGuid().ToString()
    accountKey = $releaseAccount
    items = @(@{ productId = $releaseProduct; leafCategoryId = $releaseCategory; title = 'isolated smoke retry fixture' })
}
Assert-HttpStatus $retryReserve 200 'reservation retry after release'
$retryParsed = Get-JsonContent $retryReserve 'reservation retry after release'
if (@($retryParsed.reserved).Count -ne 1) { throw 'Reservation could not be retried after NOT_ADDED release.' }
$retryToken = [string]$retryParsed.reserved[0].token
$beginRetry = Invoke-RegistryHttp "$BaseUrl/reservations/$retryToken/begin" -Method POST -BearerToken $token
Assert-HttpStatus $beginRetry 200 'retry begin'
$cleanupRetry = Invoke-RegistryHttp "$BaseUrl/reservations/$retryToken/finish" -Method POST -BearerToken $token -Body @{ outcome = 'NOT_ADDED' }
Assert-HttpStatus $cleanupRetry 200 'retry cleanup release'
$cleanupRetryJson = Get-JsonContent $cleanupRetry 'retry cleanup release'
if ($cleanupRetryJson.state -ne 'RELEASED') { throw 'Retry cleanup did not release its fixture.' }
Write-EvidenceJson 'smoke-release-retry.json' ([ordered]@{
    productId = $releaseProduct
    initialReservation = (Get-ReservationSummary $firstReserve)
    beginState = (Get-JsonContent $beginFirst 'release-flow begin').state
    notAddedState = $finishFirstJson.state
    retryReservation = (Get-ReservationSummary $retryReserve)
    retryCleanupState = $cleanupRetryJson.state
})

$uncertainProduct = "smoke-p28-uncertain-$nonce"
$uncertainAccount = "smoke-p28-uncertain-account-$nonce"
$uncertainCategory = "smoke-p28-uncertain-category-$nonce"
$uncertainReserve = Invoke-RegistryHttp "$BaseUrl/reservations" -Method POST -BearerToken $token -Body @{
    requestId = [guid]::NewGuid().ToString()
    accountKey = $uncertainAccount
    items = @(@{ productId = $uncertainProduct; leafCategoryId = $uncertainCategory; title = 'isolated smoke fixture' })
}
Assert-HttpStatus $uncertainReserve 200 'UNCERTAIN reservation'
$uncertainParsed = Get-JsonContent $uncertainReserve 'UNCERTAIN reservation'
if (@($uncertainParsed.reserved).Count -ne 1) { throw 'UNCERTAIN flow initial reservation failed.' }
$uncertainToken = [string]$uncertainParsed.reserved[0].token
$beginUncertain = Invoke-RegistryHttp "$BaseUrl/reservations/$uncertainToken/begin" -Method POST -BearerToken $token
Assert-HttpStatus $beginUncertain 200 'UNCERTAIN begin'
$finishUncertain = Invoke-RegistryHttp "$BaseUrl/reservations/$uncertainToken/finish" -Method POST -BearerToken $token -Body @{ outcome = 'UNKNOWN' }
Assert-HttpStatus $finishUncertain 200 'UNKNOWN finish'
$finishUncertainJson = Get-JsonContent $finishUncertain 'UNKNOWN finish'
if ($finishUncertainJson.state -ne 'UNCERTAIN') { throw 'UNKNOWN did not transition to UNCERTAIN.' }
$pendingResponse = Invoke-RegistryHttp "$BaseUrl/pending" -BearerToken $token
Assert-HttpStatus $pendingResponse 200 'pending query'
$pending = Get-JsonContent $pendingResponse 'pending query'
$matchingPending = @($pending | Where-Object { $_.productId -eq $uncertainProduct -and $_.state -eq 'UNCERTAIN' })
if ($matchingPending.Count -ne 1) { throw 'UNCERTAIN fixture was not visible in /pending.' }
$lockedRetry = Invoke-RegistryHttp "$BaseUrl/reservations" -Method POST -BearerToken $token -Body @{
    requestId = [guid]::NewGuid().ToString()
    accountKey = "smoke-p28-uncertain-other-account-$nonce"
    items = @(@{ productId = $uncertainProduct; leafCategoryId = "smoke-p28-uncertain-other-category-$nonce"; title = 'isolated smoke lock probe' })
}
Assert-HttpStatus $lockedRetry 200 'UNCERTAIN lock probe'
$lockedSummary = Get-ReservationSummary $lockedRetry
if (@($lockedSummary.reserved).Count -ne 0 -or @($lockedSummary.rejected | Where-Object { $_.reason -eq 'PRODUCT_ALREADY_REGISTERED' }).Count -ne 1) {
    throw 'UNCERTAIN reservation did not keep the product ID locked.'
}
Write-EvidenceJson 'smoke-uncertain-pending.json' ([ordered]@{
    productId = $uncertainProduct
    finishState = $finishUncertainJson.state
    pendingMatch = [ordered]@{ productId = $matchingPending[0].productId; accountKey = $matchingPending[0].accountKey; state = $matchingPending[0].state }
    lockedRetry = $lockedSummary
})

Write-Output "PASS health/status/auth/conflict/release-retry/uncertain-pending; evidence: $EvidenceDirectory"
