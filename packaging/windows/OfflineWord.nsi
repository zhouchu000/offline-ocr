; OfflineWord Windows 10/11 x64 per-user installer.
; PAYLOAD_FILES, OUTPUT_FILE, APP_VERSION, APP_VERSION_NUM and PAYLOAD_KB
; are supplied by tools/build_windows_installer.py.

Unicode True
ManifestSupportedOS Win10
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetDatablockOptimize on
SetDateSave on
SetOverwrite on
CRCCheck on

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "WinVer.nsh"
!include "x64.nsh"

!ifndef PAYLOAD_FILES
  !error "PAYLOAD_FILES is required"
!endif
!ifndef APP_ICON
  !error "APP_ICON is required"
!endif
!ifndef OUTPUT_FILE
  !error "OUTPUT_FILE is required"
!endif
!ifndef APP_VERSION
  !error "APP_VERSION is required"
!endif
!ifndef APP_VERSION_NUM
  !error "APP_VERSION_NUM is required"
!endif
!ifndef PAYLOAD_KB
  !error "PAYLOAD_KB is required"
!endif

!define APP_NAME "离线图片转Word"
!define APP_PUBLISHER "OfflineWord"
!define APP_REG_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\OfflineWord"
!define UNINSTALLER "$LOCALAPPDATA\OfflineWord-Uninstaller\uninstall.exe"

Var LegacyInstall

!macro SetupStatus TEXT
  !ifdef FLUENT_BACKEND
    FileOpen $8 "$EXEDIR\setup-status.txt" w
    FileWriteUTF16LE $8 "${TEXT}"
    FileClose $8
  !endif
!macroend

Name "${APP_NAME} ${APP_VERSION}"
Caption "${APP_NAME} ${APP_VERSION} 安装"
OutFile "${OUTPUT_FILE}"
; Paddle/OpenCV native Windows loaders are not reliable with non-ASCII model
; paths. Keep the physical application and model directory ASCII-only while
; retaining Chinese product names and shortcuts in the user interface.
InstallDir "$LOCALAPPDATA\Programs\OfflineWord"
BrandingText "离线图片转 Word · 本机识别"
ShowInstDetails show
ShowUninstDetails show

VIProductVersion "${APP_VERSION_NUM}"
VIAddVersionKey /LANG=2052 "ProductName" "${APP_NAME}"
VIAddVersionKey /LANG=2052 "ProductVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "FileDescription" "离线图片文字识别与 Word 导出安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "${APP_VERSION}"
VIAddVersionKey /LANG=2052 "LegalCopyright" "第三方许可见安装目录"

!define MUI_ABORTWARNING
!define MUI_ICON "${APP_ICON}"
!define MUI_UNICON "${APP_ICON}"
!define MUI_FINISHPAGE_RUN "$INSTDIR\runtime\pythonw.exe"
!define MUI_FINISHPAGE_RUN_PARAMETERS '-B $\"$INSTDIR\launch.pyw$\"'
!define MUI_FINISHPAGE_RUN_TEXT "立即启动离线图片转 Word"
!define MUI_FINISHPAGE_LINK "打开使用说明"
!define MUI_FINISHPAGE_LINK_LOCATION "$INSTDIR\docs\使用说明.md"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

Function .onInit
  ; /D= can override InstallDir. Reset it before any files are written so
  ; Paddle/OpenCV always sees the fixed ASCII model path.
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\OfflineWord"
  StrCpy $LegacyInstall "0"
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP|MB_OK "仅支持 Windows 10/11 64 位系统。"
    Abort
  ${EndIf}
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP|MB_OK "仅支持 Windows 10/11 64 位系统。"
    Abort
  ${EndIf}
  IfFileExists "$LOCALAPPDATA\Programs\离线图片转Word\*.*" legacy_found 0
  IfFileExists "$LOCALAPPDATA\Programs\离线图片转Word-卸载程序.exe" legacy_found 0
  IfFileExists "$LOCALAPPDATA\Programs\OfflineWord-Uninstall.exe" legacy_found 0
  ReadRegStr $0 HKCU "${APP_REG_KEY}" "InstallLocation"
  StrCmp $0 "$LOCALAPPDATA\Programs\离线图片转Word" legacy_found legacy_done
  legacy_found:
    StrCpy $LegacyInstall "1"
    IfSilent +2
    MessageBox MB_ICONINFORMATION|MB_OK "检测到安装在中文目录中的旧版。本安装程序将安全移除旧版程序文件并升级到英文物理目录；您的原始图片和已导出的 Word 不会被删除。请勿另行运行旧版卸载程序。"
  legacy_done:
  SetShellVarContext current
