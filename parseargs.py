import sys, os

def parse_args(args:list[str]) -> list[str]:
    piped = not sys.stdin.isatty()
    if piped:
        text = sys.stdin.read()
        return_args = args
    else:
        text = args[-1]
        return_args = args[:-1]

    filename = text    
    if os.path.isfile(filename):
        with open(filename, 'r') as f:
            text = f.read()
    return text, return_args
