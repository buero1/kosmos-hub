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

$python = Get-Command python -ErrorAction Stop

# Compress-Archive writes Windows-style backslashes into ZIP entry names. WordPress
# extracts those as literal filename characters on Linux, so create POSIX ZIP paths.
@'
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
import sys

package_dir = Path(sys.argv[1])
zip_path = Path(sys.argv[2])

with ZipFile(zip_path, 'w', ZIP_DEFLATED) as archive:
    for source in sorted(path for path in package_dir.rglob('*') if path.is_file()):
        archive.write(source, source.relative_to(package_dir.parent).as_posix())
'@ | & $python.Source - $packageDir $zipPath

if ( $LASTEXITCODE -ne 0 ) {
	throw 'Could not create the WordPress plugin ZIP package.'
}

Get-Item -LiteralPath $zipPath | Select-Object FullName, Length, LastWriteTime
