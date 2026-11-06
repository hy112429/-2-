# Step 24 隔离联测：独立 MySQL 8、Redis 7 容器和服务端，不接触业务库。
$ErrorActionPreference = 'Stop'
$mysqlHome = 'D:\mysql-8.0.29-winx64'
$mysqlExe = Join-Path $mysqlHome 'bin\mysqld.exe'
$mysqlClient = Join-Path $mysqlHome 'bin\mysql.exe'
$javaExe = 'D:\jdk17\bin\java.exe'
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$jar = Join-Path $project 'target\touguang-center-0.1.0-SNAPSHOT.jar'
$runId = [guid]::NewGuid().ToString('N')
$root = Join-Path 'D:\software\codex' ('step24-smoke-' + $runId)
$mysqlPort = 13318
$redisPort = 16388
$appPort = 18092
$mysqlProcess = $null
$redisContainer = 'tg-step24-' + $runId.Substring(0, 12)
$appProcess = $null
$dbPassword = [guid]::NewGuid().ToString('N') + 'Aa1!'
$adminPassword = [guid]::NewGuid().ToString('N') + 'Bb2!'
$readonlyPassword = [guid]::NewGuid().ToString('N') + 'Cc3!'
Add-Type -AssemblyName System.Net.Http
$http = [System.Net.Http.HttpClient]::new()
$http.Timeout = [timespan]::FromSeconds(12)

function Send-Request([string] $method, [string] $path, $body = $null, [string] $token = '', [string] $machine = '') {
    $request = [System.Net.Http.HttpRequestMessage]::new($method, "http://127.0.0.1:$appPort$path")
    if ($token) { $request.Headers.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $token) }
    if ($machine) { $request.Headers.Add('X-Machine-Id', $machine) }
    if ($null -ne $body) {
        $request.Content = [System.Net.Http.StringContent]::new(($body | ConvertTo-Json -Depth 8 -Compress), [System.Text.Encoding]::UTF8, 'application/json')
    }
    try {
        $response = $http.SendAsync($request).GetAwaiter().GetResult()
        $content = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        return [pscustomobject]@{ Status = [int]$response.StatusCode; Body = ($content | ConvertFrom-Json) }
    } finally { $request.Dispose() }
}

function Assert-Result([bool] $condition, [string] $message) {
    if (-not $condition) { throw $message }
}

