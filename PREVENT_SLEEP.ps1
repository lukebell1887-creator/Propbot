# ============================================================================
# PREVENT SLEEP — Keep Windows awake while the bot is running
# ============================================================================
# Run this BEFORE starting the bot. It does 3 things:
#   1. Sets power plan to "High Performance" (no sleep/hibernate)
#   2. Disables screen timeout (prevents lock-triggered sleep)
#   3. Runs a keepalive loop that prevents the OS from sleeping
#
# To stop: Ctrl+C (or close the window)
# ============================================================================

Write-Host "=" * 70
Write-Host "  ANTI-SLEEP: Preventing Windows from sleeping"
Write-Host "=" * 70

# --- 1. Disable sleep and hibernate via powercfg ---
Write-Host "`n  [1/3] Setting power plan..."
try {
    # Set High Performance power plan
    $highPerf = powercfg /list | Select-String "High performance" | ForEach-Object {
        ($_ -split '\s+')[3]
    }
    if ($highPerf) {
        powercfg /setactive $highPerf
        Write-Host "    [OK] Activated High Performance power plan"
    } else {
        Write-Host "    [WARN] No High Performance plan found, configuring current plan"
    }
    
    # Disable sleep on AC power (0 = never)
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
    powercfg /change monitor-timeout-ac 0
    Write-Host "    [OK] Sleep=NEVER, Hibernate=NEVER, Monitor=NEVER (AC power)"
} catch {
    Write-Host "    [WARN] Could not change power settings (may need admin): $_"
}

# --- 2. Disable automatic screen lock ---
Write-Host "`n  [2/3] Disabling screen lock timeout..."
try {
    # Disable lock screen timeout
    powercfg /change standby-timeout-dc 0
    Write-Host "    [OK] Battery sleep also disabled (if laptop)"
} catch {
    Write-Host "    [WARN] Could not change DC settings: $_"
}

# --- 3. Keepalive loop using SetThreadExecutionState ---
Write-Host "`n  [3/3] Starting keepalive loop (press Ctrl+C to stop)..."
Write-Host "    This prevents Windows from sleeping even if power settings reset."
Write-Host ""

# Use .NET interop to call SetThreadExecutionState
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class SleepPreventer {
    [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    public static extern uint SetThreadExecutionState(uint esFlags);
    
    // ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    public const uint PREVENT_SLEEP = 0x80000003;
    public const uint ALLOW_SLEEP = 0x80000000;
    
    public static void KeepAwake() {
        SetThreadExecutionState(PREVENT_SLEEP);
    }
    
    public static void AllowSleep() {
        SetThreadExecutionState(ALLOW_SLEEP);
    }
}
"@

try {
    $count = 0
    while ($true) {
        [SleepPreventer]::KeepAwake()
        $count++
        if ($count % 60 -eq 0) {
            $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
            Write-Host "  [$ts] Keepalive ping #$count — Windows will NOT sleep"
        }
        Start-Sleep -Seconds 30
    }
} finally {
    [SleepPreventer]::AllowSleep()
    Write-Host "`n  Anti-sleep disabled. Windows can sleep again."
}
