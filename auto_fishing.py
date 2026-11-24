"""Pixel-based fishing assistant. Windows uses the standard library.
macOS: in a project venv install mss and pyobjc-framework-Quartz, then grant Screen Recording
and Accessibility to the terminal or Python host. Run with python3 auto_fishing.py.
"""
import argparse
import ctypes as c
import faulthandler
import queue
from ctypes import wintypes as w
import sys
import threading
import time
import tkinter as tk


def runs(flags):
    start = None
    for index, flag in enumerate(flags):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            yield start, index
            start = None
    if start is not None:
        yield start, len(flags)


def purple(b, g, r):
    return b >= 86 and b > r + 34 and r >= g + 13 and g < 100


def locate(data, width, height):
    # The rail is visible above and below the inset targets. Find a wide,
    # nearly solid purple row, bridging the white moving marker.
    best = None
    for y in range(2, height - 5):
        row = data[y * width * 4:(y + 1) * width * 4]
        mask = [purple(*row[i:i+3]) for i in range(0, len(row), 4)]
        merged = []
        for left, right in runs(mask):
            if merged and left - merged[-1][1] <= max(8, width * .055):
                merged[-1] = merged[-1][0], right
            else:
                merged.append((left, right))
        for left, right in merged:
            span = right - left
            if span >= max(85, width * .46) and (best is None or span > best[0]):
                sample = range(left + span//10, right - span//10, max(1,span//36))
                def rail_row(row):
                    offset = row * width * 4
                    return sum(purple(*data[offset+x*4:offset+x*4+3]) for x in sample) >= len(sample)*.36

                top = bottom = y
                while top > 0 and rail_row(top-1):
                    top -= 1
                while bottom < height and rail_row(bottom):
                    bottom += 1
                if 12 <= bottom-top <= min(105,span*.29):
                    best = span,(left,right,top,bottom)
    return best[1] if best else None


def read_track(data, width, height, rail):
    left, right, top, bottom = rail
    mid = (top + bottom) // 2
    y_values = [top + (bottom-top)//5, mid, bottom - (bottom-top)//5 - 1]
    white, blue, violet = [], [], 0
    for x in range(left, right):
        pixels = [data[(y*width+x)*4:(y*width+x)*4+3] for y in y_values]
        white.append(all(min(p) > 205 and max(p)-min(p) < 45 for p in pixels))
        b, g, r = pixels[1]
        blue.append(b > 145 and g > 106 and b > g + 14 and g > r + 30)
        violet += purple(*pixels[1])
    span = right-left
    if violet < span * .34:
        return None
    markers = [(a,b) for a,b in runs(white) if 1 <= b-a <= max(4,span*.04)]
    if len(markers) != 1:
        return None
    a,b = markers[0]
    x = left + (a+b-1)/2
    # The white marker and its dark outline can split a blue target.
    zones = []
    for start,end in runs(blue):
        if zones and start-zones[-1][1] <= b-a+max(4,span*.015) and zones[-1][1]-3 <= a <= start:
            zones[-1] = zones[-1][0], end
        else:
            zones.append((start,end))
    return x, [(left+a,left+b) for a,b in zones if span*.035 <= b-a <= span*.24]


class WindowsCapture:
    def __init__(self, roi):
        self.x,self.y,self.width,self.height = roi
        self.user = c.windll.user32
        self.gdi = c.windll.gdi32
        for dll, name, args in [
            (self.user,'GetDC',[w.HWND]),
            (self.gdi,'CreateCompatibleDC',[w.HDC]),
            (self.gdi,'CreateCompatibleBitmap',[w.HDC,c.c_int,c.c_int]),
            (self.gdi,'SelectObject',[w.HDC,w.HANDLE]),
        ]:
            fn = getattr(dll,name)
            fn.argtypes, fn.restype = args, w.HANDLE
        self.gdi.BitBlt.argtypes = [w.HDC,c.c_int,c.c_int,c.c_int,c.c_int,w.HDC,c.c_int,c.c_int,w.DWORD]
        self.gdi.GetDIBits.argtypes = [w.HDC,w.HANDLE,w.UINT,w.UINT,c.c_void_p,c.c_void_p,w.UINT]
        self.user.ReleaseDC.argtypes = [w.HWND,w.HDC]
        self.gdi.DeleteDC.argtypes = [w.HDC]
        self.gdi.DeleteObject.argtypes = [w.HANDLE]
        self.screen = self.user.GetDC(None)
        self.memory = self.gdi.CreateCompatibleDC(self.screen)
        self.bitmap = self.gdi.CreateCompatibleBitmap(self.screen,self.width,self.height)
        self.info = (c.c_int32*10)(40,self.width,-self.height,(32<<16)|1,0)
        self.buffer = c.create_string_buffer(self.width*self.height*4)

    def grab(self):
        old = self.gdi.SelectObject(self.memory,self.bitmap)
        ok = self.gdi.BitBlt(self.memory,0,0,self.width,self.height,self.screen,self.x,self.y,0x00CC0020)
        self.gdi.SelectObject(self.memory,old)
        if not ok or not self.gdi.GetDIBits(self.memory,self.bitmap,0,self.height,self.buffer,self.info,0):
            raise RuntimeError('Windows screen capture failed')
        return self.buffer.raw,self.width,self.height

    def strike(self):
        self.user.keybd_event(0x20,0x39,0,0)
        self.user.keybd_event(0x20,0x39,2,0)

    def close(self):
        self.gdi.DeleteObject(self.bitmap)
        self.gdi.DeleteDC(self.memory)
        self.user.ReleaseDC(None,self.screen)


class MacCapture:
    def __init__(self,roi):
        from mss import MSS
        import Quartz

        if not Quartz.CGPreflightScreenCaptureAccess():
            raise RuntimeError('Cấp quyền Screen Recording rồi mở lại ứng dụng chạy Python')
        if not Quartz.CGPreflightPostEventAccess():
            raise RuntimeError('Cấp quyền Accessibility rồi mở lại ứng dụng chạy Python')
        self.quartz = Quartz
        self.roi = dict(zip(('left','top','width','height'),roi))
        self.screen = MSS()

    def grab(self):
        shot = self.screen.grab(self.roi)
        return shot.bgra,shot.width,shot.height

    def strike(self):
        for down in (True, False):
            event = self.quartz.CGEventCreateKeyboardEvent(None, 49, down)
            if event is None:
                raise RuntimeError('macOS không tạo được sự kiện Space')
            self.quartz.CGEventPost(self.quartz.kCGHIDEventTap, event)

    def close(self):
        self.screen.close()


def capture_steps(roi, lead, stop, report):
    capture = (WindowsCapture if sys.platform == 'win32' else MacCapture)(roi)
    rail = previous = pending = None
    observed = last_marker = last_press = 0
    presses = 0
    try:
        while not stop.is_set():
            pixels,width,height = capture.grab()
            now = time.perf_counter()
            if rail is None and now-observed >= .055:
                observed = now
                rail = locate(pixels,width,height)
            result = read_track(pixels,width,height,rail) if rail else None
            if result is None:
                if now-last_marker > .10:
                    rail = previous = pending = None
                yield 6
                continue
            x,zones = result
            last_marker = now
            if pending:
                center, sent, original_count = pending
                if len(zones) < original_count and not any(a <= center <= b for a,b in zones):
                    pending = None
                elif now-sent > .2 and abs(center-x) > (rail[1]-rail[0])*.10:
                    pending = None
            if previous and x != previous[0]:
                old_x, old_t = previous
                dt = now-old_t
                if .003 <= dt <= .12 and not pending and now-last_press > .065:
                    delta = x-old_x
                    span = rail[1]-rail[0]
                    # Cap extrapolation to one plausible display step.
                    predicted = x + max(-span*.06,min(span*.06,delta/dt*lead))
                    for a,b in zones:
                        margin = max(2,(b-a)*.20)
                        if a+1 <= x <= b-1 and a+margin <= predicted <= b-margin:
                            if stop.is_set():
                                return
                            capture.strike()
                            last_press = now
                            pending = ((a+b)/2,now,len(zones))
                            presses += 1
                            report(f'Space: {presses}')
                            break
            if not previous or x != previous[0] or now-previous[1] > .05:
                previous = x,now
            yield 4
    finally:
        capture.close()


def work(roi, lead, stop, report):
    steps = capture_steps(roi, lead, stop, report)
    try:
        for delay_ms in steps:
            if stop.wait(delay_ms / 1000):
                break
    finally:
        steps.close()


def gui(lead):
    root = tk.Tk()
    root.title('Auto Fishing')
    root.attributes('-topmost',True)
    status = tk.StringVar(value='Chọn vùng quanh thanh tím')
    roi = None
    stop = threading.Event()
    stop.set()
    thread = None
    mac_steps = None
    scheduled = None
    messages = queue.SimpleQueue()
    on_mac = sys.platform == 'darwin'

    def refresh_status():
        try:
            while True:
                token, message = messages.get_nowait()
                if token is stop and not token.is_set():
                    status.set(message)
        except queue.Empty:
            pass
        root.after(35, refresh_status)

    def mac_tick():
        nonlocal mac_steps, scheduled
        scheduled = None
        try:
            scheduled = root.after(next(mac_steps), mac_tick)
        except StopIteration:
            mac_steps = None
        except Exception as error:
            mac_steps = None
            status.set(str(error))

    def run():
        nonlocal stop, thread, mac_steps, scheduled
        if not roi or mac_steps is not None or (thread and thread.is_alive()):
            return
        stop = threading.Event()
        cancel = stop
        status.set('Chuyển focus về game trong 2 giây')
        if on_mac:
            mac_steps = capture_steps(roi, lead, cancel, status.set)
            scheduled = root.after(2000, mac_tick)
            return

        def report(message):
            messages.put((cancel, message))

        def background():
            if cancel.wait(2):
                return
            try:
                work(roi, lead, cancel, report)
            except Exception as error:
                report(str(error))

        thread = threading.Thread(target=background, daemon=True)
        thread.start()

    def pause():
        nonlocal mac_steps, scheduled
        stop.set()
        if scheduled is not None:
            root.after_cancel(scheduled)
            scheduled = None
        if mac_steps is not None:
            mac_steps.close()
            mac_steps = None
        status.set('Đã dừng')

    def select():
        nonlocal roi
        pause()
        overlay = tk.Toplevel(root)
        if on_mac:
            # Native fullscreen switches Spaces and can hide the browser.
            overlay.withdraw()
            overlay.overrideredirect(True)
            overlay.geometry(f'{root.winfo_screenwidth()}x{root.winfo_screenheight()}+0+0')
        else:
            overlay.attributes('-fullscreen',True)
        overlay.attributes('-topmost',True)
        overlay.attributes('-alpha',.22 if on_mac else .32)
        canvas = tk.Canvas(overlay,bg='#111c2d',highlightthickness=0,cursor='crosshair')
        canvas.pack(fill='both',expand=True)
        if on_mac:
            overlay.deiconify()
        overlay.lift()
        overlay.focus_force()
        begin = None
        mark = None
        def down(event):
            nonlocal begin,mark
            begin = (event.x_root,event.y_root)
            mark = canvas.create_rectangle(event.x,event.y,event.x,event.y,outline='cyan',width=3)
        def move(event):
            if begin:
                canvas.coords(mark,begin[0]-canvas.winfo_rootx(),begin[1]-canvas.winfo_rooty(),event.x,event.y)
        def release(event):
            nonlocal roi
            if not begin:
                return
            x,y = begin
            u,v = event.x_root,event.y_root
            overlay.destroy()
            if abs(x-u) >= 100 and abs(y-v) >= 24:
                roi = min(x,u),min(y,v),abs(x-u),abs(y-v)
                status.set(f'Vùng: {roi[2]} × {roi[3]}')
            else:
                status.set('Vùng quá nhỏ, kéo lại quanh thanh tím')
        for event,callback in [('<Button-1>',down),('<B1-Motion>',move),('<ButtonRelease-1>',release)]:
            canvas.bind(event,callback)
        overlay.bind('<Escape>',lambda _: overlay.destroy())

    tk.Label(root,textvariable=status,width=43,pady=12).pack()
    buttons = tk.Frame(root,pady=8)
    buttons.pack()
    for label,action in [('Chọn vùng',select),('Chạy',run),('Dừng',pause)]:
        tk.Button(buttons,text=label,command=action).pack(side='left',padx=4)
    root.protocol('WM_DELETE_WINDOW', lambda: (pause(), root.destroy()))
    root.after(35, refresh_status)
    root.mainloop()


def main():
    faulthandler.enable()
    if sys.platform == 'win32':
        try:
            c.windll.user32.SetProcessDpiAwarenessContext(c.c_void_p(-4))
        except AttributeError:
            c.windll.user32.SetProcessDPIAware()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lead-ms',type=float,default=8)
    parser.add_argument('--roi',type=int,nargs=4,metavar=('X','Y','W','H'))
    args = parser.parse_args()
    if not 0 <= args.lead_ms <= 40:
        parser.error('--lead-ms must be 0–40')
    if args.roi:
        if args.roi[2] < 100 or args.roi[3] < 24:
            parser.error('ROI must be at least 100×24')
        work(args.roi,args.lead_ms/1000,threading.Event(),lambda s: print(s,flush=True))
    else:
        gui(args.lead_ms/1000)


if __name__ == '__main__':
    main()
