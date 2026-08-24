Add-Type -AssemblyName System.IO.Compression.FileSystem
$srcDir = $PSScriptRoot
if (-not $srcDir) { $srcDir = "c:\Users\pc\Desktop\Arcie bot" }
$zipPath = Join-Path $srcDir "bot-upload.zip"
if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

$zip = [System.IO.Compression.ZipFile]::Open($zipPath, 'Create')

$files = @('main.py','app.py','requirements.txt','discloud.config','squarecloud.app','Dockerfile','vercel.json','.env','temp.png','temp_cutout.png','template.png','user_profiles.json','giveaways.json','giveaway_entries.json')
foreach ($f in $files) {
    $fullPath = Join-Path $srcDir $f
    if (Test-Path $fullPath) {
        [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $fullPath, $f, [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
}

# Recursively add static directory
$staticDir = Join-Path $srcDir "static"
if (Test-Path $staticDir) {
    Get-ChildItem -Path $staticDir -Recurse | ForEach-Object {
        if (-not $_.PSIsContainer) {
            $relPath = "static/" + $_.FullName.Substring($staticDir.Length + 1).Replace('\', '/')
            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, $relPath, [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
    }
}

$zip.Dispose()

# Also copy to bot.zip
$botZipPath = Join-Path $srcDir "bot.zip"
Copy-Item $zipPath $botZipPath -Force

Write-Host "Done! Zip created at: $zipPath and $botZipPath Size:" ([math]::Round((Get-Item $zipPath).Length / 1KB, 2)) "KB"
