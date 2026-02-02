#!/usr/bin/env python3
"""
Утилита для очистки orphaned процессов и окон bender
"""

import sys
import logging
from pathlib import Path

# Добавляем путь к bender модулю
sys.path.insert(0, str(Path(__file__).parent))

from bender.workers.copilot import cleanup_orphaned_processes

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

if __name__ == "__main__":
    print("🧹 Cleaning up orphaned bender processes and windows...")
    print()
    
    result = cleanup_orphaned_processes()
    
    print(f"✅ Cleanup complete!")
    print(f"   Killed processes: {result['total_killed']}")
    print(f"   Closed windows: {result['total_closed']}")
    print()
    
    if result['killed_processes']:
        print("Killed processes:")
        for proc in result['killed_processes']:
            print(f"  - {proc}")
        print()
    
    if result['closed_windows']:
        print("Closed windows:")
        for win in result['closed_windows']:
            print(f"  - {win}")
        print()
    
    if result['total_killed'] == 0 and result['total_closed'] == 0:
        print("No orphaned processes or windows found.")
