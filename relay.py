import os
import sys
import json
import tempfile
import subprocess
import random
import psutil
import hashlib
import socket
import select
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

# ===============================
# CONFIG & SETTINGS MANAGEMENT
# ===============================
def get_config_path():
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "relay_config.json")

DEFAULT_CONFIG = {
    "srt_server": "2.28.77.90:12345",
    "pkt_size": "1316",
    "latency": "500",        # in ms
    "sndbuf_mb": "16",       # in MB
    "telegram_url": "http://vn3.link123.fun:5000/send",
    "chat_id": "-1003971166748",
    "secret": "3571218",
    "use_tunnel": True,
    "tunnel_remote_ip": "2.28.77.90",
    "tunnel_remote_port": "29900",
    "tunnel_flows": "4",
    "tunnel_local_port": "12345"
}

def load_config():
    cfg_path = get_config_path()
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {**DEFAULT_CONFIG, **data}
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    cfg_path = get_config_path()
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        return True
    except Exception as e:
        messagebox.showerror("Lỗi", f"Không thể lưu cài đặt: {e}")
        return False

current_config = load_config()

# ===============================
# MULTI-FLOW SRT TUNNEL ENGINE
# ===============================
tunnel_thread = None
tunnel_running = False
tunnel_lock = threading.Lock()
tunnel_stats = {"packets": 0, "speed_mbps": 0.0, "active": False, "flows": 16, "status_text": "Chưa chạy"}

def run_tunnel_worker(remote_ip, remote_start_port, num_flows, local_port):
    global tunnel_running, tunnel_stats
    local_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    local_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    local_sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    try:
        local_sock.bind(("127.0.0.1", local_port))
    except Exception as e:
        tunnel_stats["status_text"] = f"Lỗi bind cổng {local_port}: {e}"
        tunnel_running = False
        return

    out_socks = []
    remote_endpoints = []
    for i in range(num_flows):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        out_socks.append(s)
        remote_endpoints.append((remote_ip, remote_start_port + i))

    all_socks = [local_sock] + out_socks
    flow_idx = 0
    batch_counter = 0
    BATCH_SIZE = 8
    last_ffmpeg_addr = None
    
    last_stat_time = time.time()
    bytes_count = 0
    pkt_count = 0

    tunnel_stats["active"] = True
    tunnel_stats["status_text"] = f"Đang chạy ({num_flows} luồng -> {remote_ip})"

    while tunnel_running:
        try:
            r, _, _ = select.select(all_socks, [], [], 0.5)
            now = time.time()
            if now - last_stat_time >= 2.0:
                dt = now - last_stat_time
                mbps = (bytes_count * 8) / (dt * 1000 * 1000)
                tunnel_stats["speed_mbps"] = mbps
                tunnel_stats["packets"] = pkt_count
                bytes_count = 0
                last_stat_time = now

            for s in r:
                if s is local_sock:
                    data, addr = local_sock.recvfrom(65535)
                    last_ffmpeg_addr = addr
                    
                    # SRT Protocol: Bit 0 is 1 for Control Packet (Handshake, Keepalive, ACK, NAK)
                    # Control packets MUST always stay on flow 0 for reliable bidirectional exchange!
                    if len(data) >= 1 and (data[0] & 0x80) != 0:
                        out_socks[0].sendto(data, remote_endpoints[0])
                    else:
                        out_socks[flow_idx].sendto(data, remote_endpoints[flow_idx])
                        batch_counter += 1
                        if batch_counter >= BATCH_SIZE:
                            batch_counter = 0
                            flow_idx = (flow_idx + 1) % num_flows
                    
                    bytes_count += len(data)
                    pkt_count += 1
                else:
                    data, _ = s.recvfrom(65535)
                    if last_ffmpeg_addr:
                        try:
                            local_sock.sendto(data, last_ffmpeg_addr)
                        except Exception:
                            pass
        except Exception:
            time.sleep(0.01)

    try:
        local_sock.close()
        for s in out_socks:
            s.close()
    except Exception:
        pass
    tunnel_stats["active"] = False
    tunnel_stats["status_text"] = "Đã dừng"

