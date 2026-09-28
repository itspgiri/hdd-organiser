import sys
import argparse
from src.cli import run_cli
from src.app import start_ui


def main():
    parser = argparse.ArgumentParser(description="Drive Organizer", add_help=False)
    parser.add_argument('--cli', action='store_true', help="Run in Terminal CLI mode")
    parser.add_argument('-h', '--help', action='store_true', help="Show help message and exit")

    args, unknown = parser.parse_known_args()

    # Route to CLI if --cli, -h/--help, or CLI positional/flag arguments were supplied
    if args.cli or args.help or unknown:
        if '--cli' in sys.argv:
            sys.argv.remove('--cli')
        run_cli()
    else:
        start_ui()


if __name__ == '__main__':
    main()

