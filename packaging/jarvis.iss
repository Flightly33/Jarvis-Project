; Inno Setup script: wraps the built application folder into a single installer.
[Setup]
AppName=J.A.R.V.I.S.
AppVersion=0.1.0
AppPublisher=Jarvis Project
DefaultDirName={localappdata}\Programs\Jarvis
DefaultGroupName=Jarvis
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=JarvisSetup
Compression=lzma2
SolidCompression=yes
DisableProgramGroupPage=yes
SetupIconFile=jarvis.ico
UninstallDisplayIcon={app}\Jarvis.exe

[Files]
Source: "..\dist\Jarvis\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Jarvis"; Filename: "{app}\Jarvis.exe"
Name: "{userdesktop}\Jarvis"; Filename: "{app}\Jarvis.exe"

[Run]
Filename: "{app}\Jarvis.exe"; Description: "Start Jarvis"; Flags: nowait postinstall skipifsilent
