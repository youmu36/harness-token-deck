Unicode true
ManifestDPIAware true
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "nsDialogs.nsh"
!include "x64.nsh"

!ifndef HTU_VERSION
  !error "HTU_VERSION must be supplied by build_release.py"
!endif
!define PRODUCT "Harness Token Usage"
!define KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\HarnessTokenUsage"
!define WVKEY "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
Name "${PRODUCT}"
OutFile "..\release\Harness Token Usage_${HTU_VERSION}_x64-setup.exe"
InstallDir "$LOCALAPPDATA\Programs\Harness Token Usage"
InstallDirRegKey HKCU "${KEY}" "InstallLocation"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Icon "app.ico"
UninstallIcon "app.ico"
VIProductVersion "${HTU_VERSION}.0"
VIAddVersionKey /LANG=1033 "ProductName" "${PRODUCT}"
VIAddVersionKey /LANG=1033 "FileDescription" "${PRODUCT} Setup"
VIAddVersionKey /LANG=1033 "FileVersion" "${HTU_VERSION}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${HTU_VERSION}"
VIAddVersionKey /LANG=1033 "LegalCopyright" "Harness Token Usage contributors"

Var RegistryKey
Var StartupKey
Var DataDir
Var MenuDir
Var DesktopDir
Var TestRoot
Var PreviousDir
Var ReplaceRadio
Var UninstallFirst
Var DeleteCheck
Var DeleteRequested
Var BackupCheck
Var BackupRequested
Var BackupPath
Var RuntimeVersion

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
Page custom ReplacePage ReplaceLeave
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE CheckDirectory
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\HarnessTokenUsage.exe"
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_TEXT "$(DesktopShortcut)"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION CreateDesktop
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_WELCOME
UninstPage custom un.DataPage un.DataLeave
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"
!include /CHARSET=UTF8 "lang\SimpChinese.nsh"
!include /CHARSET=UTF8 "lang\English.nsh"

; Acceptance redirects registry, shortcuts and data together, with a required marker.
!macro InitPaths PREFIX
Function ${PREFIX}InitPaths
  SetShellVarContext current
  SetRegView 64
  StrCpy $RegistryKey "${KEY}"
  StrCpy $StartupKey "Software\Microsoft\Windows\CurrentVersion\Run"
  StrCpy $DataDir "$LOCALAPPDATA\HarnessTokenUsage"
  StrCpy $MenuDir "$SMPROGRAMS"
  StrCpy $DesktopDir "$DESKTOP"
  ReadEnvStr $TestRoot HTU_INSTALLER_TEST_ROOT
  ${If} $TestRoot != ""
    IfSilent +2
      Abort
    IfFileExists "$TestRoot\.htu-installer-test" +2
      Abort
    StrCpy $RegistryKey "${KEY}-Acceptance"
    StrCpy $StartupKey "Software\HarnessTokenUsage-Acceptance\Run"
    StrCpy $DataDir "$TestRoot\data"
    StrCpy $MenuDir "$TestRoot\start-menu"
    StrCpy $DesktopDir "$TestRoot\desktop"
  ${EndIf}
  System::Call 'kernel32::SetEnvironmentVariableW(w "HTU_DATA_DIR", w "$DataDir")'
FunctionEnd
!macroend
!insertmacro InitPaths ""
!insertmacro InitPaths "un."

Function .onInit
  ${IfNot} ${RunningX64}
    SetErrorLevel 1
    Abort
  ${EndIf}
  Call InitPaths
  StrCpy $LANGUAGE ${LANG_SIMPCHINESE}
  !insertmacro MUI_LANGDLL_DISPLAY
  ReadRegStr $PreviousDir HKCU "$RegistryKey" "InstallLocation"
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/UNINSTALLFIRST" $1
  ${IfNot} ${Errors}
    StrCpy $UninstallFirst 1
  ${EndIf}
  ${If} $TestRoot == ""
    ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Harness Token Usage" "DisplayName"
    ${If} $0 != ""
      MessageBox MB_OK|MB_ICONINFORMATION "$(LegacyNotice)" /SD IDOK
    ${EndIf}
  ${EndIf}
FunctionEnd

Function ReplacePage
  ${If} $PreviousDir == ""
    Abort
  ${EndIf}
  !insertmacro MUI_HEADER_TEXT "$(ReplaceTitle)" "$(ReplacePrompt)"
  nsDialogs::Create 1018
  Pop $0
  ${NSD_CreateRadioButton} 0 15u 100% 25u "$(ReplaceKeep)"
  Pop $0
  ${NSD_Check} $0
  ${NSD_CreateRadioButton} 0 45u 100% 25u "$(ReplaceRemove)"
  Pop $ReplaceRadio
  nsDialogs::Show
FunctionEnd

Function ReplaceLeave
  ${NSD_GetState} $ReplaceRadio $UninstallFirst
FunctionEnd

