"""SportyBot entry point."""
import sys
import os

# Ensure project root is on the import path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.telegram_bot import main

if __name__ == "__main__":
    main()
