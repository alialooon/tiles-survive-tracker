"""Launch Tracker with a bounded Windows-only game capture fallback.

The legacy update bootstrap accepts updates to launcher.py. Keeping the capture
compatibility fix in this file avoids updates that older bootstraps reject.
"""
from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _foreground_is_game(hwnd: int | None) -> bool:
    """Win32 GetForegroundWindow can briefly return NULL (ctypes None).

    Returning False is safe: we will try to activate the game again instead
    of crashing when Windows is transitioning focus between two windows.
    """
    if hwnd is None or sys.platform != 'win32':
        return False
    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        active = user32.GetForegroundWindow()
        return active is not None and int(active) == int(hwnd)
    except (OSError, AttributeError, ValueError, TypeError):
        return False


def _activate_game(hwnd: int) -> bool:
    """Best-effort foreground activation; never synthesize gameplay input."""
    if sys.platform != 'win32':
        return False
    import windows_capture as wc
    if wc._game_is_foreground(hwnd):
        return True
    try:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
            return False
        user32.SetForegroundWindow(hwnd)
        for _ in range(5):
            if wc._game_is_foreground(hwnd):
                return True
            time.sleep(0.12)
    except (OSError, AttributeError, ValueError):
        return False
    return wc._game_is_foreground(hwnd)


def capture_game(hwnd: int):
    """Capture the game's client rectangle, not the desktop.

    Unity/DirectX may not support PrintWindow; only then use a bounded crop of
    the visible game window. Do not capture if another app is foreground.
    """
    import windows_capture as wc
    bbox = wc.client_bbox(hwnd)
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    frame = wc._capture_printwindow(hwnd, width, height)
    if wc._is_usable(frame, width, height):
        return frame

    if not wc._game_is_foreground(hwnd) and not _activate_game(hwnd):
        raise RuntimeError(
            'Игровой движок не передал отдельный кадр и не удалось активировать Tiles Survive. '
            'Переключитесь на игру вручную и повторите снимок.'
        )
    if not wc._game_is_foreground(hwnd):
        raise RuntimeError('Игра перестала быть активным окном. Повторите снимок.')
    bbox = wc.client_bbox(hwnd)
    width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    frame = wc._capture_visible_region(bbox)
    if not wc._is_usable(frame, width, height):
        raise RuntimeError('Не удалось получить изображение окна игры. Проверьте оконный режим.')
    return frame



import json
import queue
import tkinter as tk
from tkinter import ttk, messagebox
import app
from app import TrackerApp
from engine import store_scan
from survey import ManualSurvey
from tracker import detect_markers, load_image


