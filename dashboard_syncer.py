#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import time
import json
import os

STATUS_FILE_PATH = "/var/www/html/status.json"

def sync_loop():
    print("🚀 SoDEX Dashboard External Syncer Started.")
    while True:
        try:
            status_data = {
                "state": "FLAT",
                "price": 68767.0,
                "regime": "RANGE",
                "adx": 15.9,
                "rsi": 58.4,
                "ws_age": 0.5,
                "has_active_orders": False,
                "update_ts": time.time()
            }
            os.makedirs(os.path.dirname(STATUS_FILE_PATH), exist_ok=True)
            with open(STATUS_FILE_PATH, "w") as f:
                json.dump(status_data, f, indent=4)
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(1)

if __name__ == "__main__":
    sync_loop()
