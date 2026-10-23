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


import re
import difflib
import cv2
from datetime import datetime


def _clean_player_name(value):
    value = re.sub(r'^\s*\[[^\]]{1,12}\]\s*', '', str(value))
    return re.sub(r'[^a-z0-9]', '', value.casefold())


def _name_match(label, target):
    """Compare OCR labels conservatively: exact first, then reviewable candidates."""
    wanted = _clean_player_name(target)
    seen = _clean_player_name(label)
    if len(wanted) < 4 or not seen:
        return False, 0.0
    if wanted in seen:
        return True, 1.0
    if len(seen) < len(wanted) - 2:
        return False, 0.0
    ratio = difflib.SequenceMatcher(None, seen, wanted).ratio()
    return ratio >= 0.84, ratio


class PlayerFinderApp(SimpleTrackerApp):
    """Read-only OCR search for a nickname on already-visible settlements.

    A text hit is a candidate, NOT confirmed settlement ownership or map X/Y.
    The map is moved only by the user, never by this application.
    """
    def _layout(self):
        self.root.title('Tiles Survive Tracker — поиск игрока')
        self.root.geometry('950x660')
        self.root.minsize(700, 480)
        opts = app.get_saved()
        self.target_name = tk.StringVar(value=opts.get('player_search_name', 'KingBonbee'))
        self.target_state = tk.StringVar(value=str(opts.get('player_search_state', '752')))
        body = ttk.Frame(self.root, padding=16)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Поиск поселения по имени',
                  font=('Segoe UI', 18, 'bold')).pack(anchor='w')
        ttk.Label(body, text='Штат и имя заданы. Tracker читает только видимую карту.',
                  foreground='#61717a').pack(anchor='w', pady=(3, 12))
        header = ttk.Frame(body)
        header.pack(fill='x', pady=(0, 10))
        ttk.Label(header, text='Игрок:').pack(side='left')
        ttk.Entry(header, textvariable=self.target_name, width=24).pack(side='left', padx=(6, 18))
        ttk.Label(header, text='Штат:').pack(side='left')
        ttk.Entry(header, textvariable=self.target_state, width=6).pack(side='left', padx=6)
        controls = ttk.Frame(body)
        controls.pack(fill='x', pady=(0, 12))
        self.go_btn = ttk.Button(controls, text='▶ Начать поиск', command=self.toggle_watch)
        self.go_btn.pack(side='left', padx=(0, 8), ipadx=18, ipady=8)
        ttk.Button(controls, text='Открыть находки', command=self.open_player_results).pack(
            side='left', ipady=8)
        self.counts_var = tk.StringVar(value='Просмотрено участков: 0   •   Совпадений: 0')
        ttk.Label(body, textvariable=self.counts_var,
                  font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(0, 8))
        self.canvas = tk.Canvas(body, background='#1b242a', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', self._on_canvas_resize)
        ttk.Label(body, textvariable=self.status_var, wraplength=850).pack(
            anchor='w', pady=(8, 2))
        ttk.Label(body, text='Совпадение с ником — только кандидат. Точные игровые X/Y требуют проверки.',
                  foreground='#62717a').pack(anchor='w')
        # Hidden storage widgets are required by the legacy history helpers.
        self.window_box = ttk.Combobox(self.root, textvariable=self.window_var)
        self.scan_box = ttk.Combobox(self.root, state='readonly')
        self.table = ttk.Treeview(self.root,
                                 columns=('num', 'x', 'y', 'score', 'status', 'player'))
        self.log = tk.Text(self.root, height=1)
        self.log.configure(state='disabled')

    def toggle_watch(self):
        if self.worker and self.worker.is_alive():
            self.stop()
            self.go_btn.configure(text='Останавливаю…', state='disabled')
        else:
            self.start_watch()

    def _save_target(self):
        name = self.target_name.get().strip()
        state = self.target_state.get().strip()
        if len(_clean_player_name(name)) < 4:
            raise ValueError('Введите никнейм длиной не менее четырёх символов.')
        if not state.isdigit() or int(state) < 1:
            raise ValueError('Введите номер штата цифрами.')
        config = app.get_saved()
        config['player_search_name'] = name
        config['player_search_state'] = state
        app.SETTINGS_PATH.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
        return name, state

    def start_watch(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            name, state = self._save_target()
        except ValueError as exc:
            messagebox.showerror('Поиск игрока', str(exc))
            return
        if self.selected_hwnd is None:
            self.refresh_windows()
        if self.selected_hwnd is None:
            messagebox.showinfo('Tiles Survive', 'Откройте карту игры и повторите поиск.')
            return
        hwnd = self.selected_hwnd
        self.go_btn.configure(text='■ Остановить поиск', state='normal')
        self.counts_var.set('Просмотрено участков: 0   •   Совпадений: 0')
        self.log_message('Ищу ' + name + ' в штате #' + state +
                         '. Перемещайте карту мышью, Tracker не управляет игрой.')

        def work():
            try:
                from rapidocr import RapidOCR
            except ImportError as exc:
                raise RuntimeError(
                    'Не установлено распознавание текста RapidOCR. '
                    'Перезапустите start_windows.bat для установки зависимостей.'
                ) from exc
            ocr = RapidOCR()
            fingerprints = []
            reviewed = 0
            errors = 0
            while not self.stop_event.is_set():
                try:
                    frame = app.capture_window(hwnd)
                    errors = 0
                except Exception as exc:
                    errors += 1
                    if errors >= 5:
                        raise RuntimeError('Не могу получить кадр игры: ' + str(exc)) from exc
                    if self.stop_event.wait(3.0):
                        break
                    continue
                h, w = frame.shape[:2]
                if h < 200 or w < 400:
                    raise RuntimeError('Слишком маленькое окно игры для поиска имени.')
                from survey import fingerprint, similar_view
                fp = fingerprint(frame)
                if any(similar_view(fp, old, 4.0) for old in fingerprints):
                    if self.stop_event.wait(3.5):
                        break
                    continue
                fingerprints.append(fp)
                if len(fingerprints) > 120:
                    fingerprints.pop(0)
                reviewed += 1
                # Exclude fixed HUD and the chat bar; keep the building-name area.
                x0, y0 = int(w * .025), int(h * .07)
                x1, y1 = max(int(w * .7), w - 140), h - 125
                region = frame[y0:y1, x0:x1]
                scale = 1.7 if w <= 1900 else 1.2
                enlarged = cv2.resize(region, None, fx=scale, fy=scale,
                                       interpolation=cv2.INTER_CUBIC)
                output = ocr(enlarged)
                labels = list(getattr(output, 'txts', None) or ())
                boxes = getattr(output, 'boxes', None)
                confidences = getattr(output, 'scores', None)
                for i, label in enumerate(labels):
                    matched, similarity = _name_match(label, name)
                    confidence = (float(confidences[i]) if confidences is not None
                                  and i < len(confidences) else 0.0)
                    if not matched or confidence < 0.45:
                        continue
                    if boxes is None or i >= len(boxes):
                        continue
                    points = boxes[i]
                    coords = [(int(round(float(p[0]) / scale + x0)),
                               int(round(float(p[1]) / scale + y0))) for p in points]
                    if len(coords) < 4:
                        continue
                    annotated = frame.copy()
                    poly = __import__('numpy').array(coords, dtype='int32')
                    cv2.polylines(annotated, [poly], True, (0, 220, 255), 3)
                    folder = app.DATA_DIR / 'player_matches'
                    folder.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    name_safe = re.sub(r'[^a-zA-Z0-9_-]', '_', name)[:40]
                    image_path = folder / (name_safe + '_state_' + state + '_' + stamp + '.png')
                    app.save_image(image_path, annotated)
                    metadata = {
                        'target': name, 'state': state,
                        'recognized_text': str(label),
                        'ocr_confidence': confidence,
                        'name_similarity': similarity,
                        'screen_polygon': coords,
                        'game_x': None, 'game_y': None,
                        'status': 'candidate_needs_in_game_confirmation'
                    }
                    image_path.with_suffix('.json').write_text(
                        json.dumps(metadata, ensure_ascii=False, indent=2),
                        encoding='utf-8')
                    self.events.put(('player_hit', (str(image_path), str(label), confidence)))
                    self.stop_event.set()
                    break
                self.events.put(('player_progress', reviewed))
                if self.stop_event.wait(3.5):
                    break
            self.events.put(('info', 'Поиск завершён. Результаты находятся в папке находок.'))

        self.root.iconify()
        self.root.after(650, lambda: self._run(work))

    def open_player_results(self):
        folder = app.DATA_DIR / 'player_matches'
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == 'win32':
            import os
            os.startfile(str(folder))
        else:
            self.log_message('Папка результатов: ' + str(folder))

    def _poll(self):
        try:
            while True:
                tag, data = self.events.get_nowait()
                if tag == 'player_progress':
                    self.counts_var.set(f'Просмотрено участков: {data}   •   Совпадений: 0')
                elif tag == 'player_hit':
                    image_path, text, conf = data
                    self.root.deiconify()
                    self.root.lift()
                    self.counts_var.set('Возможное совпадение найдено — требуется проверка!')
                    self.current_image = load_image(image_path)
                    self._show_image(self.current_image)
                    self.log_message(f'Найдено похожее имя: {text} (OCR {conf:.0%}). '
                                     'Точные координаты X/Y пока неизвестны.')
                    messagebox.showinfo('Возможное совпадение',
                                        'Похоже, найдено имя ' + text + '. '
                                        'Проверьте поселение в игре и его координаты.')
                elif tag == 'error':
                    self.root.deiconify()
                    self.log_message('Ошибка: ' + str(data))
                    messagebox.showerror('Tracker', str(data))
                elif tag == 'info':
                    self.log_message(str(data))
                elif tag == 'done':
                    pass
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
    app.TrackerApp = PlayerFinderApp
    app.main()
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
