"""Tiles Survive Tracker v6: read-only manual-pan nickname search."""
from __future__ import annotations

import ctypes
import json
import os
import queue
import sys
import threading
import time
import unicodedata
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import Tk, StringVar, messagebox, ttk

import cv2
import numpy as np

TARGET_DEFAULT = 'KingBonbee'
STATE_DEFAULT = '752'
FPS = 8.0
SAMPLE_EVERY = 0.30
MAX_QUEUE = 700
DATA = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'TilesSurviveTracker'
RESULTS = DATA / 'player_search'
LOG = DATA / 'player_search.log'


def normalize(text: str) -> str:
    text = unicodedata.normalize('NFKC', text).casefold()
    text = text.translate(str.maketrans({
        'а': 'a', 'в': 'b', 'с': 'c', 'е': 'e', 'к': 'k', 'м': 'm', 'н': 'h',
        'о': 'o', 'р': 'p', 'т': 't', 'у': 'y', 'х': 'x', 'і': 'i', 'ӏ': 'l',
    }))
    return ''.join(c for c in text if c.isascii() and c.isalnum())


def edit_distance(a: str, b: str, maximum: int = 2) -> int:
    if abs(len(a) - len(b)) > maximum:
        return maximum + 1
    row = list(range(len(b) + 1))
    for i, char in enumerate(a, 1):
        nxt = [i]
        for j, right in enumerate(b, 1):
            nxt.append(min(nxt[-1] + 1, row[j] + 1, row[j-1] + (char != right)))
        row = nxt
        if min(row) > maximum:
            return maximum + 1
    return row[-1]


def match_name(recognized: str, desired: str, confidence: float) -> tuple[bool, float]:
    text, name = normalize(recognized), normalize(desired)
    if len(name) < 4 or not text:
        return False, 0.0
    if name in text and confidence >= 0.25:
        return True, 1.0
    if confidence < 0.52:
        return False, 0.0
    max_edits = 1 if len(name) < 8 else 2
    best = max_edits + 1
    for size in range(max(4, len(name) - max_edits), len(name) + max_edits + 1):
        for start in range(max(0, len(text) - size + 1)):
            best = min(best, edit_distance(name, text[start:start + size], max_edits))
            if best == 0:
                break
    return best <= max_edits, max(0.0, 1.0 - best / len(name))