def start_tunnel_service():
    global tunnel_thread, tunnel_running
    with tunnel_lock:
        if tunnel_running and tunnel_thread and tunnel_thread.is_alive():
            return
        tunnel_running = True
        rip = current_config.get("tunnel_remote_ip", "2.28.77.90").strip()
        rport = int(current_config.get("tunnel_remote_port", 29900))
        flows = int(current_config.get("tunnel_flows", 16))
        lport = int(current_config.get("tunnel_local_port", 12345))
        tunnel_stats["flows"] = flows
        tunnel_thread = threading.Thread(
            target=run_tunnel_worker,
            args=(rip, rport, flows, lport),
            daemon=True
        )
        tunnel_thread.start()

def stop_tunnel_service():
    global tunnel_running
    with tunnel_lock:
        tunnel_running = False

# ===============================
# GLOBAL STATE
# ===============================
running_relays = {}  # srt_url -> pid

# ===============================
# CHECK SRT RUNNING
# ===============================
def is_srt_running(srt_url):
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['name'] and "ffmpeg" in proc.info['name'].lower():
                cmdline = " ".join(proc.info['cmdline'])
                check_str = srt_url.replace("^&", "&")
                if srt_url in cmdline or check_str in cmdline:
                    return proc.info['pid']
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None

# ===============================
# REFRESH RELAY LIST
# ===============================
def sort_treeview(col, reverse):
    data = [(relay_list.set(k, col), k) for k in relay_list.get_children('')]
    data.sort(reverse=reverse)

    for index, (val, k) in enumerate(data):
        relay_list.move(k, '', index)

    relay_list.heading(col, command=lambda: sort_treeview(col, not reverse))

def refresh_list():
    relay_list.delete(*relay_list.get_children())
    running_relays.clear()

    temp_list = []

    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.info['name'] and "ffmpeg" in proc.info['name'].lower():
                cmdline = " ".join(proc.info['cmdline'])

                if "srt://" in cmdline:
                    for part in proc.info['cmdline']:
                        if "srt://" in part:
                            clean_part = part.strip('"')
                            temp_list.append((clean_part, proc.info['pid']))
        except Exception:
            continue

    temp_list.sort(key=lambda x: x[0].lower())

    for srt, pid in temp_list:
        running_relays[srt] = pid
        relay_list.insert("", "end", values=(srt, pid))

# ===============================
# AUTO REFRESH (3 seconds)
# ===============================
def auto_refresh():
    refresh_list()
    # Cập nhật thông số Tunnel
    if tunnel_stats["active"]:
        speed = tunnel_stats["speed_mbps"]
        flows = tunnel_stats["flows"]
        tunnel_status_lbl.config(
            text=f"⚡ Tunnel: Đang chạy ({flows} luồng -> {current_config.get('tunnel_remote_ip','2.28.77.90')}) | ~{speed:.2f} Mbps",
            fg="#28a745"
        )
    else:
        tunnel_status_lbl.config(text="⚪ Tunnel: Sẵn sàng (chưa có luồng)", fg="#6c757d")
    root.after(3000, auto_refresh)

