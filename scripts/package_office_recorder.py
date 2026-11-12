"""Build the office-only bundle without server secrets or databases."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / 'dist' / 'DocTutorialsOfficeRecorder-macOS.zip'
target.parent.mkdir(exist_ok=True)
files = [ROOT / 'Start Office Recorder.command', ROOT / 'requirements-office.txt', ROOT / 'RECORDING_SETUP.md']
files += sorted((ROOT / 'office_recorder').glob('*.py'))
files += [ROOT / 'office_recorder' / 'office.html']
with ZipFile(target, 'w', ZIP_DEFLATED) as archive:
    for file in files:
        archive.write(file, Path('DocTutorialsOfficeRecorder') / file.relative_to(ROOT))
print(target)

# Office desktop recorder (joins the webinar and records it; several classes at once).
desktop = ROOT / 'dist' / 'DocTutorialsOfficeDesktopRecorder.zip'
names = ['package.json', 'package-lock.json', 'main.js', 'capture-preload.js', 'ui-preload.js',
         'capture/capture.html', 'capture/capture.js', 'ui/index.html', 'ui/ui.js']
with ZipFile(desktop, 'w', ZIP_DEFLATED) as archive:
    archive.write(ROOT / 'Start Office Desktop Recorder.command', 'DocTutorialsOfficeDesktopRecorder/Start Office Desktop Recorder.command')
    for name in names:
        archive.write(ROOT / 'office_desktop' / name, Path('DocTutorialsOfficeDesktopRecorder') / 'office_desktop' / name)
print(desktop)
