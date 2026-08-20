!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"

!ifndef PROJECT_ROOT
  !error "PROJECT_ROOT must point to the repository root"
!endif

!ifdef NSIS_WIN32_MAKENSIS
  !define PROJECT_SEP "\"
!else
  !define PROJECT_SEP "/"
!endif

Unicode true
RequestExecutionLevel user
Name "CWS Codex Windows 员工端"
OutFile "${PROJECT_ROOT}${PROJECT_SEP}dist${PROJECT_SEP}CWS-Codex-Setup-v0.1.4.exe"
InstallDir "$LOCALAPPDATA\CWS Codex"
InstallDirRegKey HKCU "Software\CWS Codex" "InstallDir"
Icon "${PROJECT_ROOT}${PROJECT_SEP}windows-client${PROJECT_SEP}CwsCodex.ico"
UninstallIcon "${PROJECT_ROOT}${PROJECT_SEP}windows-client${PROJECT_SEP}CwsCodex.ico"
BrandingText "CWS Codex"
ShowInstDetails show
ShowUninstDetails show

VIProductVersion "0.1.4.0"
VIAddVersionKey /LANG=2052 "ProductName" "CWS Codex Windows 员工端"
VIAddVersionKey /LANG=2052 "CompanyName" "CWS"
VIAddVersionKey /LANG=2052 "LegalCopyright" "Copyright CWS"
VIAddVersionKey /LANG=2052 "FileDescription" "公司 Codex VS Code 员工端安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "0.1.4"
VIAddVersionKey /LANG=2052 "ProductVersion" "0.1.4"

!define MUI_ABORTWARNING
!define MUI_ICON "${PROJECT_ROOT}${PROJECT_SEP}windows-client${PROJECT_SEP}CwsCodex.ico"
!define MUI_UNICON "${PROJECT_ROOT}${PROJECT_SEP}windows-client${PROJECT_SEP}CwsCodex.ico"
!define MUI_FINISHPAGE_NOAUTOCLOSE
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

Var InstallResult
Var AutoUpdateArgument

Section "安装 CWS Codex" MainSection
  SectionIn RO
  InitPluginsDir
  SetOutPath "$PLUGINSDIR\windows-client"
  File /r "${PROJECT_ROOT}${PROJECT_SEP}windows-client${PROJECT_SEP}*.*"

  StrCpy $AutoUpdateArgument ""
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/AUTOUPDATE" $R1
  ${IfNot} ${Errors}
    StrCpy $AutoUpdateArgument "-AutoUpdate"
  ${EndIf}
  ExecWait '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "$PLUGINSDIR\windows-client\Install-CwsCodex.ps1" -InstallDir "$INSTDIR" $AutoUpdateArgument' $InstallResult
  ${If} $InstallResult != 0
    MessageBox MB_ICONSTOP|MB_OK "员工端配置失败，错误码：$InstallResult。详细原因已写入 $TEMP\CWS-Codex-Install.log。"
    Abort
  ${EndIf}

  SetOutPath "$INSTDIR"
  WriteUninstaller "$INSTDIR\卸载公司Codex.exe"
  WriteRegStr HKCU "Software\CWS Codex" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CWS Codex" "DisplayName" "CWS Codex Windows 员工端"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CWS Codex" "DisplayVersion" "0.1.4"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CWS Codex" "Publisher" "CWS"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CWS Codex" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CWS Codex" "DisplayIcon" "$INSTDIR\CwsCodex.ico"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CWS Codex" "UninstallString" '"$INSTDIR\卸载公司Codex.exe"'
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CWS Codex" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CWS Codex" "NoRepair" 1
SectionEnd

Section "Uninstall"
  IfFileExists "$INSTDIR\Uninstall-CwsCodex.ps1" 0 SkipPowerShellUninstall
  ExecWait '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\Uninstall-CwsCodex.ps1" -InstallDir "$INSTDIR"'
SkipPowerShellUninstall:
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\CWS Codex"
  DeleteRegKey HKCU "Software\CWS Codex"
  RMDir /r "$INSTDIR"
SectionEnd
