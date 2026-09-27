#define MyAppName "Presencialidad"
#define MyAppVersion "5.3.0"
#define MyAppExeName "Presencialidad.exe"

[Setup]
AppId={{BB904B37-E6D6-4D12-9C06-05E6A552A520}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\Presencialidad
DefaultGroupName=Presencialidad
OutputDir=installer-output
OutputBaseFilename=Presencialidad-5.3.0-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Presencialidad"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\Presencialidad"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Crear acceso directo en el escritorio"; GroupDescription: "Accesos directos:"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Abrir Presencialidad"; Flags: nowait postinstall skipifsilent