Function CheckDirectory
  GetFullPathName $0 "$INSTDIR"
  ${GetRoot} "$0" $1
  ${If} $0 == "$1\"
  ${OrIf} $0 == "$WINDIR"
  ${OrIf} $0 == "$LOCALAPPDATA"
  ${OrIf} $0 == "$DataDir"
    MessageBox MB_OK|MB_ICONSTOP "$(InvalidDirectory)" /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
  ${If} $PreviousDir != ""
  ${AndIf} $PreviousDir != "$INSTDIR"
    StrCpy $UninstallFirst 1
  ${EndIf}
FunctionEnd

Function CheckWebView2
  StrCpy $RuntimeVersion ""
  SetRegView 32
  ReadRegStr $RuntimeVersion HKCU "${WVKEY}" "pv"
  ${If} $RuntimeVersion == ""
    ReadRegStr $RuntimeVersion HKLM "${WVKEY}" "pv"
  ${EndIf}
  ${If} $RuntimeVersion == ""
    SetRegView 64
    ReadRegStr $RuntimeVersion HKCU "${WVKEY}" "pv"
    ${If} $RuntimeVersion == ""
      ReadRegStr $RuntimeVersion HKLM "${WVKEY}" "pv"
    ${EndIf}
  ${EndIf}
  SetRegView 64
  ${If} $RuntimeVersion == "0.0.0.0"
    StrCpy $RuntimeVersion ""
  ${EndIf}
FunctionEnd

Function CreateDesktop
  CreateDirectory "$DesktopDir"
  CreateShortCut "$DesktopDir\${PRODUCT}.lnk" "$INSTDIR\HarnessTokenUsage.exe"
  ExecWait '"$INSTDIR\HarnessTokenUsage.exe" --set-shortcut-app-id "$DesktopDir\${PRODUCT}.lnk"' $0
FunctionEnd

Section "Install"
  Call CheckDirectory
  Call CheckWebView2
  ${If} $RuntimeVersion == ""
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File "bootstrap_webview2.ps1"
    ExecWait '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "$PLUGINSDIR\bootstrap_webview2.ps1"' $0
    Call CheckWebView2
    ${If} $0 != 0
    ${OrIf} $RuntimeVersion == ""
      MessageBox MB_OK|MB_ICONSTOP "$(RuntimeError)" /SD IDOK
      SetErrorLevel 1
      Abort
    ${EndIf}
  ${EndIf}
  InitPluginsDir
  SetOutPath "$PLUGINSDIR\payload"
  File /r "..\dist\HarnessTokenUsage\*.*"
  ExecWait '"$PLUGINSDIR\payload\HarnessTokenUsage.exe" --quit --wait-for-exit' $0
  ${If} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "$(RunningError)" /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
  ${If} $UninstallFirst == 1
  ${AndIf} $PreviousDir != ""
    ExecWait '"$PreviousDir\Uninstall.exe" /S _?=$PreviousDir' $0
    ${If} $0 != 0
      SetErrorLevel 1
      Abort
    ${EndIf}
    Delete "$PreviousDir\Uninstall.exe"
    RMDir "$PreviousDir"
  ${EndIf}
  CreateDirectory "$INSTDIR"
  SetOutPath "$INSTDIR"
  ClearErrors
  File /r "..\dist\HarnessTokenUsage\*.*"
  ${If} ${Errors}
    SetErrorLevel 1
    Abort
  ${EndIf}
  SetOutPath "$INSTDIR"
  CopyFiles /SILENT "$INSTDIR\_internal\THIRD_PARTY_NOTICES.md" "$INSTDIR"
  CopyFiles /SILENT "$INSTDIR\_internal\LICENSE.pending" "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteINIStr "$INSTDIR\installer-state.ini" "install" "testRoot" "$TestRoot"
  WriteINIStr "$INSTDIR\installer-state.ini" "install" "language" "$LANGUAGE"
  WriteINIStr "$INSTDIR\installer-state.ini" "install" "dataDir" "$DataDir"
  WriteRegStr HKCU "$RegistryKey" "DisplayName" "${PRODUCT}"
  WriteRegStr HKCU "$RegistryKey" "DisplayVersion" "${HTU_VERSION}"
  WriteRegStr HKCU "$RegistryKey" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "$RegistryKey" "DisplayIcon" "$INSTDIR\HarnessTokenUsage.exe"
  WriteRegStr HKCU "$RegistryKey" "UninstallString" '$\"$INSTDIR\Uninstall.exe$\"'
  WriteRegStr HKCU "$RegistryKey" "QuietUninstallString" '$\"$INSTDIR\Uninstall.exe$\" /S'
  WriteRegDWORD HKCU "$RegistryKey" "NoModify" 1
  WriteRegDWORD HKCU "$RegistryKey" "NoRepair" 1
  CreateDirectory "$MenuDir"
  CreateShortCut "$MenuDir\${PRODUCT}.lnk" "$INSTDIR\HarnessTokenUsage.exe"
  ExecWait '"$INSTDIR\HarnessTokenUsage.exe" --set-shortcut-app-id "$MenuDir\${PRODUCT}.lnk"' $0
  ${If} $0 != 0
    SetErrorLevel 1
    Abort
  ${EndIf}
