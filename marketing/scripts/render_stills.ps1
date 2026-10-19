$artifactDir = "C:\Users\bb201\.gemini\antigravity-ide\brain\74dad804-a980-4e0b-a5fa-b4d22e643e53\stills"
New-Item -ItemType Directory -Force -Path $artifactDir | Out-Null
New-Item -ItemType Directory -Force -Path "preview_stills" | Out-Null

$frames = @(
    @{ name = "s01_problem";        frame = 350 },
    @{ name = "s02_heroreveal";     frame = 880 },
    @{ name = "s03_learn";          frame = 1450 },
    @{ name = "s05_hybridsearch";   frame = 2620 },
    @{ name = "s06_multiagent";     frame = 3200 },
    @{ name = "s07_contradictions"; frame = 3660 },
    @{ name = "s08_ttldecay";       frame = 4000 },
    @{ name = "s09_privacy";        frame = 4320 },
    @{ name = "s10_interfaces";     frame = 4650 },
    @{ name = "s11_install";        frame = 4960 },
    @{ name = "s12_tagline";        frame = 5320 },
    @{ name = "s12_cta";            frame = 5520 }
)

Write-Host "=== Rendering Dark Mode Stills ==="
Set-Location "c:\Users\bb201\Desktop\Mesh\mesh-ad-sfx"
foreach ($item in $frames) {
    $outPath = "c:\Users\bb201\Desktop\Mesh\preview_stills\dark_$($item.name).png"
    Write-Host "Rendering dark $($item.name) at frame $($item.frame)..."
    npx remotion still src/index.ts MeshAd $outPath --frame=$($item.frame) --log=error
    Copy-Item $outPath "$artifactDir\dark_$($item.name).png" -Force
}

Write-Host "=== Rendering Light Mode Stills ==="
Set-Location "c:\Users\bb201\Desktop\Mesh\mesh-ad-light-sfx"
foreach ($item in $frames) {
    $outPath = "c:\Users\bb201\Desktop\Mesh\preview_stills\light_$($item.name).png"
    Write-Host "Rendering light $($item.name) at frame $($item.frame)..."
    npx remotion still src/index.ts MeshAd $outPath --frame=$($item.frame) --log=error
    Copy-Item $outPath "$artifactDir\light_$($item.name).png" -Force
}

Write-Host "=== All stills rendered and copied to artifacts directory successfully! ==="
