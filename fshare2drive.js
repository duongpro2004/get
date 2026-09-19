#!/usr/bin/env python3
"""
Screenshot to SRT Playlist Converter
======================================
Script này nằm cùng cấp với thư mục 'screenshots'.
Tự động tìm thư mục 'screenshots' ngay cạnh script, trích xuất IP và Port từ các file ảnh,
sau đó xuất ra file playlist .m3u để mở bằng VLC Media Player.

Sử dụng:
    python screenshot_to_playlist.py
"""

import os
import sys

# Thư mục chứa chính file script này
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Thư mục screenshots nằm cùng cấp với script
SCREENSHOT_DIR = os.path.join(SCRIPT_DIR, "screenshots")
# File playlist M3U cũng được lưu cùng cấp với script
OUTPUT_PLAYLIST = os.path.join(SCRIPT_DIR, "haivision_streams.m3u")


def generate_playlist():
    if not os.path.exists(SCREENSHOT_DIR):
        print(f"[!] Lỗi: Không tìm thấy thư mục 'screenshots' tại:\n    {SCREENSHOT_DIR}")
        print("👉 Vui lòng đặt file script này nằm cùng cấp (ngay cạnh) thư mục 'screenshots'.")
        return

    print(f"\n{'=' * 60}")
    print("🎬 CHUYỂN ĐỔI SCREENSHOTS THÀNH PLAYLIST SRT CHO VLC")
    print(f"{'=' * 60}")
    print(f"📁 Thư mục screenshots: {SCREENSHOT_DIR}")
    print(f"📄 File playlist lưu tại: {OUTPUT_PLAYLIST}")
    print(f"{'=' * 60}\n")

    streams_by_ip = {}
    valid_extensions = {".jpg", ".jpeg", ".png"}

    # Duyệt qua các thư mục con trong 'screenshots'
    for root, _, files in os.walk(SCREENSHOT_DIR):
        ip_folder = os.path.basename(root)

        if root == SCREENSHOT_DIR:
            continue

        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext not in valid_extensions:
                continue

            port_str = os.path.splitext(file)[0]
            # Kiểm tra port là số hợp lệ
            if port_str.isdigit():
                port = int(port_str)
                file_path = os.path.join(root, file)

                # Chỉ lấy ảnh có dung lượng > 0 byte
                if os.path.getsize(file_path) > 0:
                    if ip_folder not in streams_by_ip:
                        streams_by_ip[ip_folder] = []
                    streams_by_ip[ip_folder].append((port, file_path))

    if not streams_by_ip:
        print("[!] Không tìm thấy ảnh chụp port nào hợp lệ trong thư mục 'screenshots'.")
        return

    total_streams = 0
    m3u_lines = ["#EXTM3U", "#PLAYLIST:Haivision SRT Active Streams\n"]

    for ip in sorted(streams_by_ip.keys()):
        ports = sorted(streams_by_ip[ip], key=lambda x: x[0])
        total_streams += len(ports)

        m3u_lines.append(f"# ==================== IP: {ip} ({len(ports)} streams) ====================")
        for port, img_path in ports:
            srt_url = f"srt://{ip}:{port}"
            clean_img_path = img_path.replace("\\", "/")
            extinf = f'#EXTINF:-1 tvg-name="{ip}:{port}" tvg-logo="{clean_img_path}" group-title="{ip}", [{ip}:{port}]'
            m3u_lines.append(extinf)
            m3u_lines.append(srt_url)
        m3u_lines.append("")

    try:
        with open(OUTPUT_PLAYLIST, "w", encoding="utf-8") as f:
            f.write("\n".join(m3u_lines))

        print(f"[✓] Thành công!")
        print(f"    - Tổng số IP tìm thấy   : {len(streams_by_ip)}")
        print(f"    - Tổng số stream SRT    : {total_streams}")
        print(f"    - File playlist đã lưu  : {OUTPUT_PLAYLIST}")
        print(f"\n👉 Kéo thả file '{os.path.basename(OUTPUT_PLAYLIST)}' vào VLC Media Player để xem ngay.")
    except Exception as e:
        print(f"[!] Lỗi khi ghi file: {e}")


if __name__ == "__main__":
    generate_playlist()
