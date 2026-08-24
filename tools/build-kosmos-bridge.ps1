param(
	[string] $Version = ''
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$sourceDir = Join-Path $root 'wordpress-plugin'
$distDir = Join-Path $root 'dist'

if ( [string]::IsNullOrWhiteSpace( $Version ) ) {
	$pluginHeader = Get-Content ( Join-Path $sourceDir 'kosmos-bridge.php' )
	$versionLine = $pluginHeader | Where-Object { $_ -match '^\s*\*\s+Version:\s+' } | Select-Object -First 1

	if ( -not $versionLine ) {
		throw 'Could not detect plugin version from wordpress-plugin/kosmos-bridge.php.'
	}

	$Version = ( $versionLine -replace '^\s*\*\s+Version:\s+', '' ).Trim()
}

$packageDir = Join-Path $distDir 'kosmos-bridge'
$zipPath = Join-Path $distDir ("kosmos-bridge-{0}.zip" -f $Version)

if ( Test-Path $packageDir ) {
	Remove-Item -LiteralPath $packageDir -Recurse -Force
}

if ( Test-Path $zipPath ) {
	Remove-Item -LiteralPath $zipPath -Force
}

New-Item -ItemType Directory -Path $packageDir | Out-Null
Copy-Item -Path ( Join-Path $sourceDir '*' ) -Destination $packageDir -Recurse -Force

$vendorDir = Join-Path $packageDir 'vendor'
if ( Test-Path $vendorDir ) {
	Remove-Item -LiteralPath $vendorDir -Recurse -Force
}

$lockFile = Join-Path $packageDir 'composer.lock'
if ( Test-Path $lockFile ) {
	Remove-Item -LiteralPath $lockFile -Force
}

Compress-Archive -Path $packageDir -DestinationPath $zipPath -Force
Get-Item -LiteralPath $zipPath | Select-Object FullName, Length, LastWriteTime
