param(
  [Parameter(Mandatory=$true)][string]$Out,
  [ValidateSet("full","region","window")][string]$Mode = "full",
  [int]$X = 0,
  [int]$Y = 0,
  [int]$Width = 0,
  [int]$Height = 0
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
public class NativeDpi {
  [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
}
"@
[NativeDpi]::SetProcessDPIAware() | Out-Null
if ($Mode -eq "window") {
  $h = [NativeDpi]::GetForegroundWindow()
  $r = New-Object RECT
  [NativeDpi]::GetWindowRect($h, [ref]$r) | Out-Null
  $X = $r.Left; $Y = $r.Top; $Width = $r.Right - $r.Left; $Height = $r.Bottom - $r.Top
} elseif ($Mode -eq "full") {
  $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
  $X = $b.X; $Y = $b.Y; $Width = $b.Width; $Height = $b.Height
}
if ($Width -le 0 -or $Height -le 0) { throw "empty capture rect" }
$bmp = New-Object System.Drawing.Bitmap $Width, $Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($X, $Y, 0, 0, $bmp.Size)
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose()
$bmp.Dispose()
Write-Output ("{0}x{1}" -f $Width, $Height)