try {
    foreach ($file in @($mysqlExe, $mysqlClient, $javaExe, $jar)) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "缺少隔离联测依赖：$file" }
    }
    foreach ($port in @($mysqlPort, $redisPort, $appPort)) {
        if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { throw "隔离端口 $port 已被占用" }
    }
    $dataDir = Join-Path $root 'mysql-data'
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    & $mysqlExe --no-defaults --initialize-insecure "--basedir=$mysqlHome" "--datadir=$dataDir" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '隔离 MySQL 初始化失败' }
    $mysqlProcess = Start-Process -FilePath $mysqlExe -ArgumentList @('--no-defaults', "--basedir=$mysqlHome", "--datadir=$dataDir", "--port=$mysqlPort", '--bind-address=127.0.0.1', '--mysqlx=OFF', '--console') -WorkingDirectory $mysqlHome -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root 'mysql.stdout.log') -RedirectStandardError (Join-Path $root 'mysql.stderr.log')
    docker run --rm -d --name $redisContainer -p "127.0.0.1:${redisPort}:6379" 'redis:7-alpine' 'redis-server' '--save' '' '--appendonly' 'no' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '隔离 Redis 7 容器启动失败' }
    for ($i = 0; $i -lt 40; $i++) {
        if ((Get-NetTCPConnection -State Listen -LocalPort $mysqlPort -ErrorAction SilentlyContinue) -and (Get-NetTCPConnection -State Listen -LocalPort $redisPort -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Seconds 1
    }
    Assert-Result ([bool](Get-NetTCPConnection -State Listen -LocalPort $mysqlPort -ErrorAction SilentlyContinue)) '隔离 MySQL 未就绪'
    Assert-Result ([bool](Get-NetTCPConnection -State Listen -LocalPort $redisPort -ErrorAction SilentlyContinue)) '隔离 Redis 未就绪'

    $sql = "CREATE DATABASE touguang_center CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci; CREATE USER 'touguang_app'@'127.0.0.1' IDENTIFIED BY '$dbPassword'; GRANT ALL PRIVILEGES ON touguang_center.* TO 'touguang_app'@'127.0.0.1';"
    $sql | & $mysqlClient --no-defaults --host=127.0.0.1 "--port=$mysqlPort" --user=root --batch 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '隔离数据库建库失败' }

    $env:DB_URL = "jdbc:mysql://127.0.0.1:$mysqlPort/touguang_center?useSSL=false&allowPublicKeyRetrieval=true&serverTimezone=Asia/Shanghai"
    $env:DB_USERNAME = 'touguang_app'
    $env:DB_PASSWORD = $dbPassword
    $env:REDIS_HOST = '127.0.0.1'
    $env:REDIS_PORT = [string]$redisPort
    $env:REDIS_PASSWORD = ''
    $env:SERVER_PORT = [string]$appPort
    $env:CENTER_BOOTSTRAP_ADMIN_USERNAME = 'smoke_admin'
    $env:CENTER_BOOTSTRAP_ADMIN_PASSWORD = $adminPassword
    $appProcess = Start-Process -FilePath $javaExe -ArgumentList @('-jar', $jar) -WorkingDirectory $project -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $root 'app.stdout.log') -RedirectStandardError (Join-Path $root 'app.stderr.log')

    $health = $null
    for ($i = 0; $i -lt 45; $i++) {
        if ($appProcess.HasExited) { throw '服务端在健康检查之前退出' }
        try { $health = Send-Request 'GET' '/api/health'; if ($health.Status -eq 200) { break } } catch { }
        Start-Sleep -Seconds 1
    }
    Assert-Result ($null -ne $health -and $health.Status -eq 200 -and $health.Body.data.mysql.status -eq 'UP' -and $health.Body.data.redis.status -eq 'UP') '健康检查未达到 UP'
    $noToken = Send-Request 'GET' '/api/clients'
    Assert-Result ($noToken.Status -eq 403) '未登录客户端列表未返回 403'

    $login = Send-Request 'POST' '/api/auth/login' @{ username = 'smoke_admin'; password = $adminPassword }
    Assert-Result ($login.Status -eq 200 -and $login.Body.data.token) '管理员登录失败'
    $adminToken = [string]$login.Body.data.token
    $adminId = [long]$login.Body.data.user.id
    $me = Send-Request 'GET' '/api/users/me' $null $adminToken
    Assert-Result ($me.Status -eq 200 -and @($me.Body.data.roles).Contains('ADMIN')) '当前用户角色读取失败'

    $created = Send-Request 'POST' '/api/users' @{ username = 'smoke_reader'; password = $readonlyPassword; displayName = '只读测试'; roles = @('READ_ONLY') } $adminToken
    Assert-Result ($created.Status -eq 200 -and @($created.Body.data.roles).Contains('READ_ONLY')) '创建只读用户失败'
    $readerBeforeBinding = Send-Request 'POST' '/api/auth/login' @{ username = 'smoke_reader'; password = $readonlyPassword }
    Assert-Result ($readerBeforeBinding.Status -eq 403) '未绑定机器的只读账号登录未返回 403'

    $machineId = ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')).ToLowerInvariant()
    $registered = Send-Request 'POST' '/api/clients/register' @{ machineId = $machineId; machineName = 'step23-isolated-client' }
    Assert-Result ($registered.Status -eq 200 -and $registered.Body.data.status -eq 'PENDING') '客户端待授权登记失败'
    $clientId = [long]$registered.Body.data.id
    $listing = Send-Request 'GET' '/api/clients' $null $adminToken
    Assert-Result ($listing.Status -eq 200 -and @($listing.Body.data | Where-Object id -eq $clientId).Count -eq 1) '管理员客户端列表未显示登记机器'
    $readerId = [long]$created.Body.data.id
    $bound = Send-Request 'POST' "/api/clients/$clientId/bind" @{ userId = $readerId } $adminToken
    Assert-Result ($bound.Status -eq 200 -and $bound.Body.data.status -eq 'BOUND' -and $bound.Body.data.boundUserId -eq $readerId) '管理员绑定失败'
    $again = Send-Request 'POST' '/api/clients/register' @{ machineId = $machineId; machineName = 'step23-isolated-client' }
    Assert-Result ($again.Status -eq 200 -and $again.Body.data.id -eq $clientId -and $again.Body.data.status -eq 'BOUND') '重复启动未保持相同机器身份'

    $readerLogin = Send-Request 'POST' '/api/auth/login' @{ username = 'smoke_reader'; password = $readonlyPassword; machineId = $machineId }
    Assert-Result ($readerLogin.Status -eq 200 -and $readerLogin.Body.data.token) '绑定机器的只读用户登录失败'
    $readerToken = [string]$readerLogin.Body.data.token
    $readerNoMachineHeader = Send-Request 'GET' '/api/roles' $null $readerToken
    Assert-Result ($readerNoMachineHeader.Status -eq 403) '机器会话未校验机器标识'
    $readerRequest = [System.Net.Http.HttpRequestMessage]::new('GET', "http://127.0.0.1:$appPort/api/roles")
    $readerRequest.Headers.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $readerToken)
    $readerRequest.Headers.Add('X-Machine-Id', $machineId)
    $readerResponse = $http.SendAsync($readerRequest).GetAwaiter().GetResult()
    Assert-Result ([int]$readerResponse.StatusCode -eq 200) '绑定机器的只读用户无法查询角色'
    $readerResponse.Dispose()
    $readerRequest.Dispose()
    $readerDenied = Send-Request 'GET' '/api/clients' $null $readerToken $machineId
    Assert-Result ($readerDenied.Status -eq 403) '只读用户访问客户端列表未返回 403'

    $ungranted = Send-Request 'POST' '/api/clients/heartbeat' @{} $readerToken $machineId
    Assert-Result ($ungranted.Status -eq 200 -and $ungranted.Body.data.status -eq 'UNGRANTED' -and $ungranted.Body.data.shouldLock) '未授予机器的心跳必须下发锁定'
    $readerCannotGrant = Send-Request 'POST' "/api/clients/$clientId/license/grant" @{ requestId = [guid]::NewGuid().ToString(); days = 1 } $readerToken $machineId
    Assert-Result ($readerCannotGrant.Status -eq 403) '只读账号不应有授权权限'

    $grantId = [guid]::NewGuid().ToString()
    $grant = Send-Request 'POST' "/api/clients/$clientId/license/grant" @{ requestId = $grantId; days = 1 } $adminToken
    Assert-Result ($grant.Status -eq 200 -and $grant.Body.data.status -eq 'EXPIRING' -and $grant.Body.data.remainingDays -eq 1) '管理员授予一天授权失败'
    $grantAgain = Send-Request 'POST' "/api/clients/$clientId/license/grant" @{ requestId = $grantId; days = 1 } $adminToken
    Assert-Result ($grantAgain.Status -eq 200 -and $grantAgain.Body.data.expiresAt -eq $grant.Body.data.expiresAt) "重复授予请求不幂等：status=$($grantAgain.Status), message=$($grantAgain.Body.message), first=$($grant.Body.data.expiresAt), again=$($grantAgain.Body.data.expiresAt)"
    $active = Send-Request 'POST' '/api/clients/heartbeat' @{ sequence = 1; clientTime = '2035-01-01T00:00:00+08:00' } $readerToken $machineId
    Assert-Result ($active.Status -eq 200 -and $active.Body.data.valid -and -not $active.Body.data.shouldLock -and $active.Body.data.offlineGraceSeconds -eq 600) '有效心跳未返回可用与宽限'
    $wrongMachine = ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')).ToLowerInvariant()
    $mismatch = Send-Request 'POST' '/api/clients/heartbeat' @{} $readerToken $wrongMachine
    Assert-Result ($mismatch.Status -eq 403) '机器标识不匹配未拒绝'

    $renewId = [guid]::NewGuid().ToString()
    $renew = Send-Request 'POST' "/api/clients/$clientId/license/renew" @{ requestId = $renewId; days = 4 } $adminToken
    Assert-Result ($renew.Status -eq 200 -and $renew.Body.data.status -eq 'ACTIVE' -and $renew.Body.data.grantedDaysTotal -eq 5) '续期四天失败'
    $renewAgain = Send-Request 'POST' "/api/clients/$clientId/license/renew" @{ requestId = $renewId; days = 4 } $adminToken
    Assert-Result ($renewAgain.Status -eq 200 -and $renewAgain.Body.data.expiresAt -eq $renew.Body.data.expiresAt) '重复续期请求不幂等'
    $operationCount = ("SELECT COUNT(*) FROM touguang_center.center_license_operation WHERE client_id=$clientId;" | & $mysqlClient --no-defaults --host=127.0.0.1 "--port=$mysqlPort" --user=root --batch --skip-column-names).Trim()
    Assert-Result ($operationCount -eq '2') '重复请求写入了额外授权操作'
    $clockAnomalyCount = ("SELECT COUNT(*) FROM touguang_center.center_heartbeat WHERE client_id=$clientId AND clock_anomaly=1;" | & $mysqlClient --no-defaults --host=127.0.0.1 "--port=$mysqlPort" --user=root --batch --skip-column-names).Trim()
    Assert-Result ($clockAnomalyCount -eq '1') '客户端时间异常未落库'

    $disable = Send-Request 'POST' "/api/clients/$clientId/license/disable" @{ requestId = [guid]::NewGuid().ToString() } $adminToken
    $disabledHeartbeat = Send-Request 'POST' '/api/clients/heartbeat' @{} $readerToken $machineId
    Assert-Result ($disable.Status -eq 200 -and $disabledHeartbeat.Body.data.status -eq 'DISABLED' -and $disabledHeartbeat.Body.data.shouldLock) '停用后心跳未锁定'
    $regrant = Send-Request 'POST' "/api/clients/$clientId/license/grant" @{ requestId = [guid]::NewGuid().ToString(); days = 1 } $adminToken
    Assert-Result ($regrant.Status -eq 200 -and $regrant.Body.data.valid) '停用后重新授予失败'
    $disabledAtCount = ("SELECT COUNT(*) FROM touguang_center.center_license WHERE client_id=$clientId AND disabled_at IS NOT NULL;" | & $mysqlClient --no-defaults --host=127.0.0.1 "--port=$mysqlPort" --user=root --batch --skip-column-names).Trim()
    Assert-Result ($disabledAtCount -eq '0') '重新授予后停用时间未清除'
    "UPDATE touguang_center.center_license SET expires_at='2020-01-01 00:00:00' WHERE client_id=$clientId;" | & $mysqlClient --no-defaults --host=127.0.0.1 "--port=$mysqlPort" --user=root --batch | Out-Null
    Assert-Result ($LASTEXITCODE -eq 0) '隔离库设定过期授权失败'
    $expiredHeartbeat = Send-Request 'POST' '/api/clients/heartbeat' @{} $readerToken $machineId
    Assert-Result ($expiredHeartbeat.Status -eq 200 -and $expiredHeartbeat.Body.data.status -eq 'EXPIRED' -and $expiredHeartbeat.Body.data.shouldLock) '过期后心跳未锁定'
    $heartbeatCount = ("SELECT COUNT(*) FROM touguang_center.center_heartbeat WHERE client_id=$clientId;" | & $mysqlClient --no-defaults --host=127.0.0.1 "--port=$mysqlPort" --user=root --batch --skip-column-names).Trim()
    Assert-Result ([int]$heartbeatCount -ge 4) '心跳记录未落库'

    $logout = Send-Request 'POST' '/api/auth/logout' $null $adminToken
    $revoked = Send-Request 'GET' '/api/users/me' $null $adminToken
    Assert-Result ($logout.Status -eq 200 -and $revoked.Status -eq 403) '退出登录后 Token 未失效'
    [pscustomobject]@{ Result = 'PASS'; Health = 'UP'; Mysql = 'UP'; Redis = 'UP'; Ungranted = $ungranted.Body.data.status; ActiveHeartbeat = $active.Body.data.status; Disabled = $disabledHeartbeat.Body.data.status; Expired = $expiredHeartbeat.Body.data.status; MachineMismatchStatus = $mismatch.Status; HeartbeatRecords = [int]$heartbeatCount; LicenseOperations = [int]$operationCount; ClockAnomalies = [int]$clockAnomalyCount; LogoutRevokedStatus = $revoked.Status; IsolatedRoot = $root } | ConvertTo-Json -Compress
} finally {
    foreach ($process in @($appProcess, $mysqlProcess)) {
        if ($null -ne $process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    }
    docker stop $redisContainer 2>$null | Out-Null
    # Windows mysqld 可能由启动器派生工作进程，只清理本次独立数据目录的子进程。
    if ($dataDir) {
        Get-CimInstance Win32_Process -Filter "Name = 'mysqld.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like "*--datadir=$dataDir *" -and $_.CommandLine -like "*--port=$mysqlPort *" } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    $http.Dispose()
    Remove-Item Env:DB_URL,Env:DB_USERNAME,Env:DB_PASSWORD,Env:REDIS_HOST,Env:REDIS_PORT,Env:REDIS_PASSWORD,Env:SERVER_PORT,Env:CENTER_BOOTSTRAP_ADMIN_USERNAME,Env:CENTER_BOOTSTRAP_ADMIN_PASSWORD -ErrorAction SilentlyContinue
}