def select_map_crop(frame: np.ndarray) -> tuple[np.ndarray, int, int]:
    height, width = frame.shape[:2]
    if height < 300 or width < 450:
        return frame, 0, 0
    top, bottom = 54, 104
    right = min(84, width // 16)
    return frame[top:height-bottom, :width-right], 0, top


def clarity(frame: np.ndarray) -> float:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    resized = cv2.resize(gray, (240, 135), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(resized, cv2.CV_64F).var())


def tiny_frame(frame: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (96, 54), interpolation=cv2.INTER_AREA)


def sufficiently_new(small: np.ndarray, previous: np.ndarray | None) -> bool:
    if previous is None:
        return True
    return float(np.mean(cv2.absdiff(small, previous))) >= 2.0


def foreground_window() -> int | None:
    if sys.platform != 'win32':
        return None
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    active = user32.GetForegroundWindow()
    return int(active) if active is not None else None


def find_game() -> int | None:
    from windows_capture import list_game_windows
    windows = list_game_windows()
    return int(windows[0].hwnd) if windows else None


def capture_visible_game(hwnd: int, sct=None) -> np.ndarray | None:
    """Only captures selected game client when it is the foreground window."""
    if foreground_window() != hwnd:
        return None
    from windows_capture import client_bbox
    left, top, right, bottom = client_bbox(hwnd)
    if right <= left or bottom <= top:
        return None
    if sct is not None:
        shot = sct.grab({'left': left, 'top': top, 'width': right-left,
                         'height': bottom-top})
        return np.asarray(shot, dtype=np.uint8)[:, :, :3].copy()
    from PIL import ImageGrab
    rgb = np.asarray(ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert('RGB'))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def ocr_lines(frame: np.ndarray, ocr) -> list[tuple[str, float, np.ndarray]]:
    cropped, xoff, yoff = select_map_crop(frame)
    result = ocr(cropped)
    if result is None or result.txts is None:
        return []
    scores = result.scores if result.scores is not None else [0.0] * len(result.txts)
    boxes = result.boxes if result.boxes is not None else []
    matches = []
    for text, score, points in zip(result.txts, scores, boxes):
        box = np.asarray(points, dtype=np.float32)
        box[:, 0] += xoff
        box[:, 1] += yoff
        matches.append((str(text), float(score), box))
    return matches


def player_hits(lines, name: str):
    hits = []
    for text, confidence, box in lines:
        matched, score = match_name(text, name, confidence)
        if matched:
            hits.append((text, confidence, score, box))
    return hits


def log(text: str) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(f'{datetime.now().isoformat(timespec="seconds")} {text}\n')


class PlayerSearch:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title('Tiles Survive Tracker — поиск игрока')
        self.root.geometry('570x345')
        self.root.minsize(500, 315)
        self.target = StringVar(value=TARGET_DEFAULT)
        self.state = StringVar(value=STATE_DEFAULT)
        self.status = StringVar(value='Откройте карту штата 752 и нажмите «Начать поиск».')
        self.metrics = StringVar(value='Кадров: 0   OCR: 0   Очередь: 0   Находок: 0')
        self.events: queue.Queue = queue.Queue()
        self.pending: queue.Queue = queue.Queue(maxsize=MAX_QUEUE)
        self.capture_stop = threading.Event()
        self.shutdown = threading.Event()
        self.capturer = None
        self.reader = None
        self.active = False
        self.hwnd = None
        self.counts = {'captured': 0, 'processed': 0, 'found': 0, 'skipped': 0, 'texts': 0}
        self.output = None
        self._layout()
        self.root.after(160, self.poll)
        self.root.protocol('WM_DELETE_WINDOW', self.close)

    def _layout(self):
        container = ttk.Frame(self.root, padding=18)
        container.pack(fill='both', expand=True)
        ttk.Label(container, text='Поиск поселения по никнейму',
                  font=('Segoe UI', 16, 'bold')).pack(anchor='w', pady=(0, 8))
        entry_row = ttk.Frame(container)
        entry_row.pack(fill='x', pady=(2, 12))
        ttk.Label(entry_row, text='Игрок').pack(side='left')
        ttk.Entry(entry_row, textvariable=self.target, width=23).pack(side='left', padx=(8, 22))
        ttk.Label(entry_row, text='Штат').pack(side='left')
        ttk.Entry(entry_row, textvariable=self.state, width=8).pack(side='left', padx=8)
        self.go = ttk.Button(container, text='▶ Начать поиск', command=self.toggle)
        self.go.pack(fill='x', ipady=10)
        row = ttk.Frame(container)
        row.pack(fill='x', pady=(12, 6))
        ttk.Button(row, text='Открыть находки', command=self.open_results).pack(side='left')
        ttk.Label(row, text='Карту перемещаете мышью. Игра не управляется Tracker.',
                  wraplength=300, foreground='#56636d').pack(side='right')
        ttk.Separator(container).pack(fill='x', pady=(10, 8))
        ttk.Label(container, textvariable=self.metrics, font=('Segoe UI', 10, 'bold')).pack(anchor='w')
        ttk.Label(container, textvariable=self.status, wraplength=515, justify='left').pack(anchor='w', pady=(8, 4))
        ttk.Label(container,
                  text='Сохраняются только вероятные совпадения.\n'
                       'Это не глобальный поиск по базе игры и не определение точных X/Y.',
                  foreground='#69747b').pack(anchor='w')

    def toggle(self):
        if self.active:
            self.capture_stop.set()
            self.active = False
            self.go.configure(text='▶ Начать поиск')
            self.status.set('Захват остановлен. Оставшиеся кадры проверяются в фоне.')
            return
        if self.capturer and self.capturer.is_alive():
            return
        name = self.target.get().strip()
        if len(normalize(name)) < 4:
            messagebox.showerror('Никнейм', 'Введите ник длиной не менее 4 символов.')
            return
        try:
            hwnd = find_game()
        except Exception as exc:
            messagebox.showerror('Tiles Survive', str(exc))
            return
        if hwnd is None:
            messagebox.showinfo('Игра не найдена', 'Запустите Tiles Survive и откройте карту.')
            return
        self.hwnd = hwnd
        self.capture_stop.clear()
        self.counts = {'captured': 0, 'processed': 0, 'found': 0, 'skipped': 0, 'texts': 0}
        self.pending = queue.Queue(maxsize=MAX_QUEUE)
        self.output = RESULTS / datetime.now().strftime('%Y%m%d_%H%M%S')
        self.output.mkdir(parents=True, exist_ok=True)
        self.active = True
        self.go.configure(text='■ Остановить поиск')
        self.status.set('Переключитесь на игру и двигайте карту. Система собирает чёткие кадры.')
        self.capturer = threading.Thread(target=self.collect, args=(hwnd,), daemon=True)
        self.reader = threading.Thread(target=self.recognize, args=(name,), daemon=True)
        self.reader.start()
        self.capturer.start()
        self.root.after(350, self.root.iconify)

    def collect(self, hwnd: int):
        try:
            try:
                from mss import MSS
                provider = MSS()
            except ImportError:
                provider = None
            prev = None
            best = None
            best_score = -1.0
            window_start = time.monotonic()
            tick = 1.0 / FPS
            while not self.capture_stop.is_set() and not self.shutdown.is_set():
                started = time.monotonic()
                frame = capture_visible_game(hwnd, provider)
                if frame is not None:
                    self.counts['captured'] += 1
                    key = tiny_frame(frame)
                    if sufficiently_new(key, prev):
                        score = clarity(frame)
                        if score > best_score:
                            best, best_score = frame, score
                        prev = key
                    now = time.monotonic()
                    if now - window_start >= SAMPLE_EVERY:
                        if best is not None:
                            ok, compressed = cv2.imencode('.jpg', best, [cv2.IMWRITE_JPEG_QUALITY, 82])
                            if ok:
                                try:
                                    self.pending.put_nowait((now, compressed.tobytes()))
                                except queue.Full:
                                    self.counts['skipped'] += 1
                        best, best_score = None, -1.0
                        window_start = now
                else:
                    if time.monotonic() - window_start >= SAMPLE_EVERY:
                        best, best_score = None, -1.0
                        window_start = time.monotonic()
                self.capture_stop.wait(max(0.001, tick - (time.monotonic() - started)))
        except Exception as exc:
            log(f'Capture failed: {exc!r}')
            self.events.put(('error', f'Ошибка захвата: {exc}'))
        finally:
            self.capture_stop.set()
            self.events.put(('capture_done', None))

    def recognize(self, name: str):
        try:
            from rapidocr import RapidOCR
            engine = RapidOCR()
        except Exception as exc:
            log(f'OCR initialization failed: {exc!r}')
            self.events.put(('error', 'Не удалось запустить OCR. Проверьте tracker-startup.log: ' + str(exc)))
            self.capture_stop.set()
            return
        try:
            while not self.shutdown.is_set():
                try:
                    timestamp, encoded = self.pending.get(timeout=0.4)
                except queue.Empty:
                    if self.capture_stop.is_set() and (not self.capturer or not self.capturer.is_alive()):
                        break
                    continue
                image = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
                try:
                    if image is not None:
                        lines = ocr_lines(image, engine)
                        hits = player_hits(lines, name)
                        self.counts['processed'] += 1
                        self.counts['texts'] += len(lines)
                        if hits:
                            self.save_hits(image, hits, timestamp, name)
                finally:
                    self.pending.task_done()
        except Exception as exc:
            log(f'OCR failed: {exc!r}')
            self.events.put(('error', f'Ошибка распознавания: {exc}'))
            self.capture_stop.set()
        finally:
            self.events.put(('ocr_done', None))

    def save_hits(self, image, hits, timestamp, name):
        self.counts['found'] += len(hits)
        annotated = image.copy()
        data = []
        for text, confidence, similarity, box in hits:
            x0 = max(0, int(np.min(box[:, 0])))
            y0 = max(0, int(np.min(box[:, 1])))
            x1 = min(image.shape[1]-1, int(np.max(box[:, 0])))
            y1 = min(image.shape[0]-1, int(np.max(box[:, 1])))
            cv2.rectangle(annotated, (x0-4, y0-4), (x1+4, y1+4), (0, 30, 255), 3)
            cv2.putText(annotated, 'POSSIBLE MATCH', (x0, max(22, y0-8)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 30, 255), 2)
            data.append({'read_text': text, 'ocr_confidence': confidence,
                         'similarity': similarity, 'bbox_px': [x0, y0, x1, y1]})
        stamp = datetime.now().strftime('%H%M%S_%f')
        file = self.output / f'candidate_{stamp}.png'
        cv2.imencode('.png', annotated)[1].tofile(str(file))
        with (self.output / 'candidates.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'target': name, 'state': self.state.get(),
                                'file': file.name, 'hits': data}, ensure_ascii=False) + '\n')
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except (ImportError, RuntimeError):
            pass
        self.events.put(('found', file))

    def poll(self):
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == 'found':
                    self.root.deiconify()
                    self.root.lift()
                    self.status.set('ВОЗМОЖНОЕ СОВПАДЕНИЕ! Откройте находки и проверьте снимок.')
                elif event == 'error':
                    self.root.deiconify()
                    self.active = False
                    self.go.configure(text='▶ Начать поиск')
                    self.status.set(str(value))
                    messagebox.showerror('Tiles Survive Tracker', str(value))
                elif event == 'ocr_done' and self.capture_stop.is_set():
                    if not self.active:
                        self.status.set('Проверка сохранённых кадров завершена. Найдено: '
                                        + str(self.counts['found']))
        except queue.Empty:
            pass
        self.metrics.set(f"Кадров: {self.counts['captured']}   OCR: {self.counts['processed']}   "
                         f"Надписей: {self.counts['texts']}   Находок: {self.counts['found']}\n"
                         f"Очередь: {self.pending.qsize()}   Пропущено: {self.counts['skipped']}")
        if self.active and self.counts['processed'] >= 15 and self.counts['texts'] == 0:
            self.status.set('OCR не видит надписей. Приблизьте карту на один шаг.')
        if self.active and self.pending.qsize() >= int(MAX_QUEUE * 0.8):
            self.status.set('Очередь почти заполнена. Замедлите прокрутку: OCR не успевает.')
        if not self.shutdown.is_set():
            self.root.after(220, self.poll)

    def open_results(self):
        RESULTS.mkdir(parents=True, exist_ok=True)
        if sys.platform == 'win32':
            os.startfile(str(RESULTS))
        else:
            messagebox.showinfo('Результаты', str(RESULTS))

    def close(self):
        self.capture_stop.set()
        self.shutdown.set()
        self.root.destroy()


def main():
    if sys.platform == 'win32':
        from windows_capture import set_dpi_awareness
        set_dpi_awareness()
    root = Tk()
    app = PlayerSearch(root)
    root.mainloop()


if __name__ == '__main__':
    main()
