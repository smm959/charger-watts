#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChargerWatts — живой счётчик мощности зарядки для macOS.

Показывает, сколько ватт прямо сейчас отдаёт зарядка (адаптер) в Mac,
сколько уходит в батарею, какова нагрузка системы и хватает ли адаптера.

Данные берутся из IORegistry (AppleSmartBattery → PowerTelemetryData),
никаких прав root и сторонних библиотек не требуется.

Использование:
    python3 charger_watts.py            # графическое окно
    python3 charger_watts.py --once     # один замер в терминал
    python3 charger_watts.py --json     # один замер в JSON
    python3 charger_watts.py --watch 2  # поток замеров в терминал
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass

APP_NAME = "ChargerWatts"
UNKNOWN_MINUTES = 65535


# ──────────────────────────────────────────────────────────────────────────────
#  Разбор вывода ioreg
# ──────────────────────────────────────────────────────────────────────────────

class _IOParser:
    """Рекурсивный разбор текстового формата свойств IORegistry."""

    def __init__(self, text: str, pos: int = 0) -> None:
        self.s = text
        self.i = pos
        self.n = len(text)

    def parse(self):
        self._ws()
        return self._value()

    def _ws(self) -> None:
        s, i, n = self.s, self.i, self.n
        while i < n and s[i] in " \t\r\n":
            i += 1
        self.i = i

    def _value(self):
        self._ws()
        if self.i >= self.n:
            return None
        c = self.s[self.i]
        if c == "{":
            return self._dict()
        if c == "(":
            return self._list()
        if c == '"':
            return self._string()
        if c == "<":
            return self._data()
        return self._word()

    def _dict(self) -> dict:
        self.i += 1  # (
        out: dict = {}
        while self.i < self.n:
            self._ws()
            if self.i >= self.n:
                break
            c = self.s[self.i]
            if c == "}":
                self.i += 1
                break
            if c == ",":
                self.i += 1
                continue
            key = self._string() if c == '"' else self._word()
            self._ws()
            if self.i < self.n and self.s[self.i] == "=":
                self.i += 1
            out[str(key)] = self._value()
        return out

    def _list(self) -> list:
        self.i += 1  # (
        out: list = []
        while self.i < self.n:
            self._ws()
            if self.i >= self.n:
                break
            c = self.s[self.i]
            if c == ")":
                self.i += 1
                break
            if c == ",":
                self.i += 1
                continue
            out.append(self._value())
        return out

    def _string(self) -> str:
        self.i += 1  # "
        buf: list[str] = []
        while self.i < self.n:
            c = self.s[self.i]
            if c == "\\" and self.i + 1 < self.n:
                buf.append(self.s[self.i + 1])
                self.i += 2
                continue
            if c == '"':
                self.i += 1
                break
            buf.append(c)
            self.i += 1
        return "".join(buf)

    def _data(self) -> str:
        self.i += 1  # <
        start = self.i
        while self.i < self.n and self.s[self.i] != ">":
            self.i += 1
        raw = self.s[start:self.i]
        self.i += 1
        return raw

    def _word(self):
        start = self.i
        while self.i < self.n and self.s[self.i] not in ",}(){<>\n":
            self.i += 1
        w = self.s[start:self.i].strip()
        if w == "Yes":
            return True
        if w == "No":
            return False
        try:
            return int(w, 10)
        except ValueError:
            pass
        try:
            return int(w, 0)
        except ValueError:
            pass
        try:
            return float(w)
        except ValueError:
            return w


def _ioreg_path() -> str:
    for p in ("/usr/sbin/ioreg", "/usr/bin/ioreg"):
        if os.path.exists(p):
            return p
    return "ioreg"


def _strip_tree_prefix(text: str) -> str:
    """Убрать столбец «|» древесного вывода ioreg, сохранив структуру."""
    out = []
    for line in text.splitlines():
        prev = None
        while prev != line:
            prev = line
            line = re.sub(r"^[ \t]*\|[ \t]?", "", line)
        out.append(line)
    return "\n".join(out)