# ===============================
# START RELAY
# ===============================
def start_relay():
    input_url = input_entry.get().strip()
    stream_id = stream_id_cb.get().strip()

    if not input_url:
        messagebox.showerror("Lỗi", "Input không được để trống")
        return

    if not stream_id:
        messagebox.showerror("Lỗi", "Vui lòng chọn Stream ID")
        return

    pkt_size = current_config.get("pkt_size", "1316").strip() or "1316"
    
    # Latency: convert ms to microseconds for ffmpeg libsrt
    raw_latency = current_config.get("latency", "500").strip() or "500"
    try:
        lat_val = int(raw_latency)
        latency_us = lat_val * 1000 if lat_val < 50000 else lat_val
    except Exception:
        latency_us = 500000

    raw_sndbuf = current_config.get("sndbuf_mb", "16").strip() or "16"
    try:
        buf_val = int(raw_sndbuf)
        sndbuf_bytes = buf_val * 1024 * 1024 if buf_val < 10000 else buf_val
    except Exception:
        sndbuf_bytes = 16777216

    tele_url = current_config.get("telegram_url", "").strip()
    chat_id = current_config.get("chat_id", "").strip()
    secret = current_config.get("secret", "").strip()

    use_tunnel = tunnel_var.get()
    if use_tunnel:
        start_tunnel_service()
        lport = current_config.get("tunnel_local_port", "12345")
        target_server = f"127.0.0.1:{lport}"
        tunnel_tag = f" [Tunnel {current_config.get('tunnel_flows', '16')}F]"
    else:
        srt_server = current_config.get("srt_server", "2.28.77.90:12345").strip()
        if srt_server.startswith("srt://"):
            srt_server = srt_server[6:]
        target_server = srt_server.split("?")[0].strip()
        tunnel_tag = ""

    output_srt = f"srt://{target_server}?streamid=#!::r={stream_id},m=publish&pkt_size={pkt_size}&latency={latency_us}&sndbuf={sndbuf_bytes}&rcvbuf={sndbuf_bytes}"
    safe_title_srt = output_srt.replace("&", "^&")

    if is_srt_running(output_srt):
        messagebox.showwarning("Trùng Stream", "Stream này đang chạy rồi")
        return

    use_re = re_var.get()
    re_flag_cmd = "-re ^\n " if use_re else ""
    title_re_tag = " [4K -re]" if use_re else ""
    mode_text = f"4K (-re enabled){tunnel_tag}" if use_re else f"Standard{tunnel_tag}"

    temp_dir = tempfile.gettempdir()
    bat_name = f"relay_{random.randint(10000,99999)}.bat"
    bat_path = os.path.join(temp_dir, bat_name)

    stop_hash = hashlib.md5(output_srt.encode('utf-8')).hexdigest()
    stop_file = os.path.join(temp_dir, f"stop_{stop_hash}.txt")
    if os.path.exists(stop_file):
        try:
            os.remove(stop_file)
        except Exception:
            pass

    start_tele_cmd = ""
    stop_tele_cmd = ""
    if tele_url:
        tele_msg_start = f"RELAY STARTED: {stream_id}{title_re_tag}{tunnel_tag} %time%"
        tele_msg_stop = f"RELAY STOPPED: {stream_id}{title_re_tag}{tunnel_tag} %time%"
        start_tele_cmd = f"""curl -s -X POST "{tele_url}" ^
 -H "Content-Type: application/json" ^
 -d "{{\\"chat_id\\":\\"{chat_id}\\",\\"text\\":\\"{tele_msg_start}\\",\\"token\\":\\"{secret}\\"}}" >nul 2>&1"""

        stop_tele_cmd = f"""curl -s -X POST "{tele_url}" ^
 -H "Content-Type: application/json" ^
 -d "{{\\"chat_id\\":\\"{chat_id}\\",\\"text\\":\\"{tele_msg_stop}\\",\\"token\\":\\"{secret}\\"}}" >nul 2>&1"""

    bat_content = f"""@echo off
chcp 65001 >nul
title RELAY: {safe_title_srt}{title_re_tag}

echo Dang relay...
echo SRT: {safe_title_srt}
echo Che do: {mode_text}
echo Latency: {latency_us // 1000}ms, Buffer: {sndbuf_bytes // (1024*1024)}MB, pkt_size: {pkt_size}
echo.

REM ===== SEND START MESSAGE =====
{start_tele_cmd}

:loop_command
if exist "{stop_file}" goto end_loop
ffmpeg ^
 -user_agent "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36" ^
 -rw_timeout 30000000 ^
 -reconnect 1 ^
 -reconnect_streamed 1 ^
 -reconnect_at_eof 1 ^
 -reconnect_on_network_error 1 ^
 -reconnect_delay_max 2 ^
 {re_flag_cmd}-i "{input_url}" ^
 -c copy ^
 -flush_packets 1 ^
 -f mpegts ^
 "{output_srt}"

if exist "{stop_file}" goto end_loop
timeout /t 1 >nul
goto loop_command

:end_loop
if exist "{stop_file}" del "{stop_file}"
echo.
echo ===== STOPPED =====

REM ===== SEND STOP MESSAGE =====
{stop_tele_cmd}

timeout /t 2 >nul
start "" cmd /c del "%~dpnx0"
exit
"""

    with open(bat_path, "w", encoding="utf-8") as f:
        f.write(bat_content)

    subprocess.Popen(
        ["cmd.exe", "/c", "start", "", bat_path],
        shell=True
    )

# ===============================
# STOP SELECTED RELAY
# ===============================
def stop_selected():
    selected = relay_list.selection()
    if not selected:
        return

    item = relay_list.item(selected[0])
    srt, pid = item["values"]

    try:
        temp_dir = tempfile.gettempdir()
        stop_hash = hashlib.md5(srt.encode('utf-8')).hexdigest()
        stop_file = os.path.join(temp_dir, f"stop_{stop_hash}.txt")
        with open(stop_file, "w") as f:
            f.write("stop")
    except Exception:
        pass

    try:
        psutil.Process(pid).kill()
    except Exception:
        pass

