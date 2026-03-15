from __future__ import annotations

import os
import sys
import struct
import threading
import pandas as pd
import tkinter as tk
from tqdm import tqdm
import customtkinter as ctk
from dataclasses import dataclass
import matplotlib.pyplot as plt
from typing import Dict, List, Optional, Tuple
from tkinter import filedialog, messagebox
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.backends._backend_tk import NavigationToolbar2Tk



START_BYTE = 0xFE
END_BYTE = 0xFF
PKT_LEN = 64
MSGID_JOG = 0x0B
MSGID_STATUS = 0xFF


@dataclass(frozen=True)
class PacketLayout:
    axis1_offset: int = 10
    axis2_offset: int = 33


def crc16_xmodem(data: bytes, init: int = 0x0000) -> int:
    crc = init
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def parse_axis(buf: bytes, offset: int) -> Dict[str, float | int]:
    mode = buf[offset + 0]
    flags = buf[offset + 1]
    setpoint, theta, omega, tau = struct.unpack_from("<ffff", buf, offset + 2)
    temp = struct.unpack_from("<b", buf, offset + 18)[0]
    rtt = struct.unpack_from("<H", buf, offset + 19)[0]
    tx_err = buf[offset + 21]
    timeouts = buf[offset + 22]

    return {
        "mode": mode,
        "flags": flags,
        "setpoint": setpoint,
        "theta": theta,
        "omega": omega,
        "tau": tau,
        "temp": temp,
        "rtt": rtt,
        "txErr": tx_err,
        "timeouts": timeouts,
    }


def extract_axis_dfs(bin_path: str, verify_crc: bool = True) -> Tuple[Dict[int, pd.DataFrame], pd.DataFrame]:
    with open(bin_path, "rb") as file:
        data = file.read()

    rows_by_axis: Dict[int, List[dict]] = {}
    jog_rows: List[dict] = []
    layout = PacketLayout()

    total_bytes = len(data)
    scan_limit = total_bytes - PKT_LEN + 1
    skip_until = 0

    for index in tqdm(range(max(scan_limit, 0)), desc="Parsing packets", unit="byte"):
        if index < skip_until:
            continue
        if data[index] != START_BYTE:
            continue
        if data[index + 63] != END_BYTE:
            continue

        packet = data[index:index + PKT_LEN]

        if verify_crc:
            expected_crc = struct.unpack_from("<H", packet, 61)[0]
            actual_crc = crc16_xmodem(packet[0:61])
            if actual_crc != expected_crc:
                continue

        from_id = packet[1]
        to_id = packet[2]
        seq = packet[3]
        msgid = packet[4]
        if msgid == MSGID_STATUS:
            timestamp_us = struct.unpack_from("<I", packet, 5)[0]
            resflag = packet[9]

            axis1_no = 2 * from_id - 1
            axis2_no = 2 * from_id

            common = {
                "from_id": from_id,
                "to_id": to_id,
                "seq": seq,
                "msgid": msgid,
                "timestamp_us": timestamp_us,
                "resflag": resflag,
            }

            axis1 = parse_axis(packet, layout.axis1_offset)
            axis2 = parse_axis(packet, layout.axis2_offset)

            axis1_row = common.copy()
            axis1_row["axis_no"] = axis1_no
            axis1_row.update(axis1)
            rows_by_axis.setdefault(axis1_no, []).append(axis1_row)

            axis2_row = common.copy()
            axis2_row["axis_no"] = axis2_no
            axis2_row.update(axis2)
            rows_by_axis.setdefault(axis2_no, []).append(axis2_row)
        elif msgid == MSGID_JOG:
            j1, j2, j3, j4, j5, j6 = struct.unpack_from("<6f", packet, 5)
            jog_rows.append(
                {
                    "packet_index": index,
                    "from_id": from_id,
                    "to_id": to_id,
                    "seq": seq,
                    "msgid": msgid,
                    "j1": j1,
                    "j2": j2,
                    "j3": j3,
                    "j4": j4,
                    "j5": j5,
                    "j6": j6,
                }
            )

        skip_until = index + PKT_LEN

    dfs = {axis: pd.DataFrame(rows) for axis, rows in rows_by_axis.items()}

    for axis, df in dfs.items():
        if not df.empty:
            dfs[axis] = df.sort_values(["timestamp_us", "seq"], kind="stable").reset_index(drop=True)

    jog_df = pd.DataFrame(jog_rows)
    if not jog_df.empty:
        jog_df = jog_df.sort_values(["packet_index", "seq"], kind="stable").reset_index(drop=True)

    return dfs, jog_df


def prompt_for_bin_file() -> Optional[str]:
    dialog_root = tk.Tk()
    dialog_root.withdraw()

    file_path = filedialog.askopenfilename(
        title="Select log file",
        filetypes=[("Binary files", "*.bin"), ("All files", "*.*")],
        parent=dialog_root,
    )

    dialog_root.destroy()
    return file_path or None


