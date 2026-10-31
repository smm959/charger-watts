#!/bin/bash
# Создаёт ChargerWatts.app и иконку. Запуск: ./build_app.sh
set -euo pipefail
cd "$(dirname "$0")"

APP="ChargerWatts.app"
PY="/opt/homebrew/Caskroom/miniconda/base/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"

echo "▸ иконка"
mkdir -p assets
"$PY" tools/make_icon.py

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>ChargerWatts</string>
    <key>CFBundleDisplayName</key><string>ChargerWatts</string>
    <key>CFBundleIdentifier</key><string>local.chargerwatts</string>
    <key>CFBundleExecutable</key><string>ChargerWatts</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>LSMinimumSystemVersion</key><string>12.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/ChargerWatts" <<'SH'
#!/bin/bash
HERE="$(cd "$(dirname "$0")/../Resources" && pwd)"
LOG="$HOME/Library/Logs/ChargerWatts.log"
mkdir -p "$(dirname "$LOG")"
for py in \
    /opt/homebrew/Caskroom/miniconda/base/bin/python3 \
    "$HOME/miniconda3/bin/python3" \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    "$HOME/.pyenv/shims/python3" \
    /usr/bin/python3
do
    if [ -x "$py" ] && "$py" -c 'import tkinter' >/dev/null 2>&1; then
        exec "$py" "$HERE/charger_watts.py" "$@" >>"$LOG" 2>&1
    fi
done
osascript -e 'display alert "ChargerWatts" message "Не найден python3 с поддержкой tkinter. Установите Python (например, с python.org или Homebrew) и запустите снова." as critical'
exit 1
SH
chmod +x "$APP/Contents/MacOS/ChargerWatts"

cp charger_watts.py "$APP/Contents/Resources/charger_watts.py"
cp assets/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
cp assets/icon_256.png "$APP/Contents/Resources/icon_256.png"

chmod +x start.command 2>/dev/null || true
echo "✓ готово: $APP"
