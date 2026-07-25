import sys
import argparse
from src.cli import run_cli
from src.app import start_ui

def main():
    parser = argparse.ArgumentParser(description="Drive Organizer", add_help=False)
    parser.add_argument('--cli', action='store_true', help="Run in Terminal CLI mode")
    
    args, unknown = parser.parse_known_args()
    
    if args.cli:
        if '--cli' in sys.argv:
            sys.argv.remove('--cli')
        run_cli()
    else:
        start_ui()

if __name__ == '__main__':
    main()
