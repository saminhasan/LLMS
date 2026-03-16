from threading import Thread
import customtkinter as ctk
from tkinter import filedialog

from usbx import usb, Device
from serial.tools import list_ports, list_ports_common

from numpy import deg2rad, float32, ones, clip

from pathlib import Path

base_dir = Path(__file__).resolve().parent
traj_dir = base_dir / "Trajectories"


WIDTH, HEIGHT = 800, 480

# Teensy 4.1  VID/PID. Add more PIDs if needed.
TEENSY_VID = 0x16C0
TEENSY_PIDS = {0x0483}

def portList() -> list[list_ports_common.ListPortInfo]: #can optional return empty list
    return [p for p in list_ports.comports() if getattr(p, "vid", None) == TEENSY_VID and getattr(p, "pid", None) in TEENSY_PIDS]

def is_teensy_usb(dev: Device) -> bool:
    return dev.vid == TEENSY_VID and dev.pid in TEENSY_PIDS



class App(ctk.CTk):
    def __init__(self, conn=None) -> None:
        super().__init__()
        self.conn = conn
        self.running = True
        self.response = None

        self.create_widgets()

        # usbx hotplug (callbacks run on background thread; bounce into UI thread)
        usb.on_connected(self._usb_connected)
        usb.on_disconnected(self._usb_disconnected)
        usb.start_monitor() # type:ignore  # Start the USB monitoring thread


        self.update_port_list()

        self.font = ctk.CTkFont(family="Consolas", size=16, weight="bold")
        for child in self.winfo_children():
            if isinstance(child, (ctk.CTkLabel, ctk.CTkButton, ctk.CTkEntry, ctk.CTkOptionMenu)):
                try:
                    child.configure(font=self.font)
                except Exception:
                    pass

        self.bind("<<ReceivedResponse>>", self.responseHandler)
        self.resLT: Thread = Thread(
            target=self.responseListener, daemon=True, name="ResponseListenerThread"
        )

    def create_widgets(self) -> None:
        self.title("Hexapod Control")
        self.geometry(f"{WIDTH}x{HEIGHT}")
        self.minsize(WIDTH, HEIGHT)
        self.resizable(False, False)

        for c in range(4):
            self.grid_columnconfigure(c, weight=1, uniform="cols")
        for r, w in enumerate([1, 1, 1, 1, 2, 2, 1, 0, 0]):
            self.grid_rowconfigure(r, weight=w)

        self.port_list = ctk.CTkOptionMenu(
            self,
            values=[p.device for p in portList()] or ["No Devices"],
            command=lambda port: self.button_callback({"PORT": port}, ),
        )
        self.port_list.grid(row=0, column=0, columnspan=2, padx=(20, 10), pady=(20, 10), sticky="ew")

        self.connect_button = ctk.CTkButton(
            self,
            text="CONNECT",
            command=lambda: self.button_callback({"CONNECT": self.port_list.get()}, ),
        )
        self.connect_button.grid(row=0, column=2, padx=(10, 10), pady=(20, 10), sticky="nsew")

        self.disconnect_button = ctk.CTkButton(
            self,
            text="DISCONNECT",
            command=lambda: self.button_callback({"DISCONNECT": self.port_list.get()}, ),
        )
        self.disconnect_button.grid(row=0, column=3, padx=(10, 20), pady=(20, 10), sticky="nsew")

        self.enable_button = ctk.CTkButton(
            self, text="ENABLE", command=lambda: self.button_callback({"ENABLE": None}, )
        )
        self.enable_button.grid(row=1, column=0, columnspan=2, padx=(20, 10), pady=(10, 10), sticky="nsew")

        self.disable_button = ctk.CTkButton(
            self, text="DISABLE", command=lambda: self.button_callback({"DISABLE": None}, )
        )
        self.disable_button.grid(row=1, column=2, columnspan=2, padx=(10, 20), pady=(10, 10), sticky="nsew")

        self.upload_button = ctk.CTkButton(
            self, text="UPLOAD", command=lambda: self.button_callback({"UPLOAD": None}, )
        )
        self.upload_button.grid(row=2, column=0, columnspan=2, padx=(20, 10), pady=(10, 10), sticky="nsew")

        self.calibrate_button = ctk.CTkButton(self, text="CALIBRATE", command=lambda: self.button_callback({"CALIBRATE": None}, )
        )
        self.calibrate_button.grid(row=2, column=2, columnspan=2, padx=(10, 20), pady=(10, 10), sticky="nsew")

        self.stage_button = ctk.CTkButton(
            self, text="STAGE", command=lambda: self.button_callback({"STAGE": None}, )
        )
        self.stage_button.grid(row=3, column=0, columnspan=2, padx=(20, 10), pady=(10, 10), sticky="nsew")

        self.park_button = ctk.CTkButton(
            self, text="PARK", command=lambda: self.button_callback({"PARK": None}, )
        )
        self.park_button.grid(row=3, column=2, columnspan=2, padx=(10, 20), pady=(10, 10), sticky="nsew")

        self.player = ctk.CTkSegmentedButton(
            self,
            values=["PLAY", "PAUSE", "STOP"],
            command=lambda choice: self.button_callback({choice: None}, ),
        )
        self.player.grid(row=4, column=0, columnspan=4, padx=(20, 20), pady=(10, 10), sticky="nsew")

        self.estop_button = ctk.CTkButton(self, text="ESTOP", command=lambda: self.button_callback({"ESTOP": None}, ))
        self.estop_button.grid(row=5, column=0, columnspan=3, rowspan=2, padx=(20, 10), pady=(10, 10), sticky="nsew")

        self.reset_button = ctk.CTkButton(
            self, text="RESET", command=lambda: self.button_callback({"RESET": None}, )
        )
        self.reset_button.grid(row=5, column=3, rowspan=2, padx=(10, 20), pady=(10, 10), sticky="nsew")

        self.input_field = ctk.CTkEntry(self, placeholder_text="")
        self.input_field.grid(row=7, column=0, columnspan=3, padx=(20, 10), pady=(10, 20), sticky="nsew")
        self.input_field.bind("<Return>", lambda _e: self.button_callback({"SEND": self.input_field.get()}, ))

        self.send_button = ctk.CTkButton(
            self, text="SEND", command=lambda: self.button_callback({"SEND": self.input_field.get()}, )
        )
        self.send_button.grid(row=7, column=3, padx=(10, 20), pady=(10, 20), sticky="nsew")

        self.protocol("WM_DELETE_WINDOW", lambda: self.button_callback({"QUIT": None}))

    def _usb_connected(self, dev: Device) -> None:
        if is_teensy_usb(dev):
            self.after(0, self.update_port_list)

    def _usb_disconnected(self, dev: Device) -> None:
        if is_teensy_usb(dev):
            self.after(0, self.update_port_list)

    def update_port_list(self) -> None:
        ports = [p.device for p in portList()] or ["No Devices"]
        print(f"[App.update_port_list] -> {ports}")
        self.port_list.configure(values=ports)
        self.port_list.set(ports[0])
        if ports[0] != "No Devices":
            self.button_callback({"PORT": ports[0]})

    def responseListener(self) -> None:
        if not self.conn:
            return
        while self.running:
            try:
                self.response = self.conn.recv()
                # add response processing here (e.g. parse state, updates, etc.)
            except Exception as e:
                print(f"[App.responseListener] -> {e}")
                continue

            if self.response is None:
                self.running = False
                break

            self.after(0, lambda: self.event_generate("<<ReceivedResponse>>", when="tail"))

    def responseHandler(self, _event) -> None:
        print(f"[App.responseHandler] -> response : {self.response} | event: {_event}")

    def run(self) -> None:
        if self.conn:
            self.resLT.start()
        self.mainloop()
    
    def button_callback(self, event) -> None:
        key, value = next(iter(event.items()))


        match key:
            case "UPLOAD":
                file_path: str = filedialog.askopenfilename(
                    initialdir=str(traj_dir),
                    title="Select File",
                    filetypes=[
                        ("CSV Files", "*.csv")
                    ],
                )
                if file_path:
                    event[key] = file_path
                else:
                    return

            case "SEND":
                try:
                    event[key] = ones(6, dtype=float32) * deg2rad(
                        clip(float(self.input_field.get()), -60, 60)
                    )
                except (ValueError, TypeError):
                    return
                finally:
                    self.input_field.delete(0, "end")

            case (
                "PORT" | "CONNECT" | "DISCONNECT" | "ENABLE" | "DISABLE"
                | "PLAY" | "PAUSE" | "STOP" | "ESTOP" | "RESET" | "CALIBRATE"
                | "STAGE" | "PARK"
            ):
                pass

            case "QUIT":
                self.on_closing()
                return

            case _:
                print(f"[App.button_callback] -> Unknown action: {key} with value: {value}")

        if self.conn:
            self.conn.send(event)

    def on_closing(self) -> None:

        if self.conn:
            self.conn.send(None)
        self.running = False
        if getattr(self, "resLT", None) and self.resLT.is_alive():
            self.resLT.join(timeout=1.0)

        # Unregister usbx callbacks (stop UI updates)
        usb.on_connected(None)
        usb.on_disconnected(None)
        self.destroy()

if __name__ == "__main__":
    app = App(conn=None)
    app.run()
