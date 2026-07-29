import asyncio
import contextlib
import io
import platform
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import bt_print
import bt_scan

_TASK_DONE = object()


class _QueueWriter(io.TextIOBase):
    def __init__(self, log_queue: "queue.Queue[str]") -> None:
        self.log_queue = log_queue

    def write(self, text: str) -> int:
        if text:
            self.log_queue.put(text)
        return len(text)

    def flush(self) -> None:
        return None

class PrinterGui:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Seznik EON Printer Toolkit")
        self.root.geometry("860x680")
        self.root.minsize(760, 620)

        self.log_queue: queue.Queue[object] = queue.Queue()
        self.is_running = False
        self.is_closing = False

        self.mode_var = tk.StringVar(value="text")
        self.pdf_path_var = tk.StringVar()
        self.image_path_var = tk.StringVar()

        self._build_ui()
        self._refresh_mode_fields()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain_log_queue)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=16)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(4, weight=1)

        top_bar = ttk.Frame(container)
        top_bar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        top_bar.columnconfigure(1, weight=1)

        scan_button = ttk.Button(top_bar, text="Scan", command=self._start_scan)
        scan_button.grid(row=0, column=0, sticky="w")
        self.scan_button = scan_button

        hint = ttk.Label(
            top_bar,
            text="Scan detects the printer and saves config for print actions.",
        )
        hint.grid(row=0, column=1, sticky="w", padx=(12, 0))

        mode_frame = ttk.LabelFrame(container, text="Action", padding=12)
        mode_frame.grid(row=1, column=0, sticky="ew")
        mode_frame.columnconfigure(0, weight=1)

        radios = (
            ("Text", "text"),
            ("PDF", "pdf"),
            ("Image", "image"),
            ("Test Page", "test"),
        )
        for idx, (label, value) in enumerate(radios):
            ttk.Radiobutton(
                mode_frame,
                text=label,
                value=value,
                variable=self.mode_var,
                command=self._refresh_mode_fields,
            ).grid(row=idx, column=0, sticky="w", pady=2)

        input_frame = ttk.LabelFrame(container, text="Input", padding=12)
        input_frame.grid(row=2, column=0, sticky="nsew", pady=(12, 0))
        input_frame.columnconfigure(0, weight=1)
        input_frame.rowconfigure(0, weight=1)

        text_frame = ttk.Frame(input_frame)
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        text_widget = tk.Text(text_frame, height=10, wrap="word")
        text_widget.grid(row=0, column=0, sticky="nsew")
        self.text_widget = text_widget
        self.text_frame = text_frame

        pdf_frame = ttk.Frame(input_frame)
        pdf_frame.columnconfigure(0, weight=1)
        ttk.Entry(pdf_frame, textvariable=self.pdf_path_var).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(pdf_frame, text="Browse PDF", command=self._browse_pdf).grid(
            row=0, column=1, padx=(8, 0)
        )
        ttk.Label(
            pdf_frame,
            text="Warning: PDF content should be 57 mm wide only.",
            foreground="#b45309",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.pdf_frame = pdf_frame

        image_frame = ttk.Frame(input_frame)
        image_frame.columnconfigure(0, weight=1)
        ttk.Entry(image_frame, textvariable=self.image_path_var).grid(
            row=0, column=0, sticky="ew"
        )
        ttk.Button(
            image_frame, text="Browse Image", command=self._browse_image
        ).grid(row=0, column=1, padx=(8, 0))
        self.image_frame = image_frame

        test_frame = ttk.Frame(input_frame)
        ttk.Label(test_frame, text="No additional input needed for test page.").grid(
            row=0, column=0, sticky="w"
        )
        self.test_frame = test_frame

        action_bar = ttk.Frame(container)
        action_bar.grid(row=3, column=0, sticky="ew", pady=(12, 12))
        action_bar.columnconfigure(1, weight=1)

        execute_button = ttk.Button(
            action_bar, text="Start Print", command=self._start_action
        )
        execute_button.grid(row=0, column=0, sticky="w")
        self.execute_button = execute_button

        clear_button = ttk.Button(action_bar, text="Clear Log", command=self._clear_log)
        clear_button.grid(row=0, column=1, sticky="e")

        log_frame = ttk.LabelFrame(container, text="Logger", padding=12)
        log_frame.grid(row=4, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        log_box = scrolledtext.ScrolledText(
            log_frame,
            wrap="word",
            height=18,
            state="disabled",
            font=self._log_font(),
        )
        log_box.grid(row=0, column=0, sticky="nsew")
        self.log_box = log_box

    def _log_font(self) -> tuple[str, int]:
        return ("Consolas", 10)

    def _refresh_mode_fields(self) -> None:
        for frame in (self.text_frame, self.pdf_frame, self.image_frame, self.test_frame):
            frame.grid_forget()

        selected = self.mode_var.get()
        if selected == "text":
            self.text_frame.grid(row=0, column=0, sticky="nsew")
        elif selected == "pdf":
            self.pdf_frame.grid(row=0, column=0, sticky="ew")
        elif selected == "image":
            self.image_frame.grid(row=0, column=0, sticky="ew")
        else:
            self.test_frame.grid(row=0, column=0, sticky="w")

    def _browse_pdf(self) -> None:
        path = filedialog.askopenfilename(
            title="Select PDF",
            filetypes=[("PDF Files", "*.pdf")],
        )
        if path:
            self.pdf_path_var.set(path)

    def _browse_image(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Image",
            filetypes=[
                ("Image Files", "*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp"),
                ("PNG", "*.png"),
                ("JPEG", "*.jpg;*.jpeg"),
                ("Bitmap", "*.bmp"),
                ("GIF", "*.gif"),
                ("WebP", "*.webp"),
            ],
        )
        if path:
            self.image_path_var.set(path)

    def _clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _append_log(self, text: str) -> None:
        if self.is_closing or not self.root.winfo_exists():
            return
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _drain_log_queue(self) -> None:
        if self.is_closing or not self.root.winfo_exists():
            return
        while True:
            try:
                chunk = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if chunk is _TASK_DONE:
                self.is_running = False
                self._set_busy(False)
                continue
            self._append_log(str(chunk))
        self.root.after(100, self._drain_log_queue)

    def _on_close(self) -> None:
        self.is_closing = True
        self.root.destroy()

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.scan_button.configure(state=state)
        self.execute_button.configure(state=state)

    def _start_scan(self) -> None:
        self._run_task(["bt_scan.py", "--save"], "Scan")

    def _start_action(self) -> None:
        mode = self.mode_var.get()

        if mode == "text":
            text = self.text_widget.get("1.0", "end").strip()
            if not text:
                messagebox.showerror("Missing Text", "Enter text to print.")
                return
            argv = ["bt_print.py", "--print-text", text]
            label = "Print Text"
        elif mode == "pdf":
            pdf_path = self.pdf_path_var.get().strip()
            if not pdf_path:
                messagebox.showerror("Missing PDF", "Choose a PDF file.")
                return
            argv = ["bt_print.py", "--print-pdf", pdf_path]
            label = "Print PDF"
        elif mode == "image":
            image_path = self.image_path_var.get().strip()
            if not image_path:
                messagebox.showerror("Missing Image", "Choose an image file.")
                return
            argv = ["bt_print.py", "--print-image", image_path]
            label = "Print Image"
        else:
            argv = ["bt_print.py", "--test-page"]
            label = "Print Test Page"

        self._run_task(argv, label)

    def _run_task(self, argv: list[str], label: str) -> None:
        if self.is_running:
            messagebox.showwarning(
                "Busy",
                "A command is already running. Wait for it to finish.",
            )
            return

        self._set_busy(True)
        self.is_running = True
        self.log_queue.put(f"\n=== {label} ===\n")
        self.log_queue.put(f"$ {self._format_command(argv)}\n\n")

        worker = threading.Thread(
            target=self._task_worker,
            args=(argv,),
            daemon=True,
        )
        worker.start()

    def _format_command(self, argv: list[str]) -> str:
        return subprocess.list2cmdline(argv)

    def _task_worker(self, argv: list[str]) -> None:
        writer = _QueueWriter(self.log_queue)
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                if argv[0] == "bt_scan.py":
                    code = asyncio.run(bt_scan.main(argv[1:]))
                else:
                    code = asyncio.run(bt_print.main(argv[1:]))
            self.log_queue.put(f"\n[exit code: {0 if code is None else code}]\n")
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            self.log_queue.put(f"\n[exit code: {code}]\n")
        except Exception as exc:
            self.log_queue.put(f"\n[error] {exc}\n")
        finally:
            self.log_queue.put(_TASK_DONE)


def main() -> None:
    root = tk.Tk()
    gui = PrinterGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