class SimpleTrackerApp(TrackerApp):
    """One-click manual-pan observer; never sends input to Tiles Survive."""
    def __init__(self, root):
        self._learn_frame = False
        self._last_result = None
        self._shown_scan = None
        super().__init__(root)
        root.after(400, self._refresh_button)

    def _layout(self):
        self.root.title('Tiles Survive Tracker — простой поиск')
        self.root.geometry('1020x700')
        self.root.minsize(720, 520)
        panel = ttk.Frame(self.root, padding=16)
        panel.pack(fill='both', expand=True)
        ttk.Label(panel, text='Tiles Survive Tracker',
                  font=('Segoe UI', 19, 'bold')).pack(anchor='w')
        ttk.Label(panel, text='Находит совпадения на видимой карте. Игрой не управляет.',
                  foreground='#64717a').pack(anchor='w', pady=(3, 12))
        buttons = ttk.Frame(panel)
        buttons.pack(fill='x', pady=(0, 13))
        self.go_btn = ttk.Button(buttons, text='▶ Начать поиск', command=self.toggle_watch)
        self.go_btn.pack(side='left', padx=(0, 8), ipadx=14, ipady=8)
        ttk.Button(buttons, text='Настроить маркер', command=self.learn_marker).pack(
            side='left', padx=(0, 8), ipady=8)
        ttk.Button(buttons, text='Открыть находки', command=self.open_results).pack(
            side='left', ipady=8)
        self.counts_var = tk.StringVar(value='Кадров: 0   •   Совпадений: 0   •   Различимых*: 0')
        ttk.Label(panel, textvariable=self.counts_var,
                  font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(0, 8))
        self.canvas = tk.Canvas(panel, background='#19242b', highlightthickness=0,
                                cursor='crosshair')
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<ButtonPress-1>', self.crop_press)
        self.canvas.bind('<B1-Motion>', self.crop_move)
        self.canvas.bind('<ButtonRelease-1>', self.crop_release)
        self.canvas.bind('<Configure>', self._on_canvas_resize)
        ttk.Label(panel, textvariable=self.status_var, wraplength=900).pack(
            anchor='w', pady=(8, 2))
        ttk.Label(panel, text='*Повторы возможны. Игровые X/Y и владелец точки не определяются.',
                  foreground='#65717b').pack(anchor='w')
        # Old persistence helpers expect these widgets, but they are not displayed.
        self.scan_box = ttk.Combobox(self.root, state='readonly')
        self.table = ttk.Treeview(self.root,
                                 columns=('num', 'x', 'y', 'score', 'status', 'player'))
        self.window_box = ttk.Combobox(self.root, textvariable=self.window_var,
                                       state='readonly')
        self.log = tk.Text(self.root, height=1)
        self.log.configure(state='disabled')

    def refresh_windows(self):
        try:
            windows = app.list_game_windows()
            self.window_handles = {f'{w.title} [{w.hwnd}]': w.hwnd for w in windows}
            self.window_box.configure(values=list(self.window_handles))
            if not windows:
                self.selected_hwnd = None
                self.window_var.set('Не найдено')
                self.log_message('Откройте Tiles Survive и перейдите на карту.')
                return
            if self.selected_hwnd not in [w.hwnd for w in windows]:
                self.selected_hwnd = windows[0].hwnd
            for label, hwnd in self.window_handles.items():
                if hwnd == self.selected_hwnd:
                    self.window_var.set(label)
                    break
            self.log_message('Окно Tiles Survive найдено автоматически.')
        except Exception as exc:
            self.log_message('Не удалось обнаружить игру: ' + str(exc))

    def toggle_watch(self):
        if self.worker and self.worker.is_alive():
            self.stop()
            self.go_btn.configure(text='Останавливаю…', state='disabled')
        elif not self.template_var.get().strip() or not Path(self.template_var.get()).is_file():
            self.learn_marker()
        else:
            self.start_watch()

    def learn_marker(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo('Поиск', 'Сначала остановите поиск.')
            return
        if self.selected_hwnd is None:
            self.refresh_windows()
        if self.selected_hwnd is None:
            messagebox.showinfo('Игра не найдена', 'Откройте карту Tiles Survive.')
            return
        self._learn_frame = True
        self.capture_screen()

    def _show_image(self, image):
        super()._show_image(image)
        if self._learn_frame:
            self._learn_frame = False
            self.begin_crop()
            self.log_message('Обведите один ресурсный маркер мышью на снимке.')

    def crop_release(self, event):
        was_cropping = self.template_mode
        super().crop_release(event)
        if was_cropping and not self.template_mode and Path(self.template_var.get()).is_file():
            data = app.get_saved()
            data['template'] = self.template_var.get()
            app.SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            self.log_message('Образец сохранён. Запускаю поиск автоматически.')
            self.root.after(450, self.start_watch)

    def start_watch(self):
        if self.worker and self.worker.is_alive():
            return
        if self.selected_hwnd is None:
            self.refresh_windows()
        if self.selected_hwnd is None:
            messagebox.showinfo('Игра не найдена', 'Откройте карту Tiles Survive.')
            return
        try:
            template, threshold, base, zone = self._params()
        except Exception as exc:
            messagebox.showerror('Маркер', str(exc))
            return
        hwnd = self.selected_hwnd
        self.counts_var.set('Кадров: 0   •   Совпадений: 0   •   Различимых*: 0')
        self.go_btn.configure(text='■ Остановить поиск', state='normal')
        self.log_message('Поиск активен. Перемещайте карту мышью, остальное автоматически.')
        self.root.iconify()  # Do not cover the game with Tracker itself.

        def work():
            survey = ManualSurvey(app.DATA_DIR / 'sessions')
            consecutive_errors = 0
            while not self.stop_event.is_set():
                try:
                    frame = app.capture_window(hwnd)
                    consecutive_errors = 0
                except Exception as exc:
                    consecutive_errors += 1
                    if consecutive_errors >= 4:
                        raise RuntimeError('Не могу прочитать окно игры: ' + str(exc)) from exc
                    if self.stop_event.wait(2.5):
                        break
                    continue
                if survey.should_scan(frame):
                    markers = detect_markers(frame, template, threshold=threshold, zone=zone)
                    scan_id = f'empty-{survey.attempted}'
                    if markers:
                        result = store_scan(app.DB_PATH, app.DATA_DIR / 'results',
                                            frame, template, threshold=threshold,
                                            base_xy=base, zone=zone,
                                            source='windows_manual_pan')
                        scan_id = result['scan_id']
                        self.events.put(('scan', result))
                    survey.add(frame, markers, scan_id)
                    self.events.put(('stats', (survey.attempted - survey.skipped,
                                               sum(p['observations'] for p in survey.points),
                                               len(survey.points))))
                if self.stop_event.wait(2.5):
                    break
            self.events.put(('info', 'Поиск остановлен. Результаты сохранены.'))

        self._run(work)

    def _refresh_button(self):
        if not (self.worker and self.worker.is_alive()):
            self.go_btn.configure(text='▶ Начать поиск', state='normal')
        if self.root.state() != 'iconic' and self._last_result:
            scan_id = self._last_result['scan_id']
            if scan_id != self._shown_scan:
                self._shown_scan = scan_id
                try:
                    self.current_image = load_image(self._last_result['screenshot_path'])
                    self._show_image(load_image(self._last_result['annotated_path']))
                except (OSError, ValueError):
                    pass
        self.root.after(400, self._refresh_button)

    def _poll(self):
        try:
            while True:
                tag, data = self.events.get_nowait()
                if tag == 'error':
                    self.root.deiconify()
                    self.log_message('Ошибка: ' + str(data))
                    messagebox.showerror('Tracker', str(data))
                elif tag == 'info':
                    self.log_message(str(data))
                elif tag == 'image':
                    self.current_image = data
                    self._show_image(data)
                elif tag == 'scan':
                    self._last_result = data
                elif tag == 'stats':
                    viewed, observations, distinct = data
                    self.counts_var.set(f'Кадров: {viewed}   •   Совпадений: {observations}   '
                                        f'•   Различимых*: {distinct}')
                elif tag == 'done' and self._restore_after_capture:
                    self._restore_after_capture = False
                    self.root.deiconify()
                    self.root.lift()
        except queue.Empty:
            pass
        self.root.after(150, self._poll)


def run() -> int:
    if not (ROOT / 'app.py').is_file():
        print('ERROR: app.py is missing. Extract the full Tracker archive.', flush=True)
        return 2
    import windows_capture as wc
    # Replace the old unchecked int(GetForegroundWindow()) call before any
    # capture: Win32 returns NULL during focus changes and ctypes maps it to None.
    wc._game_is_foreground = _foreground_is_game
    import app
    wc.capture_window = capture_game
    app.capture_window = capture_game
    app.TrackerApp = SimpleTrackerApp
    app.main()
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
