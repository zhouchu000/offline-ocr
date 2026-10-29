Unicode True
ManifestSupportedOS Win10
RequestExecutionLevel user
SetCompressor /SOLID lzma
CRCCheck on
ShowInstDetails show
!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "nsDialogs.nsh"
!include "WinVer.nsh"
!include "x64.nsh"
!include "FileFunc.nsh"
Name "离线 OCR ${TARGET_VERSION} 更新"
OutFile "${OUTPUT_FILE}"
InstallDir "$LOCALAPPDATA\Programs\OfflineWord"
BrandingText "离线更新 · 完整校验 · 可恢复"
!define MUI_ICON "${APP_ICON}"
!define MUI_WELCOMEPAGE_TITLE "离线 OCR ${TARGET_VERSION} 更新"
!define MUI_WELCOMEPAGE_TEXT "适用于 ${BASE_VERSION} 完整安装。请先关闭离线 OCR。$\r$\n$\r$\n更新包会核对签名、版本和全部文件，并保留可恢复的原版本备份。$\r$\n$\r$\n原始图片、项目和导出的 Word 不会改动。"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
Page custom SelectAction LeaveAction
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "SimpChinese"
Var ActionPage
Var UpdateRadio
Var RollbackRadio
Var Rollback

Function .onInit
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "仅支持 Windows 10/11 x64。"
    Abort
  ${EndIf}
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "仅支持 Windows 10/11 x64。"
    Abort
  ${EndIf}
  StrCpy $Rollback "0"
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/ROLLBACK" $1
  ${IfNot} ${Errors}
    StrCpy $Rollback "1"
  ${EndIf}
FunctionEnd

Function SelectAction
  !insertmacro MUI_HEADER_TEXT "选择操作" "更新应用，或恢复此更新保留的原版本。"
  nsDialogs::Create 1018
  Pop $ActionPage
  ${NSD_CreateRadioButton} 0 10u 100% 20u "更新至 ${TARGET_VERSION}"
  Pop $UpdateRadio
  ${NSD_CreateRadioButton} 0 40u 100% 20u "恢复更新前的 ${BASE_VERSION}"
  Pop $RollbackRadio
  ${NSD_Check} $UpdateRadio
  nsDialogs::Show
FunctionEnd

Function LeaveAction
  ${NSD_GetState} $RollbackRadio $Rollback
FunctionEnd

Section
  InitPluginsDir
  SetOutPath "$PLUGINSDIR\OfflineUpdater"
  File /r "${SUPPORT_FILES}"
  DetailPrint "正在校验签名、版本和全部文件，并执行所选操作…"
  ${If} $Rollback == "1"
    nsExec::ExecToLog '"$PLUGINSDIR\OfflineUpdater\runtime\python.exe" -I -B "$PLUGINSDIR\OfflineUpdater\update_cli.py" --target "$INSTDIR" --rollback --status-file "$PLUGINSDIR\OfflineUpdater\result.txt"'
  ${Else}
    nsExec::ExecToLog '"$PLUGINSDIR\OfflineUpdater\runtime\python.exe" -I -B "$PLUGINSDIR\OfflineUpdater\update_cli.py" --target "$INSTDIR" --package "$PLUGINSDIR\OfflineUpdater\update.ocrupdate" --status-file "$PLUGINSDIR\OfflineUpdater\result.txt"'
  ${EndIf}
  Pop $0
  ClearErrors
  FileOpen $2 "$PLUGINSDIR\OfflineUpdater\result.txt" r
  IfErrors result_done
  FileReadUTF16LE $2 $3
  FileClose $2
  DetailPrint "$3"
  !ifdef FLUENT_BACKEND
    FileOpen $4 "$EXEDIR\setup-status.txt" w
    FileWriteUTF16LE $4 "$3"
    FileClose $4
  !endif
  result_done:
  ${If} $0 != "0"
    SetErrorLevel 1
    IfSilent +2
    MessageBox MB_ICONSTOP "操作未完成，请查看上方提示。保留备份后可重新运行本程序恢复。"
    Abort
  ${EndIf}
  ReadRegStr $1 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OfflineWord" "InstallLocation"
  ${If} $1 == $INSTDIR
    ${If} $Rollback == "1"
      WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OfflineWord" "DisplayVersion" "${BASE_VERSION}"
    ${Else}
      WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\OfflineWord" "DisplayVersion" "${TARGET_VERSION}"
    ${EndIf}
  ${EndIf}
SectionEnd