FunctionEnd

Section "主程序（必需）" MainSection
  SectionIn RO
  SetShellVarContext current
  ; Enforce the fixed path again at the mutation boundary.
  StrCpy $INSTDIR "$LOCALAPPDATA\Programs\OfflineWord"
  ${If} $LegacyInstall == "1"
    Delete "$LOCALAPPDATA\Programs\离线图片转Word-卸载程序.exe"
    Delete "$LOCALAPPDATA\Programs\OfflineWord-Uninstall.exe"
    RMDir /r "$LOCALAPPDATA\Programs\离线图片转Word"
  ${EndIf}
  ; 0.2.6 briefly placed its uninstaller inside the application payload.
  ; Remove only that exact obsolete file so package verification stays clean.
  Delete "$INSTDIR\uninstall.exe"
  !insertmacro SetupStatus "extracting"
  SetOutPath "$INSTDIR"
  ClearErrors
  File /r "${PAYLOAD_FILES}"
  IfErrors install_failed
  !insertmacro SetupStatus "registering"
  ClearErrors

  CreateDirectory "$SMPROGRAMS\离线图片转Word"
  CreateShortCut "$SMPROGRAMS\离线图片转Word\离线图片转Word.lnk" \
    "$INSTDIR\runtime\pythonw.exe" '-B "$INSTDIR\launch.pyw"' \
    "$INSTDIR\app\offline_word\assets\app.ico" 0 SW_SHOWNORMAL "" "离线图片转 Word"
  CreateShortCut "$SMPROGRAMS\离线图片转Word\检查环境.lnk" \
    "$INSTDIR\检查环境.cmd" "" "$INSTDIR\检查环境.cmd"
  CreateShortCut "$SMPROGRAMS\离线图片转Word\核对安装文件.lnk" \
    "$INSTDIR\核对文件.cmd" "" "$INSTDIR\核对文件.cmd"
  CreateShortCut "$DESKTOP\离线图片转Word.lnk" \
    "$INSTDIR\runtime\pythonw.exe" '-B "$INSTDIR\launch.pyw"' \
    "$INSTDIR\app\offline_word\assets\app.ico" 0 SW_SHOWNORMAL "" "离线图片转 Word"

  CreateDirectory "$LOCALAPPDATA\OfflineWord-Uninstaller"
  WriteUninstaller "${UNINSTALLER}"
  CreateShortCut "$SMPROGRAMS\离线图片转Word\卸载.lnk" \
    "${UNINSTALLER}" "" "${UNINSTALLER}"

  WriteRegStr HKCU "${APP_REG_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${APP_REG_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${APP_REG_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${APP_REG_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${APP_REG_KEY}" "DisplayIcon" "$INSTDIR\app\offline_word\assets\app.ico"
  WriteRegStr HKCU "${APP_REG_KEY}" "UninstallString" '"${UNINSTALLER}"'
  WriteRegStr HKCU "${APP_REG_KEY}" "QuietUninstallString" '"${UNINSTALLER}" /S'
  WriteRegDWORD HKCU "${APP_REG_KEY}" "EstimatedSize" ${PAYLOAD_KB}
  WriteRegDWORD HKCU "${APP_REG_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${APP_REG_KEY}" "NoRepair" 1
  IfErrors install_failed
  !insertmacro SetupStatus "complete"
  Goto install_done
  install_failed:
    !insertmacro SetupStatus "failed"
    SetErrorLevel 1
    Abort
  install_done:
SectionEnd

Function un.onInit
  SetShellVarContext current
  ; NSIS executes uninstallers from a temporary copy while preserving the
  ; original directory in $INSTDIR. Validate that origin, then force the only
  ; recursive deletion target.
  StrCmp $INSTDIR "$LOCALAPPDATA\OfflineWord-Uninstaller" uninstaller_location_ok
    MessageBox MB_ICONSTOP|MB_OK "卸载程序位置无效，已停止卸载。请从 Windows“已安装的应用”中重试。"
    Abort
  uninstaller_location_ok:
    StrCpy $INSTDIR "$LOCALAPPDATA\Programs\OfflineWord"
FunctionEnd

Section "Uninstall"
  SetShellVarContext current
  Delete "$DESKTOP\离线图片转Word.lnk"
  RMDir /r "$SMPROGRAMS\离线图片转Word"
  Delete "${UNINSTALLER}"
  RMDir "$LOCALAPPDATA\OfflineWord-Uninstaller"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${APP_REG_KEY}"
SectionEnd