class LogDecoderApp:
    def __init__(self, bin_path: str, dfs: Dict[int, pd.DataFrame], jog_df: pd.DataFrame) -> None:
        self.bin_path = bin_path
        self.dfs = dfs
        self.jog_df = jog_df
        self.app_closing = False

        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk()
        self.root.title(f"Axis Plots - {os.path.basename(self.bin_path)}")
        self.root.geometry("1280x720")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.tabview = ctk.CTkTabview(self.root)
        self.tabview.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        controls = ctk.CTkFrame(self.root)
        controls.pack(side="bottom", fill="x", padx=8, pady=(4, 8))

        self.save_btn = ctk.CTkButton(controls, text="Save as Excel", command=self.save_dfs_to_excel)
        self.save_btn.pack(side="left", padx=8, pady=8)

        self.status_label = ctk.CTkLabel(controls, text="")
        self.status_label.pack(side="left", padx=8, pady=8)

        self.build_axis_tabs()
        self.build_jog_tab()

    def on_close(self) -> None:
        self.app_closing = True
        plt.close("all")
        self.root.quit()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()

    def build_axis_tabs(self) -> None:
        for axis in range(1, 7):
            df = self.dfs.get(axis)
            tab_name = f"Axis {axis}"
            self.tabview.add(tab_name)
            tab = self.tabview.tab(tab_name)

            if df is None or df.empty:
                msg = ctk.CTkLabel(tab, text=f"No data for axis {axis}")
                msg.pack(padx=12, pady=12)
                continue

            time_s = df["timestamp_us"].to_numpy(dtype="float64") * 1e-6

            fig, axis_plot = plt.subplots()
            axis_plot.plot(time_s, df["setpoint"].to_numpy(), label="setpoint")
            axis_plot.plot(time_s, df["theta"].to_numpy(), label="theta")
            axis_plot.set_title(f"Axis {axis}: setpoint & theta vs time")
            axis_plot.set_xlabel("time (s)")
            axis_plot.set_ylabel("rad")
            axis_plot.grid(True)
            axis_plot.legend(loc="upper right")

            plot_host = tk.Frame(tab)
            plot_host.pack(side="top", fill="both", expand=True)

            toolbar_frame = tk.Frame(plot_host)
            toolbar_frame.pack(side="top", fill="x")

            canvas = FigureCanvasTkAgg(fig, master=plot_host)
            toolbar = NavigationToolbar2Tk(canvas, toolbar_frame)
            toolbar.update()
            canvas.draw()
            canvas.get_tk_widget().pack(side="top", fill="both", expand=True)

    def build_jog_tab(self) -> None:
        tab_name = "JOG"
        self.tabview.add(tab_name)
        tab = self.tabview.tab(tab_name)

        if self.jog_df.empty:
            msg = ctk.CTkLabel(tab, text="No JOG packets found")
            msg.pack(padx=12, pady=12)
            return

        x = self.jog_df["packet_index"].to_numpy(dtype="float64")

        fig, jog_plot = plt.subplots()
        for i in range(1, 7):
            jog_plot.plot(x, self.jog_df[f"j{i}"].to_numpy(dtype="float64"), label=f"j{i}")
        jog_plot.set_title("JOG payload (6 floats) vs packet index")
        jog_plot.set_xlabel("packet index")
        jog_plot.set_ylabel("command")
        jog_plot.grid(True)
        jog_plot.legend(loc="upper right")

        plot_host = tk.Frame(tab)
        plot_host.pack(side="top", fill="both", expand=True)

        toolbar_frame = tk.Frame(plot_host)
        toolbar_frame.pack(side="top", fill="x")

        canvas = FigureCanvasTkAgg(fig, master=plot_host)
        toolbar = NavigationToolbar2Tk(canvas, toolbar_frame)
        toolbar.update()
        canvas.draw()
        canvas.get_tk_widget().pack(side="top", fill="both", expand=True)

    def _on_export_done(self, out_path: Optional[str] = None, error: Optional[str] = None) -> None:
        if self.app_closing or not self.root.winfo_exists():
            return

        self.save_btn.configure(state="normal", text="Save as Excel")
        self.status_label.configure(text="")

        if error is None:
            messagebox.showinfo("Export complete", f"Saved Excel file:\n{out_path}", parent=self.root)
        else:
            messagebox.showerror("Export failed", f"Could not save Excel file:\n{error}", parent=self.root)

    def _export_worker(self, out_path: str) -> None:
        try:
            with pd.ExcelWriter(out_path) as writer:
                for axis in range(1, 7):
                    df = self.dfs.get(axis)
                    if df is None:
                        pd.DataFrame().to_excel(writer, sheet_name=f"Axis_{axis}", index=False)
                    else:
                        df.to_excel(writer, sheet_name=f"Axis_{axis}", index=False)
                self.jog_df.to_excel(writer, sheet_name="JOG", index=False)
            self.root.after(0, lambda: self._on_export_done(out_path=out_path))
        except Exception as exc:
            self.root.after(0, lambda: self._on_export_done(error=str(exc)))

    def save_dfs_to_excel(self) -> None:
        default_name = f"{os.path.splitext(os.path.basename(self.bin_path))[0]}_axes.xlsx"
        out_path = filedialog.asksaveasfilename(
            title="Save Excel file",
            defaultextension=".xlsx",
            initialfile=default_name,
            filetypes=[("Excel Workbook", "*.xlsx")],
        )

        if not out_path:
            return

        self.save_btn.configure(state="disabled", text="Exporting...")
        self.status_label.configure(text="Exporting Excel in background...")

        threading.Thread(target=self._export_worker, args=(out_path,), daemon=True).start()


def main() -> int:
    bin_path = prompt_for_bin_file()
    if not bin_path:
        print("No file selected. Exiting.")
        return 0

    dfs, jog_df = extract_axis_dfs(bin_path, verify_crc=False)
    app = LogDecoderApp(bin_path=bin_path, dfs=dfs, jog_df=jog_df)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
