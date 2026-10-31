#!/bin/bash
# Двойной клик в Finder → откроется окно ChargerWatts.
cd "$(dirname "$0")" || exit 1

for py in \
    /opt/homebrew/Caskroom/miniconda/base/bin/python3 \
    "$HOME/miniconda3/bin/python3" \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    "$HOME/.pyenv/shims/python3" \
    /usr/bin/python3
do
    if [ -x "$py" ] && "$py" -c 'import tkinter' >/dev/null 2>&1; then
        exec "$py" charger_watts.py "$@"
    fi
done

echo "Не найден python3 с поддержкой tkinter."
echo "Поставьте Python с python.org или: brew install python-tk"
read -r -n 1 -p "Нажмите любую клавишу…"
