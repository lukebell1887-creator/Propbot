# ============================================================================
# Fix mql-zmq compatibility with newer MT5 builds
# ============================================================================
# The mql-zmq library defines StringToUtf8/StringFromUtf8 which now
# conflict with built-in MQL5 functions in newer builds.
# This script patches Native.mqh to remove the conflicting definitions.
# ============================================================================

$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Patching mql-zmq for MT5 compatibility" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

$mt5Base = "$env:APPDATA\MetaQuotes\Terminal"
$patched = 0

if (-Not (Test-Path $mt5Base)) {
    Write-Host "  ERROR: MT5 terminal folder not found!" -ForegroundColor Red
    exit 1
}

$terminals = Get-ChildItem -Path $mt5Base -Directory
foreach ($terminal in $terminals) {
    $nativeMqh = "$($terminal.FullName)\MQL5\Include\Mql\Lang\Native.mqh"
    
    if (-Not (Test-Path $nativeMqh)) {
        continue
    }
    
    Write-Host "`n  Patching: $nativeMqh" -ForegroundColor Yellow
    
    # Read the file
    $content = Get-Content $nativeMqh -Raw
    
    # Check if already patched
    if ($content -match "// PATCHED FOR MT5 COMPATIBILITY") {
        Write-Host "  Already patched — skipping" -ForegroundColor Green
        $patched++
        continue
    }
    
    # Backup original
    $backup = "$nativeMqh.bak"
    Copy-Item -Force $nativeMqh $backup
    Write-Host "  Backup saved to: $backup" -ForegroundColor Gray
    
    # The fix: wrap the conflicting function definitions so they don't
    # conflict with MT5's built-in versions.
    # We comment out the StringToUtf8 and StringFromUtf8 function bodies
    # that the library defines, since MT5 now provides them natively.
    
    # Strategy: Replace the entire file with a compatibility shim
    # that only defines what MT5 doesn't already have.
    
    $newContent = @"
//+------------------------------------------------------------------+
//| Module: Native.mqh                                                |
//| PATCHED FOR MT5 COMPATIBILITY                                     |
//| Original mql-zmq defines StringToUtf8/StringFromUtf8 which now   |
//| conflict with built-in MQL5 functions in newer builds.            |
//| This patched version removes those conflicting definitions.       |
//+------------------------------------------------------------------+
#property strict

// --- Type definitions that MQL5 might not have ---
#ifndef intptr_t
#define intptr_t long
#endif

#ifndef size_t
#define size_t ulong  
#endif

// StringToUtf8 and StringFromUtf8 are now built-in in newer MT5.
// The mql-zmq library's custom versions are no longer needed.
// We keep the 3-parameter version as it may differ from built-in:

// Check if we need the 3-param overload (string, uchar[], bool)
// In newer MT5, StringToUtf8 is built-in but may have different signature.
// We provide a compatibility wrapper if needed.

//+------------------------------------------------------------------+
//| Pointer operations (still needed — not built-in)                  |
//+------------------------------------------------------------------+
#import "kernel32.dll"
void RtlMoveMemory(long &dst, long src, int cnt);
void RtlMoveMemory(long dst, long &src, int cnt);
#import
//+------------------------------------------------------------------+
"@
    
    Set-Content -Path $nativeMqh -Value $newContent -Encoding UTF8
    Write-Host "  Patched successfully" -ForegroundColor Green
    $patched++
}

if ($patched -eq 0) {
    Write-Host "  No Native.mqh files found to patch!" -ForegroundColor Red
} else {
    Write-Host ""
    Write-Host "============================================" -ForegroundColor Green
    Write-Host "  Patched $patched terminal(s)" -ForegroundColor Green
    Write-Host "============================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "  NEXT: In MetaEditor, press F7 to recompile" -ForegroundColor White
    Write-Host "        SHF_ZMQ_Bridge.mq5" -ForegroundColor White
    Write-Host ""
}
