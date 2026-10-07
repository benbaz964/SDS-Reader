"""
SDS Reader - desktop app.

A point-and-click front end over the same engine cli.py uses, built so it
can be frozen into a single Windows .exe (see build/sds_reader.spec) for PCs
where Python can't be installed or run.

Workflow:
  1. Add SDS PDFs (individual files and/or whole folders).
  2. Choose the output template - the bundled default, or your own .docx /
     .txt / .md file containing {{placeholders}}. Your choice is remembered.
  3. Choose an output folder and press "Process".
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sds_reader import pipeline, schema, __version__  # noqa: E402

APP_NAME = "SDS Reader"

# ---------------------------------------------------------------------------
# Palette - kept in one place so the app matches the website.
# ---------------------------------------------------------------------------
BG = "#f4f6f8"
CARD = "#ffffff"
INK = "#14202b"
MUTED = "#5b6875"
LINE = "#dde3e9"
ACCENT = "#0f7a5c"
ACCENT_DARK = "#0b5e47"
ACCENT_SOFT = "#e5f3ee"
WARN = "#b25a00"
ERROR = "#b42318"
HEADER = "#0f1a24"

FONT = "Segoe UI"


# ---------------------------------------------------------------------------
# Settings (remembered between runs)
# ---------------------------------------------------------------------------

def _settings_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME, "settings.json")


def load_settings() -> dict:
    try:
        with open(_settings_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data: dict):
    try:
        os.makedirs(os.path.dirname(_settings_path()), exist_ok=True)
        with open(_settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass  # settings are a convenience, never a reason to fail


def open_path(path: str):
    try:
        os.startfile(path)  # type: ignore[attr-defined]  # Windows
    except AttributeError:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class SDSReaderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("980x760")
        self.minsize(820, 640)
        self.configure(bg=BG)
        self._set_icon()

        self.settings = load_settings()
        self.pdfs: list = []
        self.messages: "queue.Queue" = queue.Queue()
        self.worker = None

        template = self.settings.get("template")
        self.use_custom = tk.BooleanVar(value=bool(template and os.path.isfile(template)))
        self.template_path = tk.StringVar(value=template if self.use_custom.get() else "")
        self.outdir = tk.StringVar(value=self.settings.get("outdir")
                                   or os.path.join(os.path.expanduser("~"), "Documents", "SDS Reader Output"))
        self.recursive = tk.BooleanVar(value=self.settings.get("recursive", False))
        self.write_json = tk.BooleanVar(value=self.settings.get("write_json", False))
        self.write_csv = tk.BooleanVar(value=self.settings.get("write_csv", True))
        self.open_when_done = tk.BooleanVar(value=self.settings.get("open_when_done", True))
        self.use_llm = tk.BooleanVar(value=False)
        self.llm_model = tk.StringVar(value=self.settings.get("llm_model", pipeline.llm_reconcile.DEFAULT_MODEL))

        self._style()
        self._build()
        self._refresh_template_status()
        self.after(100, self._drain_messages)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----- look & feel ---------------------------------------------------

    def _set_icon(self):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        ico = os.path.join(base, "build", "icon.ico")
        if os.path.isfile(ico):
            try:
                self.iconbitmap(ico)
            except tk.TclError:
                pass

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=INK, font=(FONT, 10), bordercolor=LINE)
        s.configure("Card.TFrame", background=CARD, relief="flat")
        s.configure("Card.TLabel", background=CARD, foreground=INK)
        s.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9))
        s.configure("Step.TLabel", background=CARD, foreground=ACCENT, font=(FONT, 9, "bold"))
        s.configure("CardTitle.TLabel", background=CARD, foreground=INK, font=(FONT, 12, "bold"))
        s.configure("Status.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9))
        s.configure("Card.TCheckbutton", background=CARD, foreground=INK)
        s.map("Card.TCheckbutton", background=[("active", CARD)])
        s.configure("Card.TRadiobutton", background=CARD, foreground=INK)
        s.map("Card.TRadiobutton", background=[("active", CARD)])

        s.configure("TButton", padding=(12, 6), background=CARD, foreground=INK,
                    bordercolor=LINE, lightcolor=CARD, darkcolor=CARD, focuscolor=CARD)
        s.map("TButton", background=[("active", "#eef2f5"), ("disabled", CARD)],
              foreground=[("disabled", "#a7b0b9")])
        s.configure("Accent.TButton", padding=(22, 10), background=ACCENT, foreground="white",
                    bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT, focuscolor=ACCENT,
                    font=(FONT, 11, "bold"))
        s.map("Accent.TButton", background=[("active", ACCENT_DARK), ("disabled", "#9cc9ba")],
              foreground=[("disabled", "white")])
        s.configure("Link.TButton", padding=(0, 0), background=CARD, foreground=ACCENT,
                    borderwidth=0, relief="flat", focuscolor=CARD, lightcolor=CARD, darkcolor=CARD,
                    font=(FONT, 9, "underline"))
        s.map("Link.TButton", background=[("active", CARD)], foreground=[("active", ACCENT_DARK)])

        s.configure("TEntry", fieldbackground="white", bordercolor=LINE, lightcolor=LINE, padding=5)
        s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor="#e6ebef",
                    bordercolor="#e6ebef", lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)
        s.configure("Treeview", background="white", fieldbackground="white", foreground=INK,
                    bordercolor=LINE, rowheight=26, font=(FONT, 10))
        s.configure("Treeview.Heading", background="#f0f3f6", foreground=MUTED,
                    font=(FONT, 9, "bold"), bordercolor=LINE, relief="flat")
        s.map("Treeview", background=[("selected", ACCENT_SOFT)], foreground=[("selected", INK)])

    def _card(self, parent, step: str, title: str, subtitle: str):
        outer = tk.Frame(parent, bg=LINE)  # 1px border
        card = ttk.Frame(outer, style="Card.TFrame", padding=(18, 14))
        card.pack(fill="both", expand=True, padx=1, pady=1)
        head = ttk.Frame(card, style="Card.TFrame")
        head.pack(fill="x")
        ttk.Label(head, text=step.upper(), style="Step.TLabel").pack(anchor="w")
        ttk.Label(head, text=title, style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(head, text=subtitle, style="Muted.TLabel").pack(anchor="w", pady=(0, 10))
        return outer, card

    # ----- layout --------------------------------------------------------

    def _build(self):
        header = tk.Frame(self, bg=HEADER, height=64)
        header.pack(fill="x")
        mark = tk.Canvas(header, width=34, height=34, bg=HEADER, highlightthickness=0)
        mark.create_polygon(17, 2, 32, 17, 17, 32, 2, 17, fill=ACCENT, outline="")
        mark.create_text(17, 17, text="!", fill="white", font=(FONT, 13, "bold"))
        mark.pack(side="left", padx=(20, 10), pady=14)
        tk.Label(header, text=APP_NAME, bg=HEADER, fg="white", font=(FONT, 15, "bold")).pack(side="left")
        tk.Label(header, text="Offline safety data sheet extraction", bg=HEADER, fg="#8fa1b2",
                 font=(FONT, 10)).pack(side="left", padx=12, pady=(4, 0))
        tk.Label(header, text=f"v{__version__}  ·  100% offline", bg=HEADER, fg="#8fa1b2",
                 font=(FONT, 9)).pack(side="right", padx=20)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=20, pady=16)
        body.columnconfigure(0, weight=3, uniform="c")
        body.columnconfigure(1, weight=2, uniform="c")
        body.rowconfigure(0, weight=1)

        # --- Step 1: PDFs ---
        outer, card = self._card(body, "Step 1", "Safety data sheets",
                                 "Add SDS PDFs one by one or a whole folder at a time.")
        outer.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 10))
        btns = ttk.Frame(card, style="Card.TFrame")
        btns.pack(fill="x", pady=(0, 8))
        ttk.Button(btns, text="Add PDFs…", command=self._add_files).pack(side="left")
        ttk.Button(btns, text="Add folder…", command=self._add_folder).pack(side="left", padx=6)
        ttk.Button(btns, text="Remove", command=self._remove_selected).pack(side="left")
        ttk.Button(btns, text="Clear", command=self._clear).pack(side="left", padx=6)
        ttk.Checkbutton(btns, text="Include subfolders", variable=self.recursive,
                        style="Card.TCheckbutton").pack(side="right")

        list_wrap = ttk.Frame(card, style="Card.TFrame")
        list_wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(list_wrap, columns=("file", "status"), show="headings", selectmode="extended")
        self.tree.heading("file", text="File")
        self.tree.heading("status", text="Status")
        self.tree.column("file", width=330, anchor="w")
        self.tree.column("status", width=150, anchor="w")
        self.tree.tag_configure("done", foreground=ACCENT)
        self.tree.tag_configure("warn", foreground=WARN)
        self.tree.tag_configure("error", foreground=ERROR)
        sb = ttk.Scrollbar(list_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._open_selected_output)
        self.count_label = ttk.Label(card, text="No files added yet.", style="Status.TLabel")
        self.count_label.pack(anchor="w", pady=(8, 0))

        # --- Step 2: template ---
        outer, card = self._card(body, "Step 2", "Output template",
                                 "Each SDS is written into this document layout.")
        outer.grid(row=0, column=1, sticky="nsew", pady=(0, 10))
        ttk.Radiobutton(card, text="Built-in SDS summary template", value=False, variable=self.use_custom,
                        command=self._refresh_template_status, style="Card.TRadiobutton").pack(anchor="w")
        ttk.Radiobutton(card, text="My own template (.docx, .txt, .md)", value=True, variable=self.use_custom,
                        command=self._on_custom_selected, style="Card.TRadiobutton").pack(anchor="w", pady=(4, 0))
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", pady=(6, 0))
        self.template_entry = ttk.Entry(row, textvariable=self.template_path, state="readonly")
        self.template_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Upload…", command=self._choose_template).pack(side="left", padx=(6, 0))
        self.template_status = tk.Label(card, text="", bg=CARD, fg=MUTED, font=(FONT, 9),
                                        justify="left", anchor="w", wraplength=330)
        self.template_status.pack(fill="x", pady=(8, 4))
        links = ttk.Frame(card, style="Card.TFrame")
        links.pack(fill="x")
        ttk.Button(links, text="Save built-in template to edit", style="Link.TButton",
                   command=self._export_default_template).pack(side="left")
        ttk.Button(links, text="Placeholder list", style="Link.TButton",
                   command=self._show_placeholders).pack(side="left", padx=14)

        # --- Step 3: output ---
        outer, card = self._card(body, "Step 3", "Output", "Where to save the filled documents.")
        outer.grid(row=1, column=1, sticky="nsew")
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.outdir).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", command=self._choose_outdir).pack(side="left", padx=(6, 0))
        opts = ttk.Frame(card, style="Card.TFrame")
        opts.pack(fill="x", pady=(8, 0))
        ttk.Checkbutton(opts, text="Combined spreadsheet (CSV) for the batch", variable=self.write_csv,
                        style="Card.TCheckbutton").pack(anchor="w")
        ttk.Checkbutton(opts, text="Also save extracted data as JSON", variable=self.write_json,
                        style="Card.TCheckbutton").pack(anchor="w")
        ttk.Checkbutton(opts, text="Open output folder when finished", variable=self.open_when_done,
                        style="Card.TCheckbutton").pack(anchor="w")
        llm_row = ttk.Frame(opts, style="Card.TFrame")
        llm_row.pack(anchor="w", fill="x")
        ttk.Checkbutton(llm_row, text="Advanced: local Ollama AI check, model", variable=self.use_llm,
                        style="Card.TCheckbutton").pack(side="left")
        ttk.Entry(llm_row, textvariable=self.llm_model, width=14).pack(side="left", padx=(4, 0))

        # --- Footer: run + progress + log ---
        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", padx=20, pady=(0, 16))
        self.run_btn = ttk.Button(foot, text="Process", style="Accent.TButton", command=self._run)
        self.run_btn.pack(side="right")
        prog = tk.Frame(foot, bg=BG)
        prog.pack(side="left", fill="x", expand=True, padx=(0, 16))
        self.progress_label = tk.Label(prog, text="Ready.", bg=BG, fg=MUTED, font=(FONT, 9), anchor="w")
        self.progress_label.pack(fill="x")
        self.progress = ttk.Progressbar(prog, mode="determinate", style="Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(4, 0))

        log_wrap = tk.Frame(self, bg=LINE)
        log_wrap.pack(fill="both", padx=20, pady=(0, 18))
        self.log = tk.Text(log_wrap, height=7, bg="#0f1a24", fg="#cfd8e1", insertbackground="white",
                           font=("Consolas", 9), relief="flat", padx=10, pady=8, wrap="word")
        self.log.pack(fill="both", expand=True, padx=1, pady=1)
        self.log.tag_configure("ok", foreground="#6fd3a8")
        self.log.tag_configure("warn", foreground="#f3b562")
        self.log.tag_configure("err", foreground="#ff8a80")
        self._log("Add SDS PDFs, pick a template, then press Process. Nothing leaves this computer.")

    # ----- PDF list ------------------------------------------------------

    def _add_paths(self, paths):
        new = pipeline.find_pdfs_in(list(paths), recursive=self.recursive.get())
        known = {os.path.normcase(os.path.abspath(p)) for p in self.pdfs}
        added = 0
        for p in new:
            key = os.path.normcase(os.path.abspath(p))
            if key in known:
                continue
            known.add(key)
            self.pdfs.append(p)
            self.tree.insert("", "end", iid=key, values=(os.path.basename(p), "Ready"))
            added += 1
        if paths and not added and not new:
            self._log("No PDF files found there.", "warn")
        self._update_count()

    def _add_files(self):
        paths = filedialog.askopenfilenames(title="Choose SDS PDFs", filetypes=[("PDF files", "*.pdf")],
                                            initialdir=self.settings.get("last_input_dir"))
        if paths:
            self.settings["last_input_dir"] = os.path.dirname(paths[0])
            self._add_paths(paths)

    def _add_folder(self):
        folder = filedialog.askdirectory(title="Choose a folder of SDS PDFs",
                                         initialdir=self.settings.get("last_input_dir"))
        if folder:
            self.settings["last_input_dir"] = folder
            self._add_paths([folder])

    def _remove_selected(self):
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.pdfs = [p for p in self.pdfs if os.path.normcase(os.path.abspath(p)) != iid]
        self._update_count()

    def _clear(self):
        self.tree.delete(*self.tree.get_children())
        self.pdfs = []
        self._update_count()

    def _update_count(self):
        n = len(self.pdfs)
        self.count_label.configure(text="No files added yet." if not n else
                                   f"{n} PDF{'s' if n != 1 else ''} ready. Double-click a processed file to open its output.")

    def _set_row(self, path, status, tag=""):
        iid = os.path.normcase(os.path.abspath(path))
        if self.tree.exists(iid):
            self.tree.item(iid, values=(os.path.basename(path), status), tags=(tag,) if tag else ())

    def _open_selected_output(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        out = getattr(self, "_outputs", {}).get(sel[0])
        if out and os.path.isfile(out):
            open_path(out)

    # ----- template ------------------------------------------------------

    def _on_custom_selected(self):
        if not self.template_path.get():
            self._choose_template()
        self._refresh_template_status()

    def _choose_template(self):
        path = filedialog.askopenfilename(
            title="Upload your output template",
            filetypes=[("Templates", "*.docx *.txt *.md"), ("Word document", "*.docx"),
                       ("Text", "*.txt *.md")],
            initialdir=os.path.dirname(self.template_path.get()) if self.template_path.get() else None)
        if path:
            self.template_path.set(path)
            self.use_custom.set(True)
        elif not self.template_path.get():
            self.use_custom.set(False)
        self._refresh_template_status()

    def _active_template(self):
        if self.use_custom.get() and self.template_path.get():
            return self.template_path.get()
        return pipeline.DEFAULT_TEMPLATE

    def _refresh_template_status(self):
        if not self.use_custom.get():
            self.template_status.configure(
                text="Using the built-in summary layout (overview, ingredient breakdown, PPE, "
                     "first aid, fire, spillage…).", fg=MUTED)
            return
        path = self.template_path.get()
        if not path:
            self.template_status.configure(text="Upload a template to use it.", fg=WARN)
            return
        if not os.path.isfile(path):
            self.template_status.configure(text="That template file can't be found any more.", fg=ERROR)
            return
        try:
            chk = pipeline.check_template(path)
        except Exception as e:
            self.template_status.configure(text=f"Couldn't read this template: {e}", fg=ERROR)
            return
        if not chk.ok:
            self.template_status.configure(
                text="No {{placeholders}} found. Add fields such as {{product_name}} where values "
                     "should go. See the placeholder list.", fg=WARN)
            return
        n = len(chk.used) + len(chk.row_fields)
        msg = f"✓ {n} placeholder{'s' if n != 1 else ''} recognised"
        if chk.row_fields:
            msg += " (incl. an ingredient table)"
        color = ACCENT
        if chk.unknown:
            shown = ", ".join(chk.unknown[:4]) + ("…" if len(chk.unknown) > 4 else "")
            msg += f"\n⚠ Not recognised, will be left as-is: {shown}"
            color = WARN
        self.template_status.configure(text=msg, fg=color)

    def _export_default_template(self):
        dest = filedialog.asksaveasfilename(title="Save the built-in template", defaultextension=".docx",
                                            initialfile="SDS summary template.docx",
                                            filetypes=[("Word document", "*.docx")])
        if not dest:
            return
        shutil.copyfile(pipeline.DEFAULT_TEMPLATE, dest)
        self._log(f"Saved the built-in template to {dest}. Edit it in Word, then upload it in Step 2.", "ok")
        open_path(dest)

    def _show_placeholders(self):
        win = tk.Toplevel(self)
        win.title("Template placeholders")
        win.geometry("560x600")
        win.configure(bg=CARD)
        win.transient(self)
        frame = ttk.Frame(win, style="Card.TFrame", padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Template placeholders", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(frame, wraplength=520, justify="left", style="Muted.TLabel",
                  text="Type these into your Word/text template exactly as shown, including the double "
                       "curly braces. Double-click a row to copy it. For a table that grows to one row per "
                       "ingredient, put the {{row:substance_breakdown.…}} placeholders in a single table row."
                  ).pack(anchor="w", pady=(2, 8))
        search = tk.StringVar()
        ttk.Entry(frame, textvariable=search).pack(fill="x", pady=(0, 8))
        wrap = ttk.Frame(frame, style="Card.TFrame")
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=("ph", "src"), show="headings")
        tree.heading("ph", text="Placeholder")
        tree.heading("src", text="Comes from")
        tree.column("ph", width=330)
        tree.column("src", width=170)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        section_of = {f.var: f"SDS section {f.section}" for f in schema.ALL_FIELDS}
        rows = [(f"{{{{{n}}}}}", section_of.get(n, "Computed summary")) for n in pipeline.known_placeholders()]
        rows += [(f"{{{{row:substance_breakdown.{c}}}}}", "Ingredient table row")
                 for c in pipeline.SUBSTANCE_BREAKDOWN_COLUMNS]

        def refill(*_):
            q = search.get().lower().strip()
            tree.delete(*tree.get_children())
            for ph, src in rows:
                if not q or q in ph.lower() or q in src.lower():
                    tree.insert("", "end", values=(ph, src))
        search.trace_add("write", refill)
        refill()

        def copy(_e=None):
            sel = tree.selection()
            if sel:
                ph = tree.item(sel[0], "values")[0]
                self.clipboard_clear()
                self.clipboard_append(ph)
                hint.configure(text=f"Copied {ph}")
        tree.bind("<Double-1>", copy)
        hint = ttk.Label(frame, text="", style="Status.TLabel")
        hint.pack(anchor="w", pady=(6, 0))

    # ----- output --------------------------------------------------------

    def _choose_outdir(self):
        d = filedialog.askdirectory(title="Choose output folder", initialdir=self.outdir.get() or None)
        if d:
            self.outdir.set(d)

    # ----- run -----------------------------------------------------------

    def _log(self, msg, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _persist(self):
        self.settings.update({
            "template": self.template_path.get() if self.use_custom.get() else "",
            "outdir": self.outdir.get(),
            "recursive": self.recursive.get(),
            "write_json": self.write_json.get(),
            "write_csv": self.write_csv.get(),
            "open_when_done": self.open_when_done.get(),
            "llm_model": self.llm_model.get(),
        })
        save_settings(self.settings)

    def _run(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.pdfs:
            messagebox.showinfo(APP_NAME, "Add at least one SDS PDF first (Step 1).")
            return
        template = self._active_template()
        if not os.path.isfile(template):
            messagebox.showerror(APP_NAME, "The selected template file can't be found. Upload it again in Step 2.")
            return
        outdir = self.outdir.get().strip()
        if not outdir:
            messagebox.showinfo(APP_NAME, "Choose an output folder (Step 3).")
            return
        try:
            os.makedirs(outdir, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Can't create the output folder:\n{e}")
            return

        self._persist()
        self._outputs = {}
        for p in self.pdfs:
            self._set_row(p, "Waiting")
        self.run_btn.configure(state="disabled")
        self.progress.configure(maximum=len(self.pdfs), value=0)
        llm = pipeline.LLMOptions(model=self.llm_model.get().strip()) if self.use_llm.get() else None
        self._log(f"\nProcessing {len(self.pdfs)} file(s) with template: {os.path.basename(template)}")
        self.worker = threading.Thread(
            target=self._work,
            args=(list(self.pdfs), template, outdir, self.write_json.get(), self.write_csv.get(), llm),
            daemon=True)
        self.worker.start()

    def _work(self, pdfs, template, outdir, write_json, write_csv, llm):
        post = self.messages.put
        results = []
        for i, path in enumerate(pdfs):
            post(("row", path, "Reading…", ""))
            post(("progress", i, f"Reading {os.path.basename(path)} ({i + 1} of {len(pdfs)})"))
            res = pipeline.process_pdf(path, outdir, template=template, write_json=write_json, llm=llm,
                                       log=lambda m: post(("log", m, None)), overwrite=False)
            results.append(res)
            if res.error:
                post(("row", path, "Failed", "error"))
                post(("log", f"✗ {os.path.basename(path)}: {res.error}", "err"))
            else:
                missing = sum(1 for v in res.extracted.values() if v == "Not stated in SDS")
                status = "Done" if missing < 10 else f"Done · {missing} gaps"
                post(("row", path, status, "done" if missing < 10 else "warn"))
                post(("output", path, res.summary_path))
                post(("log", f"✓ {os.path.basename(path)} → {os.path.basename(res.summary_path)}", "ok"))
        csv_path = None
        if write_csv:
            try:
                csv_path = pipeline.write_combined_csv(
                    results, pipeline.unique_path(os.path.join(outdir, "SDS_combined.csv")))
            except PermissionError:
                post(("log", "Couldn't write SDS_combined.csv - is it open in Excel? Close it and run again.", "warn"))
        post(("finished", results, outdir, csv_path))

    def _drain_messages(self):
        try:
            while True:
                msg = self.messages.get_nowait()
                kind = msg[0]
                if kind == "row":
                    self._set_row(msg[1], msg[2], msg[3])
                elif kind == "progress":
                    self.progress.configure(value=msg[1])
                    self.progress_label.configure(text=msg[2])
                elif kind == "log":
                    self._log(msg[1], msg[2])
                elif kind == "output":
                    self._outputs[os.path.normcase(os.path.abspath(msg[1]))] = msg[2]
                elif kind == "finished":
                    self._finish(*msg[1:])
        except queue.Empty:
            pass
        self.after(100, self._drain_messages)

    def _finish(self, results, outdir, csv_path):
        ok = sum(1 for r in results if not r.error)
        failed = len(results) - ok
        self.progress.configure(value=len(results))
        summary = f"Finished: {ok} processed" + (f", {failed} failed" if failed else "") + "."
        self.progress_label.configure(text=summary)
        self._log(summary, "ok" if not failed else "warn")
        if csv_path:
            self._log(f"Combined spreadsheet: {csv_path}", "ok")
        self._log("Always check the output against the original SDS before relying on it.")
        self.run_btn.configure(state="normal")
        if self.open_when_done.get() and ok:
            open_path(outdir)

    def _on_close(self):
        self._persist()
        self.destroy()


def selftest(pdf: str, outdir: str) -> int:
    """Headless check used by CI against the frozen exe: process one PDF
    with the bundled template and confirm the summary document exists."""
    res = pipeline.process_pdf(pdf, outdir, write_json=True)
    ok = not res.error and res.summary_path and os.path.isfile(res.summary_path)
    with open(os.path.join(outdir, "selftest.txt"), "w", encoding="utf-8") as f:
        f.write("OK\n" if ok else f"FAIL: {res.error}\n")
    return 0 if ok else 1


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2], sys.argv[3]))
    if sys.platform == "win32":
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    SDSReaderApp().mainloop()


if __name__ == "__main__":
    main()
