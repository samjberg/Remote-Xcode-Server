import os, sys, pathlib
from time import sleep

server_port = 8751
socket_port = 50682

def get_server_port() -> int:
    return server_port

def standardize_project_name(name:str) -> str:
    name.replace()

def unix_path(path:str) -> str:
    return pathlib.Path(path).as_posix()

def allowed_filename(filename:str) -> bool:
    valid_extensions = ['diff', 'txt']
    return filename.rsplit('.', 1)[-1].lower() in valid_extensions

def get_build_log_path(job_id:int) -> str:
    cwd = os.getcwd()
    uploads_path = os.path.join(cwd, 'uploads')
    build_log_path = os.path.join(uploads_path, f'buildlog.txt')
    return build_log_path


def uploads_folder_exists() -> bool:
    cwd = os.getcwd()
    if 'uploads' not in os.listdir(cwd):
        return False
    return os.path.isdir(os.path.join(cwd, 'uploads'))

def get_project_name() -> str:
    dir_contents = os.listdir()
    for name in dir_contents:
        name_parts = name.split('.')
        if name_parts[-1] == 'xcodeproj':
            return name_parts[0]
    return os.getcwd().split('/')[-1]

def get_project_root_path(cwd:str='.') -> str:
    if cwd == '.':
        cwd = os.getcwd()
    if '\\' in cwd:
        cwd = unix_path(cwd)
    if cwd.lower() in ['/', 'c:/']:
        print('Come on dude, seriously?  Don\'t pass in the filesystem root.')
    path_parts = cwd.split('/')
    while len(path_parts) > 1:
        current_path = '/'.join(path_parts)
        for name in os.listdir(current_path):
            if name.split('.')[-1] == 'xcodeproj': #if the file extension is 'xcodeproj'
                return current_path
        path_parts = path_parts[:-1]
    return cwd

def get_appname() -> str:
    root_path = get_project_root_path()
    for name in os.listdir(root_path):
        name_parts = name.split('.')
        if name_parts[-1] == 'xcodeproj':
            return name_parts[0].replace(' ', '_')
    return root_path.split('/')[-1].replace(' ', '_')


def sleep_ms(ms:float) -> None:
    sleep(ms/1000.0)

def parse_args() -> list[str]:
    args = sys.argv[1:]
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


