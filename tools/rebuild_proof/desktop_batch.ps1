# The desktop batch of the rebuild proof. RUN IT ONLY AFTER THE USER SAYS "go desktop".
#
# It OPENS WINDOWS on this PC and hooks the keyboard and mouse while it records:
#   1. builds Twin Lab's reference CaseDesk into <evidence>\e\ref-bin (the twin-lab checkout is not written to);
#   2. records CaseDesk LIVE with the real recorder (`record --session N`, desktop collector, UI Automation),
#      the clerk being Twin Lab's scripted flow (reference\desktop\CaseDesk\flow.py), into a portal the
#      harness starts (case e);
#   3. the owner's part through /api/v1 and a frozen package from the live recording (owner.py e);
#   4. replays with Twin Lab's desktop verify (labctl.desktop.verify): the reference exe (positive control)
#      and both blind rebuilds (builds\c-casedesk, builds\d-casedesk), against the live recording and the
#      earlier spliced one. Reports: <evidence>\e\replay\desktop-summary.json.
#
# Leave the mouse and keyboard alone while it runs (about 5 minutes): the flow clicks for real, only
# while CaseDesk is in the foreground, and refuses to click into another window.
#
#   powershell -ExecutionPolicy Bypass -File tools\rebuild_proof\desktop_batch.ps1 -GoDesktop

param([switch]$GoDesktop)
$ErrorActionPreference = 'Stop'
if (-not $GoDesktop) {
    Write-Error 'This batch opens windows on this PC. Run it with -GoDesktop, only after the user said "go desktop".'
    exit 2
}
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Resolve-Path (Join-Path $here '..\..')
Set-Location $repo
$env:PYTHONUTF8 = '1'

Write-Host '1/4 building the reference CaseDesk'
python tools\rebuild_proof\desktop_live.py build-reference
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host '2/4 recording CaseDesk live (windows open now; hands off the mouse and keyboard)'
python tools\rebuild_proof\record.py e --go-desktop
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host '3/4 owner step and package from the live recording'
python tools\rebuild_proof\owner.py e
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host '4/4 desktop replays (reference, c-casedesk, d-casedesk)'
python tools\rebuild_proof\desktop_live.py replay
exit $LASTEXITCODE