def read_battery_props() -> dict:
    """Прочитать свойства AppleSmartBattery как словарь."""
    raw = subprocess.run(
        [_ioreg_path(), "-rn", "AppleSmartBattery"],
        capture_output=True, text=True, timeout=6,
    ).stdout
    raw = _strip_tree_prefix(raw)
    m = re.search(r"\+-o\s+AppleSmartBattery\b", raw)
    start = raw.find("{", m.end()) if m else raw.find("{")
    if start < 0:
        return {}
    props = _IOParser(raw, start).parse()
    return props if isinstance(props, dict) else {}


def _s64(v):
    """Amperage/InstantAmperage приходят как беззнаковое 64-битное."""
    if isinstance(v, int) and not isinstance(v, bool) and v > (1 << 63):
        return v - (1 << 64)
    return v


def _num(d: dict, key: str, default=None):
    v = d.get(key, default)
    if isinstance(v, bool) or v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    return default


# ──────────────────────────────────────────────────────────────────────────────
#  Модель замера
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Sample:
    ts: float                  # когда прочитали (unix)
    update_time: float         # когда SMC обновил телеметрию (unix)
    plugged: bool              # подключён адаптер
    charging_flag: bool        # SMC считает, что идёт заряд
    soc: int                   # заряд батареи, %
    bat_mv: float              # напряжение батареи, мВ
    bat_ma: float              # ток батареи, мА (+ заряд, − разряд)
    sys_in_mw: float | None    # мощность от зарядки в систему, мВт
    sys_mv: float | None       # напряжение на входе, мВ
    sys_ma: float | None       # ток на входе, мА
    loss_mw: float | None      # потери в адаптере, мВт
    load_mw: float | None      # суммарная нагрузка системы, мВт
    wall_mw: float | None      # мощность из розетки, мВт
    adapter_w: float | None    # паспортная мощность адаптера, Вт
    adapter_mv: float | None   # напряжение контракта, мВ
    adapter_ma: float | None   # ток контракта, мА
    adapter_name: str          # описание адаптера
    to_full_min: float | None  # минут до полного
    to_empty_min: float | None # минут до разряда

    # ── производные величины, Вт ─────────────────────────────────────────────

    @property
    def has_telemetry(self) -> bool:
        return self.sys_in_mw is not None

    @property
    def charger_w(self) -> float | None:
        """Сколько ватт зарядка прямо сейчас отдаёт в Mac."""
        if self.sys_in_mw is not None:
            return self.sys_in_mw / 1000.0
        if self.wall_mw is not None and self.loss_mw is not None:
            return max(0.0, (self.wall_mw - self.loss_mw) / 1000.0)
        return None

    @property
    def wall_w(self) -> float | None:
        """Сколько ватт зарядка тянет из розетки."""
        if self.wall_mw is not None:
            return self.wall_mw / 1000.0
        if self.sys_in_mw is not None and self.loss_mw is not None:
            return (self.sys_in_mw + self.loss_mw) / 1000.0
        return None

    @property
    def battery_w(self) -> float | None:
        """Мощность батареи: + заряд, − разряд."""
        if self.bat_mv and self.bat_ma:
            return self.bat_mv * self.bat_ma / 1e6
        return None

    @property
    def load_w(self) -> float | None:
        """Суммарное потребление Mac."""
        if self.load_mw is not None:
            return self.load_mw / 1000.0
        c, b = self.charger_w, self.battery_w
        if c is not None and b is not None:
            return c - b
        return None

    @property
    def age(self) -> float:
        """Сколько секунд прошло с момента обновления телеметрии."""
        if not self.update_time:
            return 0.0
        return max(0.0, time.time() - self.update_time)

    def state(self) -> str:
        if not self.plugged:
            return "battery"
        b = self.battery_w
        if b is None:
            return "ac"
        if b > 0.5:
            return "charging"
        if b < -0.5:
            return "starved"
        return "idle"

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update({
            "charger_w": self.charger_w,
            "wall_w": self.wall_w,
            "battery_w": self.battery_w,
            "load_w": self.load_w,
            "state": self.state(),
            "age_s": round(self.age, 1),
        })
        return d