# ===============================
# SETTINGS DIALOG
# ===============================
def open_settings():
    settings_win = tk.Toplevel(root)
    settings_win.title("Cài Đặt Hệ Thống (SRT, Tunnel & Telegram)")
    settings_win.geometry("540x550")
    settings_win.resizable(False, False)
    settings_win.transient(root)
    settings_win.grab_set()

    root.update_idletasks()
    x = root.winfo_x() + (root.winfo_width() // 2) - 270
    y = root.winfo_y() + (root.winfo_height() // 2) - 275
    settings_win.geometry(f"+{x}+{y}")

    # Khung Multi-Flow Tunnel
    tunnel_group = tk.LabelFrame(settings_win, text=" 🚀 Cài Đặt Multi-Flow Tunnel (Vượt QoS) ", font=("Segoe UI", 9, "bold"), padx=12, pady=6, fg="#1a73e8")
    tunnel_group.pack(fill="x", padx=15, pady=(8, 4))

    row_t1 = tk.Frame(tunnel_group)
    row_t1.pack(fill="x", pady=2)
    
    f_tip = tk.Frame(row_t1)
    f_tip.pack(side="left", fill="x", expand=True, padx=(0, 6))
    tk.Label(f_tip, text="Remote VPS IP (Đức):", anchor="w").pack(fill="x")
    tip_entry = tk.Entry(f_tip, font=("Segoe UI", 9))
    tip_entry.insert(0, current_config.get("tunnel_remote_ip", "2.28.77.90"))
    tip_entry.pack(fill="x")

    f_tport = tk.Frame(row_t1)
    f_tport.pack(side="right", fill="x", expand=True, padx=(6, 0))
    tk.Label(f_tport, text="Remote Port bắt đầu (UDP):", anchor="w").pack(fill="x")
    tport_entry = tk.Entry(f_tport, font=("Segoe UI", 9))
    tport_entry.insert(0, current_config.get("tunnel_remote_port", "29900"))
    tport_entry.pack(fill="x")

    row_t2 = tk.Frame(tunnel_group)
    row_t2.pack(fill="x", pady=4)

    f_tflow = tk.Frame(row_t2)
    f_tflow.pack(side="left", fill="x", expand=True, padx=(0, 6))
    tk.Label(f_tflow, text="Số luồng song song (chuẩn 16):", anchor="w").pack(fill="x")
    tflow_entry = tk.Entry(f_tflow, font=("Segoe UI", 9))
    tflow_entry.insert(0, current_config.get("tunnel_flows", "16"))
    tflow_entry.pack(fill="x")

    f_tlport = tk.Frame(row_t2)
    f_tlport.pack(side="right", fill="x", expand=True, padx=(6, 0))
    tk.Label(f_tlport, text="Cổng nội bộ (Local Port):", anchor="w").pack(fill="x")
    tlport_entry = tk.Entry(f_tlport, font=("Segoe UI", 9))
    tlport_entry.insert(0, current_config.get("tunnel_local_port", "12345"))
    tlport_entry.pack(fill="x")

    # Khung SRT Direct Server
    srt_group = tk.LabelFrame(settings_win, text=" Cài Đặt SRT Trực Tiếp (Khi tắt Tunnel) ", font=("Segoe UI", 9, "bold"), padx=12, pady=6)
    srt_group.pack(fill="x", padx=15, pady=4)

    tk.Label(srt_group, text="SRT Direct Server (IP:Port):", anchor="w").pack(fill="x")
    server_entry = tk.Entry(srt_group, font=("Segoe UI", 9))
    server_entry.insert(0, current_config.get("srt_server", "2.28.77.90:12345"))
    server_entry.pack(fill="x", pady=(2, 4))

    row_srt = tk.Frame(srt_group)
    row_srt.pack(fill="x", pady=2)

    f_lat = tk.Frame(row_srt)
    f_lat.pack(side="left", fill="x", expand=True, padx=(0, 6))
    tk.Label(f_lat, text="Latency (ms):", anchor="w").pack(fill="x")
    lat_entry = tk.Entry(f_lat, font=("Segoe UI", 9))
    lat_entry.insert(0, current_config.get("latency", "500"))
    lat_entry.pack(fill="x")

    f_buf = tk.Frame(row_srt)
    f_buf.pack(side="left", fill="x", expand=True, padx=(6, 6))
    tk.Label(f_buf, text="Buffer (MB):", anchor="w").pack(fill="x")
    buf_entry = tk.Entry(f_buf, font=("Segoe UI", 9))
    buf_entry.insert(0, current_config.get("sndbuf_mb", "16"))
    buf_entry.pack(fill="x")

    f_pkt = tk.Frame(row_srt)
    f_pkt.pack(side="right", fill="x", expand=True, padx=(6, 0))
    tk.Label(f_pkt, text="pkt_size:", anchor="w").pack(fill="x")
    pkt_entry = tk.Entry(f_pkt, font=("Segoe UI", 9))
    pkt_entry.insert(0, current_config.get("pkt_size", "1316"))
    pkt_entry.pack(fill="x")

    # Khung Telegram
    tele_group = tk.LabelFrame(settings_win, text=" Cài Đặt Thông Báo Telegram ", font=("Segoe UI", 9, "bold"), padx=12, pady=6)
    tele_group.pack(fill="x", padx=15, pady=4)

    tk.Label(tele_group, text="Link Telegram Endpoint:", anchor="w").pack(fill="x")
    tele_url_entry = tk.Entry(tele_group, font=("Segoe UI", 9))
    tele_url_entry.insert(0, current_config.get("telegram_url", "http://vn3.link123.fun:5000/send"))
    tele_url_entry.pack(fill="x", pady=(2, 4))

    row_tele = tk.Frame(tele_group)
    row_tele.pack(fill="x", pady=2)

    f_chat = tk.Frame(row_tele)
    f_chat.pack(side="left", fill="x", expand=True, padx=(0, 6))
    tk.Label(f_chat, text="Chat ID:", anchor="w").pack(fill="x")
    chat_id_entry = tk.Entry(f_chat, font=("Segoe UI", 9))
    chat_id_entry.insert(0, current_config.get("chat_id", "-1003971166748"))
    chat_id_entry.pack(fill="x")

    f_sec = tk.Frame(row_tele)
    f_sec.pack(side="right", fill="x", expand=True, padx=(6, 0))
    tk.Label(f_sec, text="Token / Secret:", anchor="w").pack(fill="x")
    secret_entry = tk.Entry(f_sec, font=("Segoe UI", 9))
    secret_entry.insert(0, current_config.get("secret", "3571218"))
    secret_entry.pack(fill="x")

    def save_action():
        new_server = server_entry.get().strip()
        new_lat = lat_entry.get().strip() or "500"
        new_buf = buf_entry.get().strip() or "16"
        new_pkt = pkt_entry.get().strip() or "1316"
        new_tele_url = tele_url_entry.get().strip()
        new_chat_id = chat_id_entry.get().strip()
        new_secret = secret_entry.get().strip()

        new_tip = tip_entry.get().strip() or "2.28.77.90"
        new_tport = tport_entry.get().strip() or "29900"
        new_tflow = tflow_entry.get().strip() or "16"
        new_tlport = tlport_entry.get().strip() or "12345"

        if new_server.startswith("srt://"):
            new_server = new_server[6:]
        new_server = new_server.split("?")[0].strip()

        current_config["srt_server"] = new_server
        current_config["latency"] = new_lat
        current_config["sndbuf_mb"] = new_buf
        current_config["pkt_size"] = new_pkt
        current_config["telegram_url"] = new_tele_url
        current_config["chat_id"] = new_chat_id
        current_config["secret"] = new_secret
        current_config["tunnel_remote_ip"] = new_tip
        current_config["tunnel_remote_port"] = new_tport
        current_config["tunnel_flows"] = new_tflow
        current_config["tunnel_local_port"] = new_tlport

        if save_config(current_config):
            server_display_lbl.config(text=f"Flussonic: {current_config.get('tunnel_remote_ip', '2.28.77.90')}")
            # Khởi động lại tunnel với config mới nếu đang bật
            if tunnel_var.get():
                stop_tunnel_service()
                root.after(500, start_tunnel_service)
            messagebox.showinfo("Thành công", "Đã lưu cài đặt SRT & Tunnel thành công!", parent=settings_win)
            settings_win.destroy()

    btn_box = tk.Frame(settings_win)
    btn_box.pack(pady=(8, 8))
    tk.Button(btn_box, text="💾 Lưu Cài Đặt", width=14, bg="#28a745", fg="white", font=("Segoe UI", 9, "bold"), command=save_action).pack(side="left", padx=5)
    tk.Button(btn_box, text="Đóng", width=10, command=settings_win.destroy).pack(side="left", padx=5)

# ===============================
# GUI
# ===============================
root = tk.Tk()
root.title("MIGU Relay Tool PRO + Multi-Flow Tunnel")
root.geometry("820x470")
root.resizable(False, False)

# Top Bar: Input URL
tk.Label(root, text="Full Input URL:", anchor="w").pack(fill="x", padx=10, pady=(8, 0))
input_entry = tk.Entry(root, font=("Segoe UI", 9))
input_entry.pack(fill="x", padx=10)

# Stream ID Combobox & Server Info frame
mid_frame = tk.Frame(root)
mid_frame.pack(fill="x", padx=10, pady=(8, 0))

left_mid = tk.Frame(mid_frame)
left_mid.pack(side="left", fill="x", expand=True)

tk.Label(left_mid, text="Select Stream ID (MIGU1 - MIGU9):", anchor="w").pack(fill="x")

cb_row = tk.Frame(left_mid)
cb_row.pack(fill="x", pady=(2, 0))

stream_id_cb = ttk.Combobox(cb_row, values=[f"MIGU{i}" for i in range(1, 10)], state="readonly", width=14)
stream_id_cb.current(0)
stream_id_cb.pack(side="left")

# Checkbox Tunnel (Vượt QoS 16 luồng) - Mặc định BẬT
def on_toggle_tunnel():
    current_config["use_tunnel"] = tunnel_var.get()
    save_config(current_config)
    if tunnel_var.get():
        start_tunnel_service()
    else:
        stop_tunnel_service()

tunnel_var = tk.BooleanVar(value=current_config.get("use_tunnel", True))
tunnel_check = tk.Checkbutton(
    cb_row,
    text="🚀 Tunnel (16 luồng QoS)",
    variable=tunnel_var,
    font=("Segoe UI", 9, "bold"),
    fg="#0066cc",
    command=on_toggle_tunnel
)
tunnel_check.pack(side="left", padx=(10, 0))

# Checkbox -re (4K only) - Mặc định TẮT
re_var = tk.BooleanVar(value=False)
re_check = tk.Checkbutton(
    cb_row,
    text="-re (4K only)",
    variable=re_var,
    font=("Segoe UI", 9, "bold"),
    fg="#d32f2f"
)
re_check.pack(side="left", padx=(10, 0))

right_mid = tk.Frame(mid_frame)
right_mid.pack(side="right", padx=(10, 0), pady=(12, 0))

server_display_lbl = tk.Label(right_mid, text=f"Flussonic: {current_config.get('tunnel_remote_ip', '2.28.77.90')}", fg="#1a73e8", font=("Segoe UI", 9, "bold"))
server_display_lbl.pack(side="left", padx=(0, 8))

tk.Button(right_mid, text="⚙ Cài đặt", command=open_settings).pack(side="left")

# Status Bar cho Tunnel
tunnel_bar = tk.Frame(root)
tunnel_bar.pack(fill="x", padx=10, pady=(4, 0))
tunnel_status_lbl = tk.Label(tunnel_bar, text="⚪ Tunnel: Đang khởi tạo...", font=("Segoe UI", 9, "bold"), fg="#6c757d", anchor="w")
tunnel_status_lbl.pack(fill="x")

# Action Buttons
btn_frame = tk.Frame(root)
btn_frame.pack(pady=8)

tk.Button(btn_frame, text="START RELAY", width=18, bg="#28a745", fg="white", font=("Segoe UI", 9, "bold"), command=start_relay).grid(row=0, column=0, padx=5)
tk.Button(btn_frame, text="STOP SELECTED", width=18, bg="#dc3545", fg="white", font=("Segoe UI", 9, "bold"), command=stop_selected).grid(row=0, column=1, padx=5)
tk.Button(btn_frame, text="LÀM MỚI DANH SÁCH", width=18, command=refresh_list).grid(row=0, column=2, padx=5)

# Treeview List
columns = ("SRT URL", "PID")
relay_list = ttk.Treeview(root, columns=columns, show="headings", height=9)

relay_list.heading("SRT URL", text="SRT URL", command=lambda: sort_treeview("SRT URL", False))
relay_list.heading("PID", text="PID", command=lambda: sort_treeview("PID", False))

relay_list.column("SRT URL", width=620)
relay_list.column("PID", width=100)

relay_list.pack(fill="both", padx=10, pady=(2, 8))

# Khởi chạy tunnel nếu đang bật
if tunnel_var.get():
    start_tunnel_service()

# Start auto refresh loop
auto_refresh()

root.mainloop()