SectionEnd

Function un.onInit
  Call un.InitPaths
  ReadINIStr $0 "$INSTDIR\installer-state.ini" "install" "testRoot"
  ${If} $0 != $TestRoot
    SetErrorLevel 1
    Abort
  ${EndIf}
  ReadINIStr $LANGUAGE "$INSTDIR\installer-state.ini" "install" "language"
  StrCpy $DeleteRequested 0
  StrCpy $BackupRequested 0
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/DELETEDATA" $1
  ${IfNot} ${Errors}
    StrCpy $DeleteRequested 1
    StrCpy $BackupRequested 1
  ${EndIf}
  ClearErrors
  ${GetOptions} $0 "/BACKUPDATA" $1
  ${IfNot} ${Errors}
    StrCpy $BackupRequested 1
  ${EndIf}
  ClearErrors
  ${GetOptions} $0 "/NOBACKUP" $1
  ${IfNot} ${Errors}
    StrCpy $BackupRequested 0
  ${EndIf}
FunctionEnd

Function un.DataPage
  !insertmacro MUI_HEADER_TEXT "$(DataTitle)" "$(DataPrompt)"
  nsDialogs::Create 1018
  Pop $0
  ${NSD_CreateCheckbox} 0 15u 100% 30u "$(DeleteData)"
  Pop $DeleteCheck
  ${NSD_CreateCheckbox} 0 50u 100% 30u "$(BackupData)"
  Pop $BackupCheck
  ${NSD_Check} $BackupCheck
  nsDialogs::Show
FunctionEnd

Function un.DataLeave
  ${NSD_GetState} $DeleteCheck $DeleteRequested
  ${NSD_GetState} $BackupCheck $BackupRequested
  ${If} $DeleteRequested == 1
    MessageBox MB_YESNO|MB_ICONEXCLAMATION|MB_DEFBUTTON2 "$(ConfirmDelete)" IDYES +2
      Abort
  ${EndIf}
FunctionEnd

Section "Uninstall"
  ExecWait '"$INSTDIR\HarnessTokenUsage.exe" --quit --wait-for-exit' $0
  ${If} $0 != 0
    MessageBox MB_OK|MB_ICONSTOP "$(RunningError)" /SD IDOK
    SetErrorLevel 1
    Abort
  ${EndIf}
  ${If} $BackupRequested == 1
    IfFileExists "$DataDir\htu.sqlite3" 0 no_database
    CreateDirectory "$DesktopDir"
    ${GetTime} "" "L" $0 $1 $2 $3 $4 $5 $6
    StrCpy $BackupPath "$DesktopDir\htu-$2$1$0-$4$5$6.sqlite3"
    StrCpy $7 1
backup_name:
    IfFileExists "$BackupPath" 0 backup_ready
    IntOp $7 $7 + 1
    StrCpy $BackupPath "$DesktopDir\htu-$2$1$0-$4$5$6-$7.sqlite3"
    Goto backup_name
backup_ready:
    ExecWait '"$INSTDIR\HarnessTokenUsage.exe" --backup-data "$BackupPath"' $0
    ${If} $0 != 0
      Goto backup_failed
    ${EndIf}
no_database:
  ${EndIf}
  ; Release the install directory as the current output directory before removal.
  SetOutPath "$TEMP"
  ${If} $DeleteRequested == 1
    ClearErrors
    RMDir /r "$DataDir"
    ${If} ${Errors}
      Goto backup_failed
    ${EndIf}
  ${EndIf}
  Delete "$MenuDir\${PRODUCT}.lnk"
  Delete "$DesktopDir\${PRODUCT}.lnk"
  DeleteRegKey HKCU "$RegistryKey"
  DeleteRegValue HKCU "$StartupKey" "HarnessTokenUsage"
  ; Delete only owned payload files, leaving unrelated files in a custom directory.
  !include "..\build\uninstall_files.nsh"
  ; These application-owned trees can contain caches or files from older builds.
  RMDir /r "$INSTDIR\_internal"
  RMDir /r "$INSTDIR\HarnessTokenUsage.exe.WebView2"
  Delete "$INSTDIR\THIRD_PARTY_NOTICES.md"
  Delete "$INSTDIR\LICENSE.pending"
  Delete "$INSTDIR\installer-state.ini"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  ${If} $DeleteRequested == 1
    IfFileExists "$DataDir\*.*" 0 +3
      SetErrorLevel 1
      Abort
  ${EndIf}
  Goto done
backup_failed:
  MessageBox MB_OK|MB_ICONSTOP "$(BackupError)" /SD IDOK
  SetErrorLevel 1
  Abort
done:
SectionEnd
