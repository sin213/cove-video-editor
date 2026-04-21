; Inno Setup script for Cove Video Editor (Windows)
; Invoked from build.ps1 via:
;   iscc /DAppVersion=X.Y.Z /DSourceDir=<abs dist\cove-video-editor> \
;        /DOutputDir=<abs release> /DIconFile=<abs cove_icon.ico> installer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\cove-video-editor"
#endif
#ifndef OutputDir
  #define OutputDir "..\release"
#endif
#ifndef IconFile
  #define IconFile "..\cove_icon.ico"
#endif

[Setup]
AppId={{B14D2A8C-71A6-4A9E-B7C6-2F1E4E77D3AA}
AppName=Cove Video Editor
AppVersion={#AppVersion}
AppPublisher=Cove
AppPublisherURL=https://github.com/Sin213/cove-video-editor
AppSupportURL=https://github.com/Sin213/cove-video-editor/issues
AppUpdatesURL=https://github.com/Sin213/cove-video-editor/releases
DefaultDirName={autopf}\Cove Video Editor
DefaultGroupName=Cove Video Editor
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\cove-video-editor.exe
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=cove-video-editor-{#AppVersion}-Setup
SetupIconFile={#IconFile}
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Cove Video Editor"; Filename: "{app}\cove-video-editor.exe"
Name: "{group}\Uninstall Cove Video Editor"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Cove Video Editor"; Filename: "{app}\cove-video-editor.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\cove-video-editor.exe"; Description: "Launch Cove Video Editor"; Flags: nowait postinstall skipifsilent