def read_sample() -> Sample:
    props = read_battery_props()
    if not props:
        raise RuntimeError("не удалось прочитать AppleSmartBattery")

    pt = props.get("PowerTelemetryData") or {}
    ipd = props.get("PowerDistribution") or {}
    adp = props.get("AdapterDetails") or {}
    bd = props.get("BatteryData") or {}
    ch = props.get("ChargerData") or {}

    plugged = bool(props.get("ExternalConnected") or props.get("AppleRawExternalConnected"))
    charging = bool(ch.get("IsCharging", props.get("IsCharging", False)))

    soc = _num(props, "CurrentCapacity", 0) or 0
    bat_mv = _num(props, "AppleRawBatteryVoltage") or _num(props, "Voltage", 0) or 0.0
    bat_ma = _s64(props.get("Amperage", props.get("InstantAmperage", 0))) or 0

    ttf = _num(bd, "AvgTimeToFull", _num(props, "AvgTimeToFull"))
    tte = _num(bd, "AvgTimeToEmpty", _num(props, "AvgTimeToEmpty"))
    if ttf in (None, UNKNOWN_MINUTES) or ttf <= 0:
        ttf = None
    if tte in (None, UNKNOWN_MINUTES) or tte <= 0:
        tte = None

    adapter_w = _num(adp, "Watts")
    if adapter_w is None and _num(adp, "AdapterVoltage") and _num(adp, "Current"):
        adapter_w = _num(adp, "AdapterVoltage") * _num(adp, "Current") / 1e6

    return Sample(
        ts=time.time(),
        update_time=_num(props, "UpdateTime", 0) or 0.0,
        plugged=plugged,
        charging_flag=charging,
        soc=int(soc),
        bat_mv=float(bat_mv),
        bat_ma=float(bat_ma),
        sys_in_mw=_num(pt, "SystemPowerIn"),
        sys_mv=_num(pt, "SystemVoltageIn"),
        sys_ma=_num(pt, "SystemCurrentIn"),
        loss_mw=_num(pt, "AdapterEfficiencyLoss"),
        load_mw=_num(pt, "SystemLoad"),
        wall_mw=_num(ipd, "IPDInputPower"),
        adapter_w=adapter_w,
        adapter_mv=_num(adp, "AdapterVoltage"),
        adapter_ma=_num(adp, "Current"),
        adapter_name=str(adp.get("Description") or "адаптер"),
        to_full_min=ttf,
        to_empty_min=tte,
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Форматирование
# ──────────────────────────────────────────────────────────────────────────────

def f1(v: float | None, dash: str = "—") -> str:
    """Число с одним знаком, запятой и минусом по-русски."""
    if v is None:
        return dash
    return f"{v:.1f}".replace(".", ",").replace("-", "\u2212")


def f2(v: float | None, dash: str = "—") -> str:
    """Число с двумя знаками после запятой."""
    if v is None:
        return dash
    return f"{v:.2f}".replace(".", ",").replace("-", "\u2212")


def f_int(v: float | None, dash: str = "—") -> str:
    if v is None:
        return dash
    return f"{v:.0f}"


def fmt_duration(minutes: float | None) -> str:
    if not minutes or minutes <= 0:
        return ""
    m = int(round(minutes))
    if m < 60:
        return f"{m} мин"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} ч {m:02d} мин"
    return f"{h // 24} сут"


STATE_TITLES = {
    "charging": "ЗАРЯЖАЕТ",
    "idle": "ПИТАЕТ MAC",
    "starved": "ЗАРЯДКИ НЕ ХВАТАЕТ",
    "battery": "ОТ БАТАРЕИ",
    "ac": "ПОДКЛЮЧЕНА",
}


