# install.ps1 - Instalador leve do Nexus Upload
#
# Em vez de empacotar centenas de MB, este script traz o codigo do repositorio
# privado e monta o ambiente na maquina. Depois disso, atualizar e so abrir o
# app: o launcher da git pull sozinho.
#
# Uso (PowerShell, na pasta onde salvou este arquivo):
#     .\install.ps1
#     .\install.ps1 -Destino "D:\NexusUpload" -Repo "https://github.com/USER/REPO.git"

param(
    [string]$Destino = "$env:LOCALAPPDATA\NexusUpload",
    # repo publico oficial de distribuicao — o uploader nao precisa passar nada
    [string]$Repo = "https://github.com/SPECTROz07/nexus-uploader.git",
    [switch]$SemAtalho
)

$ErrorActionPreference = "Stop"

function Passo($texto) { Write-Host "`n>> $texto" -ForegroundColor Cyan }
function Ok($texto)    { Write-Host "   $texto" -ForegroundColor Green }
function Aviso($texto) { Write-Host "   $texto" -ForegroundColor Yellow }
function Erro($texto)  { Write-Host "   $texto" -ForegroundColor Red }

Write-Host "==============================================" -ForegroundColor White
Write-Host "  Nexus Upload - instalacao" -ForegroundColor White
Write-Host "==============================================" -ForegroundColor White

# ─── 1. pre-requisitos ───────────────────────────────────────────────────────
Passo "Conferindo pre-requisitos"

$git = Get-Command git -ErrorAction SilentlyContinue
if (-not $git) {
    Erro "Git nao encontrado. Instale o Git for Windows: https://git-scm.com/download/win"
    Erro "Ele e quem baixa e atualiza o codigo - sem ele nao da pra seguir."
    exit 1
}
Ok "git   -> $((git --version))"

# procura um Python 3.11+ (o projeto roda em 3.13)
$python = $null
foreach ($tentativa in @("py -3.13", "py -3.12", "py -3.11", "python")) {
    $partes = $tentativa.Split(" ")
    $cmd = Get-Command $partes[0] -ErrorAction SilentlyContinue
    if ($cmd) {
        try {
            $v = & $partes[0] $partes[1..($partes.Length-1)] --version 2>&1
            if ($v -match "Python 3\.(1[1-9]|[2-9]\d)") { $python = $tentativa; break }
        } catch { }
    }
}
if (-not $python) {
    Erro "Python 3.11 ou mais novo nao encontrado. Baixe em https://python.org/downloads"
    Erro "Marque 'Add Python to PATH' na instalacao."
    exit 1
}
Ok "python -> $python"

# ─── 2. repositorio ──────────────────────────────────────────────────────────
if (-not $Repo) {
    Write-Host ""
    $Repo = Read-Host "URL do repositorio (ex: https://github.com/USER/nexus-upload.git)"
}
if (-not $Repo) { Erro "Sem URL nao da pra continuar."; exit 1 }

Passo "Preparando $Destino"

if (Test-Path (Join-Path $Destino ".git")) {
    Ok "Ja existe uma instalacao aqui - atualizando em vez de clonar"
    Push-Location $Destino
    git pull --ff-only
    Pop-Location
} else {
    if ((Test-Path $Destino) -and (Get-ChildItem $Destino -Force | Measure-Object).Count -gt 0) {
        Erro "A pasta $Destino existe e nao esta vazia. Escolha outra com -Destino."
        exit 1
    }
    Ok "Clonando o repositorio (publico, nao pede login)"
    git clone $Repo $Destino
    if ($LASTEXITCODE -ne 0) { Erro "Falha ao clonar. Confira a URL e seu acesso ao repo."; exit 1 }
}

Set-Location $Destino

# ─── 3. ambiente Python ──────────────────────────────────────────────────────
Passo "Montando o ambiente Python (a parte demorada, so agora)"

if (-not (Test-Path "$Destino\venv")) {
    $partes = $python.Split(" ")
    & $partes[0] $partes[1..($partes.Length-1)] -m venv "$Destino\venv"
    if ($LASTEXITCODE -ne 0) { Erro "Falha ao criar o venv."; exit 1 }
    Ok "venv criado"
} else {
    Ok "venv ja existia"
}

$pyVenv = "$Destino\venv\Scripts\python.exe"
& $pyVenv -m pip install --upgrade pip --quiet
Ok "pip atualizado"

Write-Host "   instalando dependencias (varios minutos na primeira vez)..."
& $pyVenv -m pip install -r "$Destino\requirements.txt" --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Erro "Falha no pip install. Veja o erro acima."; exit 1 }
Ok "dependencias instaladas"

# ─── 4. navegador do Playwright ──────────────────────────────────────────────
Passo "Baixando o navegador do Playwright"
$env:PLAYWRIGHT_BROWSERS_PATH = "$Destino\playwright_browsers"
& $pyVenv -m playwright install chromium
if ($LASTEXITCODE -eq 0) { Ok "chromium pronto" } else { Aviso "Falhou - providers com Playwright podem nao funcionar" }

# ─── 5. arquivos de configuracao ─────────────────────────────────────────────
Passo "Configuracao"
$modelos = @{
    "settings.example.json"  = "settings.json"
    "s3_config.example.json" = "s3_config.json"
}
$precisaEditar = @()
foreach ($modelo in $modelos.Keys) {
    $destinoArq = $modelos[$modelo]
    if ((Test-Path $modelo) -and -not (Test-Path $destinoArq)) {
        Copy-Item $modelo $destinoArq
        $precisaEditar += $destinoArq
        Ok "criado $destinoArq a partir do modelo"
    }
}

# ─── 6. atalho ───────────────────────────────────────────────────────────────
if (-not $SemAtalho) {
    Passo "Criando atalho na area de trabalho"
    $pythonwVenv = "$Destino\venv\Scripts\pythonw.exe"
    $atalho = "$([Environment]::GetFolderPath('Desktop'))\Nexus Upload.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($atalho)
    $lnk.TargetPath = $pythonwVenv
    $lnk.Arguments = "`"$Destino\launcher.py`""
    $lnk.WorkingDirectory = $Destino
    $icone = "$Destino\assets\logo.ico"
    if (Test-Path $icone) { $lnk.IconLocation = $icone }
    $lnk.Description = "Nexus Upload (atualiza pelo Git ao abrir)"
    $lnk.Save()
    Ok "atalho criado: $atalho"
}

# ─── fim ─────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "==============================================" -ForegroundColor Green
Write-Host "  Instalado em: $Destino" -ForegroundColor Green
Write-Host "==============================================" -ForegroundColor Green

if ($precisaEditar.Count -gt 0) {
    Write-Host ""
    Aviso "Antes de usar, preencha suas credenciais em:"
    foreach ($arq in $precisaEditar) { Write-Host "     $Destino\$arq" -ForegroundColor Yellow }
}

Write-Host ""
Write-Host "Abrir pelo atalho 'Nexus Upload'. A cada abertura ele puxa do Git sozinho."
Write-Host "Pra ver o que a atualizacao fez:" -ForegroundColor DarkGray
Write-Host "   $Destino\venv\Scripts\python.exe $Destino\launcher.py --console" -ForegroundColor DarkGray
