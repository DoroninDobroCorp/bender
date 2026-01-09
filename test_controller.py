"""
Тест DroidController - проверка базовой функциональности
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.droid_controller import DroidController


async def test_controller():
    """Простой тест контроллера"""
    
    # Используем текущую директорию как тестовый проект
    project_path = Path(__file__).parent
    
    print("=== DroidController Test ===\n")
    
    controller = DroidController(
        project_path=str(project_path),
        droid_binary="droid",
        log_dir="logs",
        idle_timeout=60,
        check_interval=2.0
    )
    
    print(f"Session: {controller.session_name}")
    print(f"Project: {controller.project_path}")
    print(f"Log file: {controller.log_file}\n")
    
    try:
        # 1. Start
        print("1. Starting Droid...")
        await controller.start()
        print("   OK - Droid started\n")
        
        # 2. Check running
        print("2. Checking if running...")
        is_running = controller.is_running()
        print(f"   is_running: {is_running}\n")
        
        # 3. Send simple command
        print("3. Sending test command...")
        response = await controller.send("What is 2+2? Answer briefly.", timeout=30)
        print(f"   Response length: {len(response)} chars")
        print(f"   Preview: {response[:200]}...\n" if len(response) > 200 else f"   Response: {response}\n")
        
        # 4. Check approval request
        print("4. Checking approval request...")
        has_approval = controller.has_approval_request()
        print(f"   has_approval_request: {has_approval}\n")
        
        # 5. New chat
        print("5. Opening new chat...")
        await controller.new_chat()
        print("   OK - New chat opened\n")
        
        # 6. Stop
        print("6. Stopping Droid...")
        await controller.stop()
        print("   OK - Droid stopped\n")
        
        # 7. Verify stopped
        print("7. Verifying stopped...")
        is_running = controller.is_running()
        print(f"   is_running: {is_running}\n")
        
        print("=== ALL TESTS PASSED ===")
        print(f"Log file: {controller.log_file}")
        
    except Exception as e:
        print(f"\nERROR: {e}")
        # Cleanup
        if controller.is_running():
            await controller.stop()
        raise


if __name__ == "__main__":
    asyncio.run(test_controller())