def state_sentence(s: Sample) -> str:
    st = s.state()
    if st == "charging":
        parts = [f"в батарею идёт {f1(s.battery_w)} Вт", f"заряд {s.soc}%"]
        if s.to_full_min:
            parts.append(f"до полного ~{fmt_duration(s.to_full_min)}")
        return " · ".join(parts)
    if st == "idle":
        return f"батарея на паузе · заряд {s.soc}% · нагрузка {f1(s.load_w)} Вт"
    if st == "starved":
        return (f"адаптер слабее нагрузки: {f1(abs(s.battery_w or 0))} Вт "
                f"идёт из батареи · заряд {s.soc}%")
    if st == "battery":
        parts = [f"зарядка не подключена · заряд {s.soc}%"]
        if s.to_empty_min:
            parts.append(f"хватит на ~{fmt_duration(s.to_empty_min)}")
        return " · ".join(parts)
    return f"заряд {s.soc}%"


def format_text(s: Sample) -> str:
    st = s.state()
    big = abs(s.battery_w) if st == "battery" and s.battery_w is not None else s.charger_w
    caption = "отдаёт батарея" if st == "battery" else "отдаёт зарядка"
    lines = [f"{STATE_TITLES[st]} · {f1(big)} Вт ({caption})"]
    if s.plugged:
        lines.append(f"из розетки {f1(s.wall_w)} Вт · "
                     f"потери в адаптере {f2((s.loss_mw or 0) / 1000)} Вт")
    if s.sys_mv and s.plugged:
        lines.append(
            f"вход {f2((s.sys_mv or 0) / 1000)} В × {f2((s.sys_ma or 0) / 1000)} А"
            f" · батарея {f1(s.battery_w)} Вт"
        )
    else:
        lines.append(f"батарея {f2((s.bat_mv or 0) / 1000)} В × "
                     f"{f2((s.bat_ma or 0) / 1000)} А = {f1(s.battery_w)} Вт")
    lines.append(f"нагрузка Mac {f1(s.load_w)} Вт · заряд {s.soc}%")
    if s.adapter_w:
        lines.append(
            f"адаптер: {f1(s.adapter_w)} Вт ({s.adapter_name}), "
            f"{f2((s.adapter_mv or 0) / 1000)} В / {f2((s.adapter_ma or 0) / 1000)} А"
        )
    lines.append(f"{state_sentence(s)}")
    lines.append(f"данные SMC: {s.age:.0f} с назад")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
#  Опрос в отдельном потоке
# ──────────────────────────────────────────────────────────────────────────────

class Monitor:
    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self.queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.queue.put(read_sample())
            except Exception as exc:  # noqa: BLE001 — показываем проблему в UI
                self.queue.put(exc)
            self._stop.wait(self.interval)


# ──────────────────────────────────────────────────────────────────────────────
#  Графический интерфейс
# ──────────────────────────────────────────────────────────────────────────────

BG = "#0A0F16"
CARD = "#121A25"
EDGE = "#1E2B3C"
GRID = "#1A2534"
TXT = "#EEF4FF"
MUT = "#8194AC"
DIM = "#4C5F76"
GREEN = "#3DDC97"
AMBER = "#FFB020"
RED = "#FF6B6B"
BLUE = "#4CC2FF"

STATE_HUES = {"charging": GREEN, "idle": BLUE, "starved": AMBER,
              "battery": RED, "ac": BLUE}

FONT = "Helvetica Neue"
W, H = 480, 712


def _rounded(c, x0, y0, x1, y1, r, **kw):
    pts = [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
        x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
        x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]
    return c.create_polygon(pts, smooth=True, **kw)


def _nice_scale(values: list[float]) -> float:
    top = max([v for v in values if v is not None and v > 0] or [10.0])
    for step in (5, 10, 15, 20, 25, 30, 40, 50, 60, 80, 96, 100,
                 140, 180, 240, 300):
        if top <= step:
            return float(step)
    return float(math.ceil(top / 50.0) * 50)


def _spaced(text: str) -> str:
    return "\u2009".join(text)


class App:
    def __init__(self, monitor: Monitor, topmost: bool = False) -> None:
        import tkinter as tk

        self.tk = tk
        self.monitor = monitor
        self.root = tk.Tk()
        self.root.title("ChargerWatts — ватты зарядки")
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self.root.attributes("-topmost", bool(topmost))
        self.topmost = bool(topmost)

        self.canvas = tk.Canvas(self.root, width=W, height=H, bg=BG,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        here = os.path.dirname(os.path.abspath(__file__))
        for name in ("assets/icon_256.png", "icon_256.png"):
            path = os.path.join(here, name)
            if os.path.exists(path):
                try:
                    self._icon = tk.PhotoImage(file=path)
                    self.root.iconphoto(True, self._icon)
                except Exception:  # noqa: BLE001 — иконка не критична
                    pass
                break

        self.snap: Sample | None = None
        self.error: str | None = None
        self.history: deque[tuple[float, float]] = deque(maxlen=1200)
        self._last_key = None
        self._copy_flash = 0.0

        self.canvas.bind("<Button-1>", self._on_click)
        self.root.bind("<Escape>", lambda _e: self.root.destroy())

        self._place_window()
        monitor.start()
        self._tick()

    # ── окно ─────────────────────────────────────────────────────────────────

    def _place_window(self) -> None:
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = int(sw * 0.5 - W * 0.5)
        y = int(sh * 0.5 - H * 0.5 - 20)
        self.root.geometry(f"{W}x{H}+{x}+{max(60, y)}")

    def _buttons(self) -> list[tuple[str, tuple[float, float, float, float], str]]:
        """(tag, rect, подпись)"""
        return [
            ("topmost", (24, 664, 158, 696), "поверх окон"),
            ("copy", (170, 664, 304, 696), "копировать"),
        ]

    def _on_click(self, event) -> None:
        for tag, (x0, y0, x1, y1), _label in self._buttons():
            if x0 <= event.x <= x1 and y0 <= event.y <= y1:
                if tag == "topmost":
                    self.topmost = not self.topmost
                    self.root.attributes("-topmost", self.topmost)
                elif tag == "copy" and self.snap:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(format_text(self.snap))
                    self._copy_flash = time.time() + 1.4
                    self.root.after(1500, self._render)
                self._render()
                return

    # ── цикл обновления ──────────────────────────────────────────────────────

    def _tick(self) -> None:
        changed = False
        try:
            while True:
                item = self.monitor.queue.get_nowait()
                if isinstance(item, Sample):
                    self.snap, self.error = item, None
                    self._push_history(item)
                    changed = True
                else:
                    self.error = str(item)
                    changed = True
        except queue.Empty:
            pass

        if changed or self._age_changed():
            self._render()
        self.root.after(250, self._tick)

    def _push_history(self, s: Sample) -> None:
        if s.plugged and s.charger_w is not None:
            w = s.charger_w
        elif s.battery_w is not None:
            w = abs(s.battery_w)
        else:
            return
        self.history.append((s.ts, w))

    def _age_changed(self) -> bool:
        if self.snap is None:
            return False
        key = int(self.snap.age)
        if key != self._last_key:
            self._last_key = key
            return True
        return False

    # ── отрисовка ────────────────────────────────────────────────────────────

    def _render(self) -> None:
        c = self.canvas
        c.delete("all")
        s = self.snap

        self._header(s)
        if s is None:
            self._empty_state()
            self._footer()
            return

        self._gauge(s)
        self._spark(s)
        self._details(s)
        self._footer()

    def _header(self, s: Sample | None) -> None:
        c = self.canvas
        c.create_text(24, 30, anchor="w", text=_spaced("ЗАРЯДКА"),
                      font=(FONT, 10, "bold"), fill=DIM)

        if s is None:
            age_text, dot = "нет данных", RED
        else:
            age = s.age
            if age < 2:
                age_text = "только что"
            elif age < 60:
                age_text = f"{int(age)} с назад"
            else:
                age_text = f"{int(age // 60)} мин назад"
            dot = GREEN if age < 90 else (AMBER if age < 180 else RED)

        c.create_text(456, 30, anchor="e", text=age_text,
                      font=(FONT, 10), fill=MUT)
        dx = 456 - self._measure(age_text, 10, "normal") - 11
        c.create_oval(dx - 4, 26, dx + 4, 34, outline="", fill=dot)
        c.create_oval(dx - 9, 21, dx + 9, 39, outline="#132030", width=1, fill="")

    def _empty_state(self) -> None:
        c = self.canvas
        c.create_text(240, 200, text="НЕТ ДАННЫХ",
                      font=(FONT, 22, "bold"), fill=MUT)
        msg = self.error or "Ожидание телеметрии батареи…"
        c.create_text(240, 236, text=msg, font=(FONT, 11), fill=DIM,
                      width=380, justify="center")

    def _gauge(self, s: Sample) -> None:
        c = self.canvas
        st = s.state()

        if st == "battery":
            value = abs(s.battery_w) if s.battery_w is not None else None
            caption = "Вт · отдаёт батарея"
        else:
            value = s.charger_w
            caption = "Вт · зарядка отдаёт Mac"

        hue = STATE_HUES[st]

        scale = _nice_scale([
            value or 0.0,
            (s.adapter_w or 0.0) if s.plugged else 0.0,
            max(self.history, key=lambda p: p[1])[1] if self.history else 0.0,
        ])

        cx, cy, R, rw = 240, 196, 100, 15
        c.create_oval(cx - R, cy - R, cx + R, cy + R,
                      outline="#151F2C", width=rw)

        frac = 0.0 if not value else max(0.0, min(1.0, value / scale))
        start_deg, sweep = 225.0, 270.0
        if frac > 0.002:
            c.create_arc(cx - R, cy - R, cx + R, cy + R,
                         start=start_deg, extent=-sweep * frac,
                         style="arc", outline=hue, width=rw)
            ang = math.radians(start_deg - sweep * frac)
            ex, ey = cx + R * math.cos(ang), cy - R * math.sin(ang)
            c.create_oval(ex - rw / 2, ey - rw / 2, ex + rw / 2, ey + rw / 2,
                          outline="", fill=hue)
        ang0 = math.radians(start_deg)
        sx, sy = cx + R * math.cos(ang0), cy - R * math.sin(ang0)
        c.create_oval(sx - rw / 2, sy - rw / 2, sx + rw / 2, sy + rw / 2,
                      outline="", fill="#1D2B3C")

        big = f1(value)
        size = 66 if len(big) <= 3 else (52 if len(big) <= 4 else 42)
        c.create_text(cx, cy - 12, text=big, font=(FONT, size, "bold"), fill=TXT)
        c.create_text(cx, cy + 44, text=caption, font=(FONT, 12), fill=MUT)
        c.create_text(cx, cy + 66, text=f"шкала 0–{f_int(scale)} Вт",
                      font=(FONT, 9), fill=DIM)

        # статус-пилюля
        title = STATE_TITLES[st]
        tw = self._measure(title, 10, "bold") + 44
        px0, px1, py = cx - tw / 2, cx + tw / 2, 330
        _rounded(c, px0, py - 15, px1, py + 15, 15, fill=self._tint(hue),
                 outline="")
        c.create_oval(px0 + 16, py - 3, px0 + 22, py + 3, outline="", fill=hue)
        c.create_text((px0 + px1) / 2 + 5, py, text=title,
                      font=(FONT, 10, "bold"), fill=hue)

        c.create_text(240, 366, text=state_sentence(s), font=(FONT, 10),
                      fill=MUT, width=430, justify="center")

    def _tint(self, hex_color: str, k: float = 0.16, base: int = 10) -> str:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        mix = lambda v: int(v * k + base)  # noqa: E731
        return f"#{mix(r):02x}{mix(g):02x}{mix(b):02x}"

    def _measure(self, text: str, size: int, weight: str) -> int:
        import tkinter.font as tkfont
        return tkfont.Font(family=FONT, size=size, weight=weight).measure(text)

    def _spark(self, s: Sample) -> None:
        c = self.canvas
        _rounded(c, 24, 386, 456, 514, 18, fill=CARD, outline=EDGE)

        now = time.time()
        window = 180.0
        points = [(t, w) for t, w in self.history if now - t <= window]
        hue = STATE_HUES[s.state()]
        peak = max([w for _t, w in points] or [0.0])
        vals = [w for _t, w in points] or [0.0]
        stats = (f"мин {f1(min(vals))} · сред {f1(sum(vals) / len(vals))} · "
                 f"макс {f1(max(vals))}")

        c.create_text(42, 406, anchor="w", text=_spaced("МОЩНОСТЬ"),
                      font=(FONT, 9, "bold"), fill=DIM)
        c.create_text(438, 406, anchor="e", text=stats, font=(FONT, 9), fill=MUT)

        x0, x1 = 42, 438
        y0, y1 = 424, 492
        scale = _nice_scale([peak, (s.adapter_w or 0.0) if s.plugged else 0.0, 10.0])

        for frac in (0.25, 0.5, 0.75, 1.0):
            y = y1 - (y1 - y0) * frac
            c.create_line(x0, y, x1, y, fill=GRID, width=1)

        if points:
            edge_t = now - window
            if points[0][0] > edge_t + 1.0:
                points = [(edge_t, points[0][1])] + points

        if len(points) >= 2:
            xy = []
            for t, w in points:
                x = x0 + (x1 - x0) - (now - t) / window * (x1 - x0)
                y = y1 - (y1 - y0) * min(1.0, max(0.0, w) / scale)
                xy.extend([x, y])
            fill_xy = [x0, y1] + xy + [x1, y1]
            c.create_polygon(fill_xy, fill=self._tint(hue, 0.13, 10),
                             outline="", smooth=False)
            c.create_line(*xy, fill=hue, width=2, smooth=False)
            lx, ly = xy[-2], xy[-1]
            c.create_oval(lx - 3, ly - 3, lx + 3, ly + 3, outline="", fill=TXT)
        else:
            c.create_text((x0 + x1) / 2, (y0 + y1) / 2,
                          text="накапливаю данные…", font=(FONT, 10), fill=DIM)

        if s.adapter_w and s.plugged:
            yref = y1 - (y1 - y0) * min(1.0, s.adapter_w / scale)
            c.create_line(x0, yref, x1, yref, fill="#3A2E17", width=1, dash=(3, 3))
            if 22 < yref - y0 < y1 - y0 - 12:
                c.create_text(x0 + 2, yref - 7, anchor="w",
                              text=f"адаптер {f1(s.adapter_w)} Вт",
                              font=(FONT, 8), fill="#8A6D33")

        c.create_text(x0, 504, anchor="w", text="−3 мин",
                      font=(FONT, 8), fill=DIM)
        c.create_text(x1, 504, anchor="e", text="сейчас",
                      font=(FONT, 8), fill=DIM)

    def _details(self, s: Sample) -> None:
        c = self.canvas
        _rounded(c, 24, 524, 456, 652, 18, fill=CARD, outline=EDGE)

        wall = f1(s.wall_w) + " Вт" if (s.wall_w is not None and s.plugged) else "—"
        load = f1(s.load_w) + " Вт" if s.load_w is not None else "—"
        bat = s.battery_w
        bat_txt = "—" if bat is None else (f"−{f1(abs(bat))} Вт" if bat < 0 else f"+{f1(bat)} Вт")
        vin = (f"{f2((s.sys_mv or 0) / 1000)} В · {f2((s.sys_ma or 0) / 1000)} А"
               if (s.sys_mv and s.plugged) else "—")
        adp = (f"{f1(s.adapter_w)} Вт · {f1((s.adapter_mv or 0) / 1000)} В"
               if s.adapter_w and s.plugged else "—")

        rows = [
            (("ИЗ РОЗЕТКИ", wall), ("НАГРУЗКА MAC", load)),
            (("БАТАРЕЯ", bat_txt), ("ЗАРЯД", f"{s.soc}%")),
            (("ВХОД", vin), ("АДАПТЕР", adp)),
        ]
        ys = (550, 588, 626)
        for i, ((l1, v1), (l2, v2)) in enumerate(rows):
            y = ys[i]
            c.create_text(42, y, anchor="w", text=_spaced(l1),
                          font=(FONT, 9, "bold"), fill=DIM)
            c.create_text(226, y, anchor="e", text=v1,
                          font=(FONT, 11, "bold"), fill=TXT)
            c.create_text(258, y, anchor="w", text=_spaced(l2),
                          font=(FONT, 9, "bold"), fill=DIM)
            c.create_text(438, y, anchor="e", text=v2,
                          font=(FONT, 11, "bold"), fill=TXT)
            if i < len(rows) - 1:
                c.create_line(42, y + 19, 438, y + 19, fill="#182634", width=1)

    def _footer(self) -> None:
        c = self.canvas
        for tag, (x0, y0, x1, y1), label in self._buttons():
            active = tag == "topmost" and self.topmost
            _rounded(c, x0, y0, x1, y1, 16,
                     fill="#16324A" if active else "#121A25",
                     outline=BLUE if active else EDGE)
            text = label
            if tag == "copy" and self._copy_flash > time.time():
                text = "скопировано"
            c.create_text((x0 + x1) / 2, (y0 + y1) / 2 + 1, text=text,
                          font=(FONT, 10), fill=BLUE if active or
                          (tag == "copy" and text == "скопировано") else MUT)
        c.create_text(456, 680, anchor="e", text="SMC · ioreg · 1 Гц",
                      font=(FONT, 8), fill=DIM)

    def run(self) -> None:
        self.root.mainloop()
        self.monitor.stop()


# ──────────────────────────────────────────────────────────────────────────────
#  CLI
# ──────────────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Сколько ватт сейчас отдаёт зарядка (macOS, AppleSmartBattery).",
    )
    ap.add_argument("--once", action="store_true", help="один замер и выход")
    ap.add_argument("--json", action="store_true", help="вывести замер в JSON")
    ap.add_argument("--watch", nargs="?", const=1.0, type=float, metavar="СЕК",
                    help="печатать замеры каждые N секунд (по умолчанию 1)")
    ap.add_argument("--topmost", action="store_true", help="окно поверх всех")
    args = ap.parse_args(argv)

    if args.json:
        try:
            print(json.dumps(read_sample().to_dict(), ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            return 1
        return 0

    if args.once:
        try:
            print(format_text(read_sample()))
        except Exception as exc:  # noqa: BLE001
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.watch:
        try:
            while True:
                s = read_sample()
                ch = f1(s.charger_w) if s.plugged else "—"
                print(f"{time.strftime('%H:%M:%S')}  "
                      f"зарядка {ch:>5} Вт · "
                      f"батарея {f1(s.battery_w):>6} Вт · "
                      f"нагрузка {f1(s.load_w):>5} Вт · "
                      f"{s.soc:>3}% · {STATE_TITLES[s.state()]}", flush=True)
                time.sleep(max(0.2, args.watch))
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1

    # GUI
    try:
        import tkinter  # noqa: F401
    except Exception:  # noqa: BLE001
        print("Tkinter недоступен. Данные можно получить так:\n"
              f"  {sys.executable} {__file__} --once", file=sys.stderr)
        return 2

    App(Monitor(1.0), topmost=args.topmost).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